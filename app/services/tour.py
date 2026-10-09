"""The new-user walkthrough and the "Get set up" checklist.

The tour is a list of steps, each on one page and pointing at one thing on it (static/js/tour.js draws
the highlight and the explanation). It starts after /welcome, waits wherever the student wanders (a
"Continue the tour" pill), and can be restarted from the profile. Setup steps check themselves off from
what the server can see (status()); the two it can't see, a calendar link added to a phone and hiding
the checklist, are marks the student sets (User.tour_marks). Steps for things this server doesn't
offer (AI, Google) or the student can't use yet (files before any Canvas class) are left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import has_request_context, request, url_for
from sqlalchemy import select

from ..extensions import db
from ..models import CanvasAccount, Course, Deck, PracticeQuiz, TutorConversation, TutorMessage, User, utcnow
from . import ai, integrations

MARKS = ("phone_calendar", "hide_setup")


@dataclass
class Step:
    id: str
    endpoint: str
    title: str
    body: str
    # CSS selectors, first visible one wins (none: a card in the middle). phone_target is tried first on
    # narrow screens; whichever matches decides the text (phone_body when phone_target matched).
    target: str = ""
    args: dict = field(default_factory=dict)
    check: str = ""                        # a status() key this step is done by
    phone_body: str = ""
    phone_target: str = ""
    mark: str = ""                         # a MARKS key the step's "I've added it" button sets
    when: str = ""                         # "google" / "ai" / "canvas": only when offered / possible
    prefix: bool = False                   # shown on any page under the step's path (/tutor/ opens a chat)


STEPS = [
    Step("welcome", "main.dashboard", "Welcome to Homework Hatch!",
         "This quick tour gets your school connected and shows you around. It takes about three minutes. "
         "Use Next and Back, or click around whenever you like: the tour waits for you, and you can restart it from "
         "your profile."),
    Step("menu", "main.dashboard", "Everything is in this menu",
         "Home, Classes, Calendar, Study, Tutor, Chat and Files. Your account, coins and sign-out are at the bottom.",
         target=".rail .nav", phone_target="[data-toggle-sidebar]",
         phone_body="Tap here for Home, Classes, Calendar, Study, Tutor, Chat and Files, plus your account."),
    Step("canvas", "settings.sync", "Connect Canvas",
         "Homework Hatch reads Canvas through a Chrome extension that syncs as you, in your own browser, so we never "
         "see your Canvas password. Add it, click Connect on your Canvas, and your classes, due dates and grades "
         "arrive within a couple of minutes. This step checks itself off when they do.",
         target="#ext", phone_target=".touch-only", check="school",
         phone_body="The Canvas extension runs in Chrome on a laptop or Chromebook, so do that part there (the tour "
                    "will wait). Not on Canvas? The next step works from here."),
    Step("calendar_link", "settings.sync", "Not on Canvas?",
         "Brightspace, Blackboard, Moodle and Schoology give you a private calendar link. Paste it here and your "
         "due dates come in, checked again about every hour. The card says where to find the link.",
         target="#calendar-links", check="school"),
    Step("files", "settings.sync", "Pick which classes' files to keep",
         "Tick the classes whose files you want kept (or choose Keep files for all my classes), then Save. Kept files "
         "power the tutor and AI flashcards, and you can change this any time.",
         target="#files", check="files", when="canvas"),
    Step("google", "settings.integrations_page", "Connect Google (optional)",
         "Put your upcoming due dates in Google Calendar automatically, and pick files from Google Drive for the tutor "
         "and AI sets.", target="main .card", check="google", when="google"),
    Step("home", "main.dashboard", "Home",
         "The week at a glance, what's due next, your grades and anything planned for today. A dot on a day means "
         "something's due.", target=".week"),
    Step("classes", "courses.index", "Your classes",
         "Every Canvas class gets a page: assignments, grades with Brain Grade (your test grade next to everything "
         "else), modules, files and announcements. Classes from a calendar link show their due dates. Customize "
         "renames or recolors a class just for you.", target=".class-card, main .card"),
    Step("calendar", "main.calendar_view", "Your calendar",
         "Due dates, school events, study sessions and anything you add with + Add, in day, week or month view.",
         target=".cal-controls", args={"view": "week"}),
    Step("phone", "main.calendar_view", "Put it on your phone",
         "Copy this private link into Google Calendar, Apple Calendar or Outlook and it stays up to date by itself. "
         "Keep it to yourself: anyone with it can see your due dates and everything on your calendar, notes included.",
         target="#subscribe-card", args={"view": "week"}, check="phone", mark="phone_calendar"),
    Step("study", "study.index", "Study",
         "Your flashcards and practice quizzes, with Learn and Test modes. Have sets in Quizlet or Anki? Import them "
         "for free.", target=".tabs"),
    Step("generate", "study.generate", "Make a set with AI",
         "Build flashcards or a quiz from files you pick, a topic in a class, your notes, or just a description of "
         "what you're studying. Each set uses one AI action.", target="#gen-modes", when="ai"),
    Step("exams", "planner.index", "Exams",
         "We find your tests and quizzes and build a study schedule that fits your day. The sessions show up on your "
         "calendar.", target="section.card"),
    Step("tutor", "tutor.index", "The tutor",
         "Ask about anything in your classes. It answers from your own materials and cites them. Ask it to plan your "
         "week, then add the plan to your calendar in one tap.", target=".tutor-layout, main .card", prefix=True,
         when="ai"),
    Step("chat", "chat.index", "Chat",
         "Each Canvas class you take as a student gets a room with the classmates who use Homework Hatch. You can "
         "message a classmate privately once they accept.", target="#rooms-card"),
    Step("myfiles", "uploads.index", "Your files",
         "Upload notes, readings or study guides to use with the tutor and AI sets.", target="#upload-form"),
    Step("finish", "main.dashboard", "You're all set!",
         "The Get set up checklist on Home shows anything left to connect. Restart this tour any time from your "
         "profile.", target="#setup-card"),
]
BY_ID = {s.id: s for s in STEPS}


def _files_choices(user: User) -> list[bool | None]:
    """sync_files of the student's current Canvas classes (None: not chosen yet)."""
    return list(db.session.scalars(select(Course.sync_files).join(CanvasAccount, CanvasAccount.id == Course.account_id).where(
        Course.user_id == user.id, Course.active.is_(True), CanvasAccount.lms == "canvas")))


