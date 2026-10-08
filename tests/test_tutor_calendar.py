"""Tutor -> calendar: the tutor plans with real dates and suggests items in a hidden block; the student
picks and edits them and adds them to their Homework Hatch calendar (and its feed), or adds items by hand."""

from __future__ import annotations

import json
import shutil

import pytest
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.extensions import db
from app.models import Course, TutorMessage, User, UserEvent
from app.services import myevents
from app.utils import local_now

from .conftest import login, make_user

NY = ZoneInfo("America/New_York")


def _conv(client, course_id=None) -> int:
    r = client.post("/tutor/new", data={"course_id": course_id or ""})
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def _ask(client, conv_id, text="Help me plan for the midterm"):
    body = client.post(f"/tutor/{conv_id}/message", json={"text": text}).get_data(as_text=True)
    return json.loads(body.split("event: done\ndata: ", 1)[1].split("\n", 1)[0]), body


def _plan(today: date) -> str:
    d1, d2 = today + timedelta(days=1), today + timedelta(days=2)
    items = [{"title": "Review chapter 3 notes", "date": d1.isoformat(), "start": "19:00", "end": "20:00", "class": "Calculus I"},
             {"title": "Practice problems", "date": d2.isoformat(), "notes": "odd ones"},
             {"title": "Too old", "date": (today - timedelta(days=10)).isoformat()},
             {"title": "", "date": d1.isoformat()}, "not an item"]
    return (f"Here's a plan:\n\n- Tomorrow 7-8pm: review chapter 3\n- Then practice problems\n\n"
            f"<hh-calendar>\n{json.dumps(items)}\n</hh-calendar>")


def _utc(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=NY).astimezone(timezone.utc).replace(tzinfo=None)


def test_tutor_plans_with_real_dates_and_suggests_items(synced_user, client, fake_ai):
    fake_ai.close_session_before_streaming = True
    today = local_now(synced_user).date()
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    db.session.add(UserEvent(user_id=synced_user.id, title="Lab group meeting", start_at=_utc(today + timedelta(days=3), 15)))
    db.session.commit()
    fake_ai.answer = _plan(today)
    conv_id = _conv(client, course.id)
    done, body = _ask(client, conv_id)
    # The tutor got the date, the time zone, what's due and what's already planned, and the rules for the block.
    call = fake_ai.calls[-1]
    prompt = call["messages"][-1]["content"]
    assert prompt.startswith("<today>") and f"{today:%B %-d, %Y}" in prompt and "America/New_York" in prompt
    assert "Due soon:" in prompt and "HW 3" in prompt and "Lab group meeting" in prompt
    assert "<hh-calendar>" in (call["system"] if isinstance(call["system"], str) else call["system"][0]["text"])
    # The answer reads without the block, which never reaches the page, and its valid items come back.
    assert "hh-calendar" not in done["html"] and "review chapter 3" in done["html"]
    assert [i["title"] for i in done["calendar"]] == ["Review chapter 3 notes", "Practice problems"]
    assert done["calendar"][0]["start"] == "19:00" and done["calendar"][1]["start"] is None
    msg = db.session.get(TutorMessage, done["id"])
    assert msg.role == "assistant" and "hh-calendar" not in msg.content and len(msg.calendar) == 2
    raw = db.session.execute(text("select calendar from tutor_message where id = :i"), {"i": msg.id}).scalar()
    assert raw.startswith("enc1:"), "suggested items are encrypted like the rest of the chat"
    # The page offers them under the answer.
    page = client.get(f"/tutor/{conv_id}").get_data(as_text=True)
    assert f'data-id="{msg.id}"' in page and "Review chapter 3 notes" in page and "tutor_calendar.js" in page
    # A follow-up sees the list it suggested, so "move it to Thursday" can send a corrected one.
    fake_ai.answer = "Sure."
    _ask(client, conv_id, "Move the review to Thursday")
    history = fake_ai.calls[-1]["messages"]
    assert history[1]["role"] == "assistant" and "<hh-calendar>" in history[1]["content"]