def status(user: User) -> dict[str, bool]:
    """What's set up: a school connected (Canvas synced, or a calendar link read), files chosen for every
    Canvas class, Google, the phone calendar (the student says so), a first study set, a first tutor
    question. Computed once per request."""
    cache = request.environ.setdefault("hh.tour_status", {}) if has_request_context() else {}
    if user.id in cache:
        return cache[user.id]
    marks = set(user.tour_marks or [])
    choices = _files_choices(user)
    out = {
        "school": db.session.scalar(select(CanvasAccount.id).where(
            CanvasAccount.user_id == user.id, CanvasAccount.last_sync_at.is_not(None)).limit(1)) is not None,
        # Like the dashboard's "not chosen yet" banner: a class the next sync brings in needs a choice too.
        "files": bool(choices) and (user.keep_all_files is True or all(c is not None for c in choices)),
        "phone": "phone_calendar" in marks,
        "study": db.session.scalar(select(Deck.id).where(Deck.user_id == user.id).limit(1)) is not None
                 or db.session.scalar(select(PracticeQuiz.id).where(PracticeQuiz.user_id == user.id).limit(1)) is not None,
        "tutor": db.session.scalar(select(TutorMessage.id).join(TutorConversation).where(
            TutorConversation.user_id == user.id, TutorMessage.role == "user").limit(1)) is not None,
    }
    if integrations.available():
        out["google"] = integrations.connected(user, "calendar") or integrations.connected(user, "drive")
    out["_canvas_classes"] = bool(choices)
    cache[user.id] = out
    return out