def test_adding_suggested_items(app, synced_user, client, fake_ai):
    today = local_now(synced_user).date()
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    fake_ai.answer = _plan(today)
    conv_id = _conv(client)
    done, _ = _ask(client, conv_id)
    items = [dict(done["calendar"][0], title="Review ch. 3 (edited)", course_id=course.id),
             dict(done["calendar"][1], course_id=None)]
    r = client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": items})
    assert r.status_code == 200 and r.get_json()["added"] == 2
    timed, all_day = db.session.scalars(select(UserEvent).order_by(UserEvent.id)).all()
    d1, d2 = today + timedelta(days=1), today + timedelta(days=2)
    assert timed.title == "Review ch. 3 (edited)" and timed.course_id == course.id and timed.source == "tutor"
    assert timed.start_at == _utc(d1, 19) and timed.end_at == _utc(d1, 20) and not timed.all_day
    assert all_day.all_day and all_day.all_day_date == d2 and all_day.notes == "odd ones"
    assert db.session.execute(text("select title from user_event where id = :i"), {"i": timed.id}).scalar().startswith("enc1:")
    # Adding again doesn't duplicate.
    again = client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": items}).get_json()
    assert again["added"] == 0 and again["skipped"] == 2
    assert 'data-added="2"' in client.get(f"/tutor/{conv_id}").get_data(as_text=True)
    # They're on the calendar (day, week and month), the dashboard and the subscribe link.
    week = client.get(f"/calendar?view=week&d={d1.isoformat()}").get_data(as_text=True)
    assert "Review ch. 3 (edited)" in week and "Practice problems" in week and "7:00 PM" in week
    assert "Review ch. 3 (edited)" in client.get(f"/calendar?view=month&d={d1.isoformat()}").get_data(as_text=True)
    assert "Practice problems" in client.get(f"/calendar?view=day&d={d2.isoformat()}").get_data(as_text=True)
    feed = client.get(f"/calendar/{synced_user.calendar_token}.ics").get_data(as_text=True)
    assert f"UID:mine-{timed.id}@" in feed and f"DTSTART;VALUE=DATE:{d2:%Y%m%d}" in feed
    assert f"DTSTART:{timed.start_at:%Y%m%dT%H%M%SZ}" in feed and "Review ch. 3 (edited) (Calculus I)" in feed
    from app.services.feeds import OWN_UID

    assert OWN_UID.match(f"mine-{timed.id}@hatch.test"), "re-imported subscriptions skip our own items"
    # Bad requests and other people's answers.
    assert client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": []}).status_code == 400
    assert client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": "x"}).status_code == 400
    assert client.post(f"/tutor/messages/{done['id']}/calendar", json=["x"]).status_code == 400
    bad = client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": [{"title": "x", "date": "2099-01-01"}]})
    assert bad.status_code == 400 and "two years" in bad.get_json()["error"]
    user_msg = db.session.scalar(select(TutorMessage).where(TutorMessage.role == "user"))
    assert client.post(f"/tutor/messages/{user_msg.id}/calendar", json={"items": items}).status_code == 404
    other = make_user("olive")
    oc = app.test_client()
    login(oc, other)
    assert oc.post(f"/tutor/messages/{done['id']}/calendar", json={"items": items}).status_code == 404
    assert "Review ch. 3" not in oc.get(f"/calendar?view=week&d={d1.isoformat()}").get_data(as_text=True)


def test_items_by_hand_edit_done_and_delete(app, synced_user, client):
    today = local_now(synced_user).date()
    assert client.get(f"/calendar/items/new?d={today.isoformat()}").status_code == 200
    r = client.post("/calendar/items/new", data={"title": "Office hours", "date": today.isoformat(), "start": "14:30", "end": ""})
    assert r.status_code == 302
    e = db.session.scalar(select(UserEvent))
    assert e.source == "manual" and e.end_at - e.start_at == timedelta(hours=1)
    assert "Office hours" in client.get("/dashboard").get_data(as_text=True), "today's items are on the dashboard"
    client.post(f"/calendar/items/{e.id}", data={"title": "Office hours (Rice 120)", "date": today.isoformat(), "all_day": "1"})
    e = db.session.get(UserEvent, e.id)
    assert e.title == "Office hours (Rice 120)" and e.all_day and e.all_day_date == today
    assert "Office hours (Rice 120)" in client.get(f"/calendar/items/{e.id}").get_data(as_text=True)
    client.post(f"/calendar/items/{e.id}/done")
    assert db.session.get(UserEvent, e.id).done_at is not None
    assert client.post("/calendar/items/new", data={"title": "", "date": today.isoformat()}).status_code == 400
    # Someone else can't see, change or delete it.
    other = make_user("olive")
    oc = app.test_client()
    login(oc, other)
    assert oc.get(f"/calendar/items/{e.id}").status_code == 404
    assert oc.post(f"/calendar/items/{e.id}/delete").status_code == 404
    client.post(f"/calendar/items/{e.id}/delete")
    assert db.session.get(UserEvent, e.id) is None


def test_export_and_account_deletion(synced_user, client, fake_ai):
    today = local_now(synced_user).date()
    fake_ai.answer = _plan(today)
    done, _ = _ask(client, _conv(client))
    client.post(f"/tutor/messages/{done['id']}/calendar", json={"items": done["calendar"]})
    data = client.get("/settings/data/export").get_json()
    assert [i["title"] for i in data["calendar_items"]] == ["Review chapter 3 notes", "Practice problems"]
    assert data["tutor"][0]["messages"][1]["calendar"]
    db.session.delete(db.session.get(User, synced_user.id))
    db.session.commit()
    assert db.session.scalar(select(UserEvent.id)) is None


def test_reading_the_block():
    today = date(2026, 10, 8)
    fenced = 'Plan below.\n```\n<hh-calendar>\n[{"title": "A", "date": "2026-10-09", "start": "7pm"}]\n</hh-calendar>\n```\nGood luck!'
    clean, items = myevents.split_answer(fenced, today)
    assert clean == "Plan below.\n\nGood luck!" and items[0]["start"] == "19:00"
    cut, items = myevents.split_answer('Plan.\n<hh-calendar>\n[{"title": "A", "date": "2026-10-0', today)
    assert cut == "Plan." and items == [], "an answer cut off mid-block keeps its text"
    lines = '<hh-calendar>\n{"title": "A", "date": "2026-10-09"},\n{"title": "B", "date": "2026-10-10", "start": "9:30 AM", "end": "9:00"}\n</hh-calendar>'
    _, items = myevents.split_answer(lines, today)
    assert [(i["title"], i["start"], i["end"]) for i in items] == [("A", None, None), ("B", "09:30", None)]
    many = "<hh-calendar>" + json.dumps([{"title": f"T{i}", "date": "2026-10-10"} for i in range(50)]) + "</hh-calendar>"
    assert len(myevents.split_answer(many, today)[1]) == myevents.MAX_SUGGESTED
    assert myevents.split_answer("No plan here.", today) == ("No plan here.", [])
    assert myevents._clock("25:00") is None and myevents._clock("12am") == "00:00" and myevents._clock("12:15 pm") == "12:15"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stream_hides_the_block():
    node = shutil.which("node")
    script = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "tutor_calendar.js"
    js = f"""
const vm = require("vm"); const fs = require("fs");
const ctx = {{ window: {{ HH_CAL: {{ courses: [], course: null, today: "2026-10-08", addUrl: "/x/0/calendar" }} }},
  document: {{ getElementById: () => null, querySelectorAll: () => [] }}, hh: {{}} }};
vm.createContext(ctx); vm.runInContext(fs.readFileSync({json.dumps(str(script))}, "utf8"), ctx);
const v = ctx.window.hhCalVisible;
console.log(JSON.stringify([v("Plan:\\n<hh-calendar>[{{"), v("Plan:\\n<hh-c"), v("a < b"), v("Plan:\\n<"),
  v("I put them in the <hh-calendar> list"), v("Plan:\\n```\\n<hh-cal"), v("Plan:\\n```json\\n<hh-calendar>\\n[")]));
"""
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["Plan:\n", "Plan:\n", "a < b", "Plan:\n", "I put them in the <hh-calendar> list",
                                      "Plan:\n", "Plan:\n"]