def checklist(user: User) -> list[dict]:
    """The Get set up card's items, in order (files once there are Canvas classes; Google and the tutor
    only when offered)."""
    s = status(user)
    items = [("school", "Connect your school", url_for("settings.sync"),
              "Canvas through the Chrome extension, or a calendar link from another school system.")]
    if s["_canvas_classes"]:
        items.append(("files", "Choose which classes' files to keep", url_for("settings.sync") + "#files",
                      "They power the tutor and AI flashcards."))
    items.append(("phone", "Put your calendar on your phone", url_for("main.calendar_view") + "#subscribe-card",
                  "Copy your private calendar link into your phone's calendar app."))
    if "google" in s:
        items.append(("google", "Connect Google (optional)", url_for("settings.integrations_page"),
                      "Due dates in Google Calendar, files from Drive."))
    if ai.available():
        items += [("study", "Make your first study set", url_for("study.generate"), "From your files, notes or a description."),
                  ("tutor", "Ask the tutor something", url_for("tutor.index"), "It answers from your own class materials.")]
    else:
        items.append(("study", "Make or import your first study set", url_for("study.import_deck"),
                      "Write your own cards, or bring a set from Quizlet or Anki."))
    return [{"key": k, "label": label, "url": url, "help": hint, "done": s[k], "optional": k == "google"}
            for k, label, url, hint in items]


def setup_card(user: User) -> list[dict] | None:
    """The checklist while it's worth showing: not hidden, and something required still to do."""
    if "hide_setup" in (user.tour_marks or []):
        return None
    items = checklist(user)
    return None if all(i["done"] for i in items if not i["optional"]) else items


def steps(user: User) -> list[Step]:
    s = status(user)
    offered = {"google": "google" in s, "ai": ai.available(), "canvas": s["_canvas_classes"]}
    return [x for x in STEPS if not x.when or offered[x.when]]


def config(user: User) -> dict | None:
    """What static/js/tour.js needs on every page while the student is touring, or None."""
    if not user.tour_step:
        return None
    mine = steps(user)
    ids = [x.id for x in mine]
    s = status(user)
    card = setup_card(user) is not None
    out = []
    for x in mine:
        body, target = x.body, x.target
        if x.id == "myfiles" and integrations.available():
            body += " You can also pick them from Google Drive."
        if x.id == "finish" and not card:
            body, target = "Restart this tour any time from your profile.", ""
        out.append({"id": x.id, "url": url_for(x.endpoint, **x.args), "path": url_for(x.endpoint), "title": x.title,
                    "body": body, "phone_body": x.phone_body or body, "target": target, "phone_target": x.phone_target,
                    "check": x.check, "mark": x.mark, "prefix": x.prefix, "done": bool(x.check and s.get(x.check))})
    return {"current": user.tour_step if user.tour_step in ids else ids[0], "steps": out,
            "urls": {"step": url_for("tour.step"), "end": url_for("tour.end"), "status": url_for("tour.status_json"),
                     "mark": url_for("tour.mark")}}


def start(user: User) -> str:
    user.tour_step, user.tour_done_at = STEPS[0].id, None
    user.tour_marks = [m for m in (user.tour_marks or []) if m != "hide_setup"]  # the last step points at the checklist
    return user.tour_step


def finish(user: User) -> None:
    user.tour_step, user.tour_done_at = None, utcnow()


def set_mark(user: User, key: str, on: bool = True) -> None:
    if key not in MARKS:
        raise ValueError(key)
    marks = [m for m in (user.tour_marks or []) if m in MARKS and m != key]
    user.tour_marks = marks + ([key] if on else [])
    if has_request_context():
        request.environ.get("hh.tour_status", {}).pop(user.id, None)