def test_reading_odd_answers():
    today = date(2026, 10, 8)
    item = '[{"title": "A", "date": "2026-10-09"}]'
    # A mention of the tag mid-sentence isn't the block, and isn't cut.
    clean, items = myevents.split_answer(f"I put the dates in the <hh-calendar> list below.\n\n- Fri: A\n\n<hh-calendar>\n{item}\n</hh-calendar>", today)
    assert clean.startswith("I put the dates in the <hh-calendar> list below.") and "- Fri: A" in clean and len(items) == 1
    # Two blocks: both lists are offered.
    two = f'Week 1:\n<hh-calendar>{item}</hh-calendar>\nWeek 2:\n<hh-calendar>[{{"title": "B", "date": "2026-10-16"}}]</hh-calendar>'
    assert [i["title"] for i in myevents.split_answer(two, today)[1]] == ["A", "B"]
    # Code blocks next to the block keep their fences.
    before = f"Try:\n```python\nprint(1)\n```\n<hh-calendar>\n{item}\n</hh-calendar>"
    assert myevents.split_answer(before, today)[0] == "Try:\n```python\nprint(1)\n```"
    after = f"A\n<hh-calendar>{item}</hh-calendar>\n```python\nprint(1)\n```\nEnd"
    assert myevents.split_answer(after, today)[0] == "A\n\n```python\nprint(1)\n```\nEnd"
    # Overnight items keep their end; backwards or 0-length ones get the default length.
    night = ('<hh-calendar>[{"title": "N", "date": "2026-10-09", "start": "22:00", "end": "00:30"},'
             '{"title": "M", "date": "2026-10-09", "start": "22:00", "end": "24:00"},'
             '{"title": "Z", "date": "2026-10-09", "start": "19:00", "end": "19:00"}]</hh-calendar>')
    assert [(i["start"], i["end"]) for i in myevents.split_answer(night, today)[1]] == [("22:00", "00:30"), ("22:00", "00:00"), ("19:00", None)]


def test_times_overnight_clock_changes_and_feed_text(synced_user, client):
    today = local_now(synced_user).date()
    client.post("/calendar/items/new", data={"title": "Late study", "date": today.isoformat(), "start": "22:00", "end": "00:30"})
    e = db.session.scalars(select(UserEvent).order_by(UserEvent.id)).all()[-1]
    assert e.end_at - e.start_at == timedelta(hours=2, minutes=30)
    r = client.post("/calendar/items/new", data={"title": "Backwards", "date": today.isoformat(), "start": "19:00", "end": "18:00"})
    assert r.status_code == 400 and "ends before it starts" in r.get_data(as_text=True)
    # 2:30 AM doesn't exist on the day clocks spring forward: the item still ends after it starts.
    client.post("/calendar/items/new", data={"title": "Gap", "date": "2027-03-14", "start": "02:30", "end": "03:00"})
    gap = db.session.scalars(select(UserEvent).order_by(UserEvent.id)).all()[-1]
    assert gap.end_at > gap.start_at
    # Notes typed on two lines don't put a bare carriage return in the feed.
    client.post("/calendar/items/new", data={"title": "Notes", "date": today.isoformat(), "notes": "Bring HW 3\r\nRoom 120"})
    feed = client.get(f"/calendar/{synced_user.calendar_token}.ics").get_data(as_text=False)
    assert b"DESCRIPTION:Bring HW 3\\nRoom 120" in feed and b"\r\\n" not in feed


def test_revised_plans_dont_duplicate(synced_user, client, fake_ai):
    today = local_now(synced_user).date()
    d1, d2, d3 = (today + timedelta(days=n) for n in (1, 2, 3))
    plan = lambda practice_day: ("Plan:\n<hh-calendar>" + json.dumps([  # noqa: E731
        {"title": "Review ch 3", "date": d1.isoformat(), "start": "19:00"},
        {"title": "Practice set", "date": practice_day.isoformat(), "start": "19:00"}]) + "</hh-calendar>")
    fake_ai.answer = plan(d2)
    conv_id = _conv(client)
    first, _ = _ask(client, conv_id)
    client.post(f"/tutor/messages/{first['id']}/calendar", json={"items": first["calendar"]})
    # "Move the practice set a day later": the unchanged item shows as already added.
    fake_ai.answer = plan(d3)
    second, _ = _ask(client, conv_id, "Move the practice set a day later")
    assert [i["have"] for i in second["calendar"]] == [True, False]
    r = client.post(f"/tutor/messages/{second['id']}/calendar", json={"items": second["calendar"]}).get_json()
    assert r["added"] == 1 and r["skipped"] == 1
    # The old practice set is offered for removal, and removing it leaves the revised plan.
    assert [e["title"] for e in r["earlier"]] == ["Practice set"]
    other = make_user("olive")
    stranger = UserEvent(user_id=other.id, title="Not yours", start_at=_utc(d1, 9))
    db.session.add(stranger)
    db.session.commit()
    gone = client.post(r["remove_url"], json={"ids": [e["id"] for e in r["earlier"]] + [stranger.id]}).get_json()
    assert gone["removed"] == 1 and db.session.get(UserEvent, stranger.id) is not None
    mine = sorted((e.title, myevents.local_day(e, synced_user)) for e in db.session.scalars(select(UserEvent).where(UserEvent.user_id == synced_user.id)))
    assert mine == [("Practice set", d3), ("Review ch 3", d1)]
    # Items added by hand under an answer without suggestions are remembered after a reload.
    fake_ai.answer = "Sure."
    plain, _ = _ask(client, conv_id, "thanks")
    client.post(f"/tutor/messages/{plain['id']}/calendar", json={"items": [{"title": "Office hours", "date": d1.isoformat()}]})
    assert f'data-id="{plain["id"]}" data-calendar=\'[]\' data-added="1"' in client.get(f"/tutor/{conv_id}").get_data(as_text=True)


def test_caps_hidden_classes_and_context_all_day(synced_user, client, monkeypatch):
    from app.models import CalendarEvent, CanvasAccount

    today = local_now(synced_user).date()
    # The cap counts every item, past ones too.
    monkeypatch.setattr(myevents, "MAX_ITEMS", 2)
    for n in range(3):
        client.post("/calendar/items/new", data={"title": f"Old {n}", "date": (today - timedelta(days=45)).isoformat()})
    assert db.session.scalar(select(func.count(UserEvent.id))) == 2
    monkeypatch.setattr(myevents, "MAX_ITEMS", 5000)
    # Editing an item of a hidden class keeps the class.
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    client.post("/calendar/items/new", data={"title": "Calc review", "date": today.isoformat(), "course_id": course.id})
    e = db.session.scalars(select(UserEvent).order_by(UserEvent.id)).all()[-1]
    course.hidden = True
    db.session.commit()
    page = client.get(f"/calendar/items/{e.id}").get_data(as_text=True)
    assert f'<option value="{course.id}" selected' in page
    # The tutor sees a Canvas all-day event on its own date, not at midnight in another zone.
    account = db.session.scalar(select(CanvasAccount))
    ny_midnight = _utc(today + timedelta(days=2), 0)
    db.session.add(CalendarEvent(user_id=synced_user.id, account_id=account.id, canvas_id="hol", title="No class",
                                 start_at=ny_midnight - timedelta(hours=3), all_day=True, all_day_date=today + timedelta(days=2)))
    db.session.commit()
    ctx = myevents.context(db.session.get(User, synced_user.id), [])
    assert f"- {today + timedelta(days=2):%a %b %-d}, all day: No class" in ctx
