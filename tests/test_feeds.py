"""Calendar links (app/services/feeds.py): reading each LMS's iCal feed, turning it into classes, due
dates and events through the normal ingest, the SSRF guards, the routes and the hourly refresh.

Every calendar here is synthetic, written in the shape each LMS produces (see the comments in
feeds.py for the real feeds the shapes come from). No network: DNS and HTTP are faked.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from app.extensions import db
from app.models import (Assignment, CalendarEvent, CalendarFeed, CanvasAccount, Course, Integration, SyncRun, User,
                        utcnow)
from app.services import assessments, crypto, feeds

from .conftest import login, make_user
from .test_sources_google import composio  # noqa: F401 (the fake Google Calendar)

NY = ZoneInfo("America/New_York")
TOKEN = "s3cr3tT0kenDoNotLeak"


# ---------------------------------------------------------------- calendar builders


def z(dt: datetime) -> str:
    """Naive UTC -> iCal UTC."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


def at(days: float, hour: int | None = None) -> datetime:
    """Naive UTC, `days` from now (at a whole hour UTC if given)."""
    t = utcnow().replace(microsecond=0) + timedelta(days=days)
    return t.replace(hour=hour, minute=0, second=0) if hour is not None else t


def cal(*events: str, head: str = "") -> bytes:
    body = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + head + "".join(events) + "END:VCALENDAR\r\n"
    return body.encode()


def ev(uid: str, summary: str, start: str, end: str | None = None, **props) -> str:
    """One VEVENT. start/end are full property lines' values, e.g. '20261001T120000Z' or
    ';VALUE=DATE:20261001' (anything starting with ';' is used as parameters + value)."""
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}",
             f"DTSTART{start}" if start.startswith(";") else f"DTSTART:{start}"]
    if end:
        lines.append(f"DTEND{end}" if end.startswith(";") else f"DTEND:{end}")
    for key, value in props.items():
        lines.append(f"{key.replace('_', '-')}{value}" if str(value).startswith(";") else f"{key.replace('_', '-')}:{value}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def brightspace_ics() -> bytes:
    """Brightspace's shape: PRODID -//D2L//, course in LOCATION, ' - Due' / ' - Available' /
    ' - Availability Ends' suffixes, zero-length UTC due markers, type header + links in DESCRIPTION."""
    host = "learn.example.edu"
    view = lambda ou, n: f"View event - https://{host}/d2l/le/calendar/{ou}/event/{n}/detailsview?ou={ou}#{n}"  # noqa: E731
    quiz = lambda ou, qi: f"Quizzes:\\nQuiz 3 - https://{host}/d2l/lms/quizzing/quizzing.d2l?ou={ou}&qi={qi}"  # noqa: E731
    box = lambda ou, db_: f"Assignments:\\nLab - https://{host}/d2l/lms/dropbox/user/folder_submit_files.d2l?ou={ou}&db={db_}"  # noqa: E731
    chem, hist = "Fall 2026 CHEM 1010-001 LEC", "Fall 2026 HIST 2200-003 LEC"
    due, opens = at(3), at(1)
    return cal(
        ev("6606-1@x", "Quiz 3 - Due", z(due), z(due), LOCATION=chem, DESCRIPTION=quiz(111, 501) + "\\n\\n" + view(111, 1)),
        ev("6606-2@x", "Quiz 3 - Available", z(opens), z(opens), LOCATION=chem, DESCRIPTION=quiz(111, 501) + "\\n\\n" + view(111, 2)),
        ev("6606-3@x", "Lab Report 2 - Availability Ends", z(at(5)), z(at(5)), LOCATION=chem, DESCRIPTION=box(111, 77)),
        ev("6606-4@x", "Essay 1 - Due", z(at(6)), z(at(6)), LOCATION=hist, DESCRIPTION=box(222, 88) + "\\n\\n" + view(222, 4)),
        ev("6606-5@x", "Essay 1 - Availability Ends", z(at(8)), z(at(8)), LOCATION=hist, DESCRIPTION=box(222, 88)),
        ev("6606-6@x", "Midterm Exam - Due", z(at(10)), z(at(10)), LOCATION=hist),
        ev("6606-7@x", "Review Session ", z(at(4)), z(at(4.05)), LOCATION=f"Room 114 ({hist})",
           DESCRIPTION="Bring questions.\\n\\n" + view(222, 7)),
        ev("6606-8@x", "Fall Break", ";VALUE=DATE:" + at(12).strftime("%Y%m%d"), ";VALUE=DATE:" + at(14).strftime("%Y%m%d"),
           LOCATION="Example University"),
        ev("6606-9@x", "Old Homework - Due", z(at(-3)), z(at(-3)), LOCATION=chem),
        head="PRODID:-//D2L//NONSGML v1.0//EN\r\nMETHOD:PUBLISH\r\nX-WR-CALNAME:All Courses - Example University\r\n",
    )


def moodle_ics() -> bytes:
    """Moodle's shape: UID <id>@host, UTC times with DTSTART = DTEND, course short name in CATEGORIES,
    names like '<activity> is due' / 'opens' / 'closes', HTML-escaped names, TAB-folded lines."""
    t = lambda d: z(at(d))  # noqa: E731
    return cal(
        ev("11@moodle.example.edu", "Essay 1 is due", t(2), t(2), CATEGORIES="ENG101"),
        ev("12@moodle.example.edu", "Unit 4 Quiz opens", t(1), t(1), CATEGORIES="BIO110"),
        ev("13@moodle.example.edu", "Unit 4 Quiz closes", t(5), t(5), CATEGORIES="BIO110"),
        ev("14@moodle.example.edu", "Read chapter 2 should be completed", t(3), t(3), CATEGORIES="ENG101"),
        ev("15@moodle.example.edu", "Essay 1 is due to be graded", t(9), t(9), CATEGORIES="ENG101"),
        ev("16@moodle.example.edu", "Labs &amp; Reports is due", t(4), t(4), CATEGORIES="BIO110"),
        ev("17@moodle.example.edu", "Campus open day", t(6), t(6), CATEGORIES="Site events"),
        "BEGIN:VEVENT\r\nUID:18@moodle.example.edu\r\nSUMMARY:A rather long workshop name that Moodle folds over two\r\n\t"
        f" lines deadline for submissions\r\nDTSTART:{t(7)}\r\nDTEND:{t(7)}\r\nCATEGORIES:ENG101\r\nEND:VEVENT\r\n",
        head="PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024100700//EN\r\nMETHOD:PUBLISH\r\n",
    )


BB_TZ = ("BEGIN:VTIMEZONE\r\nTZID:America/Denver\r\nBEGIN:STANDARD\r\nDTSTART:19701101T020000\r\nTZOFFSETFROM:-0600\r\n"
         "TZOFFSETTO:-0700\r\nRRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\n"
         "DTSTART:19700308T020000\r\nTZOFFSETFROM:-0700\r\nTZOFFSETTO:-0600\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
         "END:DAYLIGHT\r\nEND:VTIMEZONE\r\n")


def blackboard_ics(stamp: str = "20260901T120000Z") -> bytes:
    """Blackboard's shape: PRODID -//Blackboard//EN, institution in X-WR-CALNAME, a VTIMEZONE and TZID
    times, titles as typed, no course; the UID says what an entry is (older ones start with a
    timestamp that changes on every download)."""
    local = lambda d: (at(d).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Denver"))  # noqa: E731
                       .strftime(";TZID=America/Denver:%Y%m%dT%H%M%S"))
    return cal(
        ev("_blackboard.platform.gradebook2.GradableItem-_2987747_1", "Module 4 - Quiz", local(3), local(3)),
        ev(f"{stamp}-_blackboard.platform.gradebook2.GradableItem-_2987748_1@app011.example", "Final Exam", local(20), local(20)),
        ev("_blackboard.data.calendar.CalendarEntry-_55_1", "Lab meeting", local(2), local(2.1), LOCATION="Room 5"),
        ev("_blackboard.data.discussionboard.Engagement-_6551398_1", "Discussion 2: Introductions", local(4), local(4)),
        ev("_blackboard.platform.gradebook2.GradableItem-_6551398_1", "Discussion 2: Introductions", local(8), local(8)),
        head="PRODID:-//Blackboard//EN\r\nCALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\nX-WR-CALNAME:Example College\r\n"
             "X-PUBLISHED-TTL:PT4H\r\n" + BB_TZ,
    )


def schoology_ics(host: str = "lms.district.example") -> bytes:
    """Schoology's shape: UID calendar-event-<id>@schoology.com, http:// URL saying what it is,
    DTSTART = due time and DTEND an hour later, all-day items as VALUE=DATE, no course."""
    due = at(4)
    day = at(6).strftime("%Y%m%d")
    return cal(
        ev("calendar-event-1@schoology.com", "Unit 2 Worksheet", z(due), z(due + timedelta(hours=1)),
           URL=f";VALUE=URI:http://{host}/assignment/6343975288", DESCRIPTION=f" - Link: http://{host}/assignment/6343975288"),
        ev("calendar-event-2@schoology.com", "Chapter 5 Test", ";VALUE=DATE:" + day, None,
           URL=f";VALUE=URI:http://{host}/assessment/777"),
        ev("calendar-event-3@schoology.com", "Back to School Night", z(at(5)), z(at(5.1)),
           URL=f";VALUE=URI:http://{host}/event/99/profile"),
    )


# ---------------------------------------------------------------- fake network


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None):
        self.status, self.body, self.headers = status, body, dict(headers or {})
        self.released = False

    def stream(self, amt, decode_content=True):
        for i in range(0, len(self.body), amt):
            yield self.body[i:i + amt]

    def release_conn(self):
        self.released = True


class Net:
    """DNS answers (host -> addresses; unknown hosts get a public address) and HTTP answers
    (url -> FakeResponse, or a function of the request headers)."""

    def __init__(self):
        self.dns: dict[str, list[str]] = {"localhost": ["127.0.0.1", "::1"]}  # as a real resolver answers
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []

    def serve(self, url: str, response):
        p = urlsplit(url)
        self.routes[(p.hostname, (p.path or "/") + (f"?{p.query}" if p.query else ""))] = response

    def getaddrinfo(self, host, port, *args, **kwargs):
        try:
            addresses = [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            addresses = self.dns.get(host, ["93.184.216.34"])
        if not addresses:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    def open(self, scheme, address, port, host, target, headers):
        self.calls.append({"scheme": scheme, "address": address, "port": port, "host": host, "target": target,
                           "headers": dict(headers)})
        answer = self.routes.get((host, target))
        if answer is None:
            return FakeResponse(404, b"not found")
        return answer(headers) if callable(answer) else answer


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(socket, "getaddrinfo", n.getaddrinfo)
    monkeypatch.setattr(feeds, "_open", n.open)
    return n


BS_URL = f"https://learn.example.edu/d2l/le/calendar/feed/user/feed.ics?token={TOKEN}"


def add_feed(client, url, follow=True):
    return client.post("/settings/feeds", data={"url": url}, follow_redirects=follow)


@pytest.fixture
def student(app, client):
    user = make_user()
    login(client, user)
    return user


def parse(body: bytes, tz=NY):
    return feeds.parse(body, tz, utcnow())


def snapshot_for(lms: str, body: bytes, host: str = "school.example"):
    feed = CalendarFeed(id=7, user_id=1, url=f"https://{host}/x.ics", url_hash="h", lms=lms, host=host)
    return feeds.build_snapshot(feed, parse(body), NY, utcnow())


def assignments(snapshot) -> dict[str, dict]:
    return {a["name"]: {**a, "course": c["name"]} for c in snapshot["courses"] for a in c["assignments"]}


def events(snapshot) -> dict[str, dict]:
    names = {c["id"]: c["name"] for c in snapshot["courses"]}
    return {e["title"]: {**e, "course": names.get(e["course_id"])} for e in snapshot["calendar_events"]}


# ---------------------------------------------------------------- parsing


def test_parse_folding_time_zones_floating_and_all_day():
    body = cal(
        "BEGIN:VEVENT\r\nUID:f1\r\nSUMMARY:Folded over\r\n  two lines\r\n\twith a tab\r\nDTSTART:20261015T160000Z\r\nEND:VEVENT\r\n",
        ev("t1", "Zoned", ";TZID=America/Chicago:20261015T100000"),
        ev("t2", "Custom zone", ";TZID=Denver Time:20261015T100000"),
        ev("fl", "Floating", "20261015T100000"),
        ev("ad", "All day", ";VALUE=DATE:20261016", ";VALUE=DATE:20261018"),
        ev("bad", "Broken date", "notadate"),
        head=BB_TZ.replace("America/Denver", "Denver Time") + "X-WR-TIMEZONE:America/Los_Angeles\r\n",
    )
    items = {i.uid: i for i in parse(body).items}
    assert items["f1"].summary == "Folded over two lineswith a tab"
    assert items["t1"].start == datetime(2026, 10, 15, 15, 0)  # CDT is UTC-5
    assert items["t2"].start == datetime(2026, 10, 15, 16, 0)  # the VTIMEZONE's MDT, UTC-6
    assert items["fl"].start == datetime(2026, 10, 15, 17, 0)  # floating: the calendar's X-WR-TIMEZONE (PDT)
    ad = items["ad"]
    assert ad.all_day and (ad.first_day.isoformat(), ad.last_day.isoformat()) == ("2026-10-16", "2026-10-17")
    assert ad.start == datetime(2026, 10, 16, 4, 0)  # local midnight in the student's zone (EDT)
    assert "bad" not in items
    # Without X-WR-TIMEZONE, floating times are the student's.
    plain = parse(cal(ev("fl", "Floating", "20261015T100000")))
    assert plain.items[0].start == datetime(2026, 10, 15, 14, 0)


def test_repeats_are_expanded_in_the_window_with_exceptions_and_a_cap():
    start = at(-30, hour=15)
    body = cal(
        ev("weekly", "Lab section", z(start), z(start + timedelta(hours=2)), RRULE="FREQ=WEEKLY",
           EXDATE=z(start + timedelta(weeks=6))),
        ev("weekly", "Lab moved", z(start + timedelta(weeks=7, hours=1)), RECURRENCE_ID=z(start + timedelta(weeks=7))),
        ev("weekly", "Lab cancelled", z(start + timedelta(weeks=8)), RECURRENCE_ID=z(start + timedelta(weeks=8)),
           STATUS="CANCELLED"),
        ev("counted", "Five times", z(at(1, hour=12)), RRULE="FREQ=DAILY;COUNT=5"),
        ev("until", "Until", ";VALUE=DATE:" + at(-1).strftime("%Y%m%d"),
           RRULE="FREQ=DAILY;UNTIL=" + at(2).strftime("%Y%m%d")),
    )
    parsed = parse(body)
    now = utcnow()
    labs = sorted(i.start for i in parsed.items if i.uid == "weekly")
    assert labs and min(labs) >= now - feeds.LOOKBACK - timedelta(days=1) and max(labs) <= now + feeds.LOOKAHEAD
    assert start + timedelta(weeks=6) not in labs, "EXDATE"
    assert start + timedelta(weeks=7, hours=1) in labs and start + timedelta(weeks=7) not in labs, "moved"
    assert start + timedelta(weeks=8) not in labs, "cancelled occurrence"
    assert {i.summary for i in parsed.items if i.uid == "weekly"} == {"Lab section", "Lab moved"}
    assert len({i.rid for i in parsed.items if i.uid == "weekly"}) == len(labs), "each occurrence its own id"
    assert len([i for i in parsed.items if i.uid == "counted"]) == 5
    assert len([i for i in parsed.items if i.uid == "until"]) == 4  # yesterday to two days ahead, inclusive

    many = cal(*(ev(f"d{n}", f"Daily {n}", z(at(-1, hour=9)), RRULE="FREQ=DAILY") for n in range(20)))
    parsed = parse(many)
    assert len(parsed.items) == feeds.MAX_OCCURRENCES and parsed.shape["occurrences_capped"]


def test_cancelled_events_are_skipped_and_big_calendars_are_capped():
    body = cal(ev("c1", "Cancelled class", z(at(2)), STATUS="CANCELLED"), ev("c2", "Kept", z(at(2))))
    assert [i.uid for i in parse(body).items] == ["c2"]
    big = cal(*(ev(f"e{n}", f"Event {n}", z(at(-400 + n * 0.2))) for n in range(feeds.MAX_EVENTS + 200)))
    parsed = parse(big)
    assert len(parsed.items) == feeds.MAX_EVENTS and parsed.shape["events_capped"]
    newest = max(i.start for i in parsed.items)
    assert newest > utcnow(), "what's coming up is kept, the oldest are dropped"


def test_unreadable_calendars_raise_a_friendly_error():
    for junk in (b"<html>Sign in</html>", b"", b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"):
        with pytest.raises(feeds.FeedError):
            parse(junk)


# ---------------------------------------------------------------- what each entry is, per LMS


def test_brightspace_suffixes_courses_and_quizzes():
    snap, counts = snapshot_for("brightspace", brightspace_ics(), "learn.example.edu")
    a, e = assignments(snap), events(snap)
    assert set(a) == {"Quiz 3", "Lab Report 2", "Essay 1", "Midterm Exam", "Old Homework"}
    assert a["Quiz 3"]["is_quiz"] and a["Quiz 3"]["submission_types"] == ["online_quiz"]
    assert a["Quiz 3"]["html_url"].startswith("https://learn.example.edu/d2l/lms/quizzing/")
    assert not a["Essay 1"]["is_quiz"] and a["Essay 1"]["submission_types"] == []
    assert a["Quiz 3"]["course"] == "Fall 2026 CHEM 1010-001 LEC" and a["Essay 1"]["course"] == "Fall 2026 HIST 2200-003 LEC"
    assert {c["course_code"] for c in snap["courses"]} == {"CHEM 1010-001", "HIST 2200-003"}
    # "Availability Ends" is the deadline only without a "Due": Lab Report 2 has none, Essay 1 has one.
    # Essay 1's stays on the calendar, outside the class (the planner sees the item once).
    assert "Essay 1 - Availability Ends" in e and e["Essay 1 - Availability Ends"]["course"] is None
    assert "Lab Report 2 - Availability Ends" not in e
    # The quiz's opening stays on the calendar, outside the class (the planner sees the quiz once).
    assert e["Quiz 3 - Available"]["course"] is None
    # An instructor's event with its own room: "<room> (<course>)"; an institution event: no class.
    assert e["Review Session"]["course"] == "Fall 2026 HIST 2200-003 LEC"
    assert e["Fall Break"]["course"] is None and e["Fall Break"]["all_day"]
    assert a["Old Homework"]["status"] == "past_due" and a["Quiz 3"]["status"] == "upcoming"
    assert all(x["status"] in ("upcoming", "past_due") and x["points_possible"] is None for x in a.values())
    assert counts == {"due": 5, "events": 4, "courses": 2, "fallback_course": False}
    due = datetime.fromisoformat(a["Quiz 3"]["due_at"][:-1])
    assert abs((due - at(3)).total_seconds()) < 2


def test_moodle_names_categories_and_escapes():
    snap, _ = snapshot_for("moodle", moodle_ics(), "moodle.example.edu")
    a, e = assignments(snap), events(snap)
    assert set(a) == {"Essay 1", "Unit 4 Quiz", "Read chapter 2", "Labs & Reports",
                      "A rather long workshop name that Moodle folds over two lines"}
    assert a["Unit 4 Quiz"]["is_quiz"] and not a["Essay 1"]["is_quiz"]
    assert a["Essay 1"]["course"] == "ENG101" and a["Unit 4 Quiz"]["course"] == "BIO110"
    assert {c["name"]: c["course_code"] for c in snap["courses"]} == {"BIO110": "BIO110", "ENG101": "ENG101"}
    assert e["Unit 4 Quiz opens"]["course"] is None, "the opening of a quiz that has a deadline"
    assert e["Essay 1 is due to be graded"]["course"] == "ENG101"
    assert e["Campus open day"]["course"] is None, "site events aren't a class"


def test_blackboard_uids_and_one_class_for_everything():
    snap, counts = snapshot_for("blackboard", blackboard_ics(), "bb.example.edu")
    a, e = assignments(snap), events(snap)
    assert set(a) == {"Module 4 - Quiz", "Final Exam", "Discussion 2: Introductions"}
    assert a["Module 4 - Quiz"]["is_quiz"] and not a["Final Exam"]["is_quiz"]
    assert set(e) == {"Lab meeting", "Discussion 2: Introductions"}
    assert {c["name"] for c in snap["courses"]} == {"Example College"}  # the calendar's name
    assert counts["fallback_course"]
    due = datetime.fromisoformat(a["Final Exam"]["due_at"][:-1])
    assert abs((due - at(20)).total_seconds()) < 2, "TZID times convert to UTC"
    # The old-style UID's timestamp changes with every download; the id doesn't.
    again, _ = snapshot_for("blackboard", blackboard_ics(stamp="20261003T090000Z"), "bb.example.edu")
    assert assignments(again)["Final Exam"]["id"] == a["Final Exam"]["id"]


def test_schoology_urls_due_times_and_all_day_items():
    snap, _ = snapshot_for("schoology", schoology_ics(), "lms.district.example")
    a, e = assignments(snap), events(snap)
    assert set(a) == {"Unit 2 Worksheet", "Chapter 5 Test"} and set(e) == {"Back to School Night"}
    worksheet = a["Unit 2 Worksheet"]
    assert abs((datetime.fromisoformat(worksheet["due_at"][:-1]) - at(4)).total_seconds()) < 2, "DTSTART, not DTEND"
    assert worksheet["html_url"] == "https://lms.district.example/assignment/6343975288"
    test = a["Chapter 5 Test"]
    assert test["is_quiz"]  # an /assessment/ item
    local = datetime.fromisoformat(test["due_at"][:-1]).replace(tzinfo=timezone.utc).astimezone(NY)
    assert (local.date(), local.hour, local.minute) == (at(6).date(), 23, 59), "all day: 23:59 in the student's zone"
    assert {c["name"] for c in snap["courses"]} == {"Calendar (lms.district.example)"}


def test_other_calendars_use_only_clear_markers():
    body = cal(ev("o1", "Problem Set 4 - Due", z(at(2)), CATEGORIES="PHYS 101"),
               ev("o2", "Due: Lab writeup", z(at(3)), CATEGORIES="PHYS 101"),
               ev("o3", "Library closes", z(at(1))),
               head="X-WR-CALNAME:My classes\r\n")
    snap, _ = snapshot_for("other", body)
    assert set(assignments(snap)) == {"Problem Set 4", "Lab writeup"}
    assert events(snap)["Library closes"]["course"] is None


def test_lms_detection_from_links_and_calendars():
    assert feeds.detect_lms(BS_URL) == "brightspace"
    assert feeds.detect_lms("https://bb.example.edu/webapps/calendar/calendarFeed/abc123/learn.ics") == "blackboard"
    assert feeds.detect_lms("https://m.example.edu/calendar/export_execute.php?userid=1&authtoken=x") == "moodle"
    assert feeds.detect_lms("https://lms.district.example/calendar/feed/ical/1675537677/abc/ical.ics") == "schoology"
    assert feeds.detect_lms("https://calendar.example.com/x.ics") == "other"
    assert feeds.lms_from_calendar("-//D2L//NONSGML v1.0//EN", []) == "brightspace"
    assert feeds.lms_from_calendar("", ["calendar-event-1@schoology.com"]) == "schoology"


# ---------------------------------------------------------------- the link


def test_normalize_accepts_https_and_webcal_and_rejects_the_rest(app):
    assert feeds.normalize(f"  webcal://Learn.Example.EDU/d2l/le/calendar/feed/user/feed.ics?token={TOKEN}#x \n") == \
        f"https://learn.example.edu/d2l/le/calendar/feed/user/feed.ics?token={TOKEN}"
    assert feeds.normalize("https://a.example:443/x.ics") == "https://a.example/x.ics"
    assert feeds.normalize("https://[2606:4700::1111]/x.ics") == "https://[2606:4700::1111]/x.ics"
    assert feeds.normalize("https://m.example.edu/calendar/export_execute.php?userid=1&authtoken=t&preset_what=all"
                           "&preset_time=weeknow").endswith("&preset_time=recentupcoming")
    for bad in ("file:///etc/passwd", "ftp://example.com/x.ics", "javascript:alert(1)", "https://user:pw@example.com/x.ics",
                "https://a@example.com/x.ics", "https://example.com:8443/x.ics", "example.com/x.ics", "",
                "https://exa mple.com/x.ics", "https://example.com/" + "a" * 2000):
        with pytest.raises(feeds.FeedError):
            feeds.normalize(bad)
    assert feeds.normalize("http://example.com/x.ics") == "http://example.com/x.ics"  # tests/development only
    app.config["ENV_NAME"] = "production"
    with pytest.raises(feeds.FeedError):
        feeds.normalize("http://example.com/x.ics")


# ---------------------------------------------------------------- fetching (SSRF)


@pytest.mark.parametrize("host_or_ip", ["localhost", "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1",
                                        "169.254.169.254", "[::1]", "[fd00::1]", "[fe80::1]", "[::ffff:127.0.0.1]",
                                        "0.0.0.0", "100.64.0.1", "224.0.0.1", "[64:ff9b::a00:1]"])
def test_private_addresses_are_refused(app, net, host_or_ip):
    with pytest.raises(feeds.FeedError, match="private network"):
        feeds.fetch_url(f"https://{host_or_ip}/cal.ics")
    assert net.calls == [], "nothing was requested"


def test_every_resolved_address_must_be_public(app, net):
    net.dns["rebind.example"] = ["93.184.216.34", "10.1.2.3"]
    with pytest.raises(feeds.FeedError, match="private network"):
        feeds.fetch_url("https://rebind.example/cal.ics")
    net.dns["gone.example"] = []
    with pytest.raises(feeds.FeedError, match="couldn't find"):
        feeds.fetch_url("https://gone.example/cal.ics")


def test_the_connection_goes_to_the_checked_address(app, net):
    net.dns["cal.example"] = ["93.184.216.34"]
    net.serve("https://cal.example/a.ics?token=1", FakeResponse(200, cal(), {"ETag": '"v1"'}))
    got = feeds.fetch_url("https://cal.example/a.ics?token=1", etag='"v0"')
    assert got.status == 200 and got.etag == '"v1"'
    call = net.calls[0]
    assert (call["address"], call["host"], call["port"], call["target"]) == ("93.184.216.34", "cal.example", 443, "/a.ics?token=1")
    assert call["headers"]["User-Agent"].startswith("HomeworkHatch-CalendarLink/1.0")
    assert call["headers"]["If-None-Match"] == '"v0"'


def test_redirects_are_followed_by_hand_and_rechecked(app, net):
    net.serve("https://a.example/1.ics", FakeResponse(302, headers={"Location": "https://b.example/2.ics"}))
    net.serve("https://b.example/2.ics", FakeResponse(301, headers={"Location": "/3.ics"}))
    net.serve("https://b.example/3.ics", FakeResponse(200, cal()))
    assert feeds.fetch_url("https://a.example/1.ics").status == 200
    assert [c["host"] + c["target"] for c in net.calls] == ["a.example/1.ics", "b.example/2.ics", "b.example/3.ics"]

    net.serve("https://a.example/evil.ics", FakeResponse(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}))
    with pytest.raises(feeds.FeedError, match="private network"):
        feeds.fetch_url("https://a.example/evil.ics")
    net.dns["internal.example"] = ["10.0.0.7"]
    net.serve("https://a.example/evil2.ics", FakeResponse(307, headers={"Location": "https://internal.example/x"}))
    with pytest.raises(feeds.FeedError, match="private network"):
        feeds.fetch_url("https://a.example/evil2.ics")
    net.serve("https://a.example/file.ics", FakeResponse(302, headers={"Location": "file:///etc/passwd"}))
    with pytest.raises(feeds.FeedError):
        feeds.fetch_url("https://a.example/file.ics")
    for n in range(5):
        net.serve(f"https://a.example/loop{n}", FakeResponse(302, headers={"Location": f"/loop{n + 1}"}))
    with pytest.raises(feeds.FeedError, match="too many"):
        feeds.fetch_url("https://a.example/loop0")
    app.config["ENV_NAME"] = "production"  # no plain http outside development
    net.serve("https://a.example/down.ics", FakeResponse(302, headers={"Location": "http://a.example/cal.ics"}))
    with pytest.raises(feeds.FeedError):
        feeds.fetch_url("https://a.example/down.ics")


def test_body_size_content_and_status_checks(app, net):
    huge = b"BEGIN:VCALENDAR\r\n" + b"X" * (feeds.MAX_BYTES + 10)
    net.serve("https://a.example/huge.ics", FakeResponse(200, huge))
    with pytest.raises(feeds.FeedError, match="5 MB"):
        feeds.fetch_url("https://a.example/huge.ics")
    net.serve("https://a.example/said-huge.ics", FakeResponse(200, cal(), {"Content-Length": str(feeds.MAX_BYTES + 1)}))
    with pytest.raises(feeds.FeedError, match="5 MB"):
        feeds.fetch_url("https://a.example/said-huge.ics")
    net.serve("https://a.example/login.ics", FakeResponse(200, b"<!DOCTYPE html><html>Sign in</html>"))
    with pytest.raises(feeds.FeedError, match="web page"):
        feeds.fetch_url("https://a.example/login.ics")
    net.serve("https://a.example/pdf.ics", FakeResponse(200, b"%PDF-1.7 ..."))
    with pytest.raises(feeds.FeedError, match="didn't return a calendar"):
        feeds.fetch_url("https://a.example/pdf.ics")
    net.serve("https://m.example/calendar/export_execute.php?x=1", FakeResponse(200, b"Invalid authentication"))
    with pytest.raises(feeds.FeedError, match="Moodle says"):
        feeds.fetch_url("https://m.example/calendar/export_execute.php?x=1", lms="moodle")
    with pytest.raises(feeds.FeedError, match=r"stopped working \(404\)\. Copy a fresh link from Brightspace"):
        feeds.fetch_url("https://a.example/missing.ics", lms="brightspace")
    net.serve("https://a.example/same.ics", FakeResponse(304))
    assert feeds.fetch_url("https://a.example/same.ics", etag='"x"').status == 304


# ---------------------------------------------------------------- snapshot -> ingest


def test_a_link_becomes_classes_due_dates_and_events(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics(), {"ETag": '"one"'}))
    page = add_feed(client, BS_URL).get_data(as_text=True)
    assert "Added your Brightspace calendar link: found 5 due dates and 4 events in 2 classes" in page
    feed = db.session.scalar(select(CalendarFeed))
    account = db.session.get(CanvasAccount, feed.account_id)
    assert account.lms == "ics" and account.canvas_user_id == f"feed-{feed.id}" and account.host == "learn.example.edu"
    assert feed.lms == "brightspace" and feed.event_count == 9 and feed.etag == '"one"' and feed.last_error is None
    courses = {c.name: c for c in db.session.scalars(select(Course).where(Course.account_id == account.id))}
    assert set(courses) == {"Fall 2026 CHEM 1010-001 LEC", "Fall 2026 HIST 2200-003 LEC"}
    names = {a.name for a in db.session.scalars(select(Assignment))}
    assert names == {"Quiz 3", "Lab Report 2", "Essay 1", "Midterm Exam", "Old Homework"}
    assert db.session.scalar(select(func.count(CalendarEvent.id))) == 4
    run = db.session.scalar(select(SyncRun).where(SyncRun.account_id == account.id))
    shape = run.stats["shape"]
    assert shape["vevents"] == 9 and shape["props"]["LOCATION"] == 9 and shape["due"] == 5 and shape["courses"] == 2
    assert "Quiz" not in json.dumps(shape) and TOKEN not in json.dumps(shape), "shape only, no text"

    # The dashboard says where it came from, and doesn't ask about files for these classes.
    home = client.get("/dashboard").get_data(as_text=True)
    assert "from your Brightspace calendar link" in home and "Quiz 3" in home
    assert "Pick which classes" not in home


def test_refetch_deletes_what_disappeared_and_unchanged_is_a_no_op(app, client, student, net):
    body = moodle_ics()
    net.serve("https://moodle.example.edu/calendar/export_execute.php?userid=1&authtoken=t", lambda h: FakeResponse(200, body))
    add_feed(client, "https://moodle.example.edu/calendar/export_execute.php?userid=1&authtoken=t")
    feed = db.session.scalar(select(CalendarFeed))
    essay = db.session.scalar(select(Assignment).where(Assignment.name == "Essay 1"))
    runs = db.session.scalar(select(func.count(SyncRun.id)))

    assert feeds.refresh(feed, force=True)["unchanged"] is True  # same calendar: nothing rewritten
    assert db.session.scalar(select(func.count(SyncRun.id))) == runs + 1
    assert db.session.get(Assignment, essay.id) is not None

    assert db.session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Campus open day")) is not None
    body = body.replace(b"BEGIN:VEVENT\r\nUID:11@", b"BEGIN:VEVENT\r\nUID:99@").replace(b"Essay 1 is due\r\n", b"Essay 2 is due\r\n")
    body = b"BEGIN:VEVENT".join(part for part in body.split(b"BEGIN:VEVENT") if b"Campus open day" not in part)
    counts = feeds.refresh(feed, force=True)
    assert counts["unchanged"] is False
    db.session.expire_all()
    names = {a.name for a in db.session.scalars(select(Assignment))}
    assert "Essay 1" not in names and "Essay 2" in names
    assert db.session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Campus open day")) is None, \
        "an event gone from the calendar is deleted"


def test_not_modified_keeps_rows_and_moves_passed_due_dates(app, client, student, net):
    hits = []

    def answer(headers):
        hits.append(headers.get("If-None-Match"))
        return FakeResponse(304) if headers.get("If-None-Match") == '"v1"' else FakeResponse(200, brightspace_ics(), {"ETag": '"v1"'})

    net.serve(BS_URL, answer)
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    quiz = db.session.scalar(select(Assignment).where(Assignment.name == "Quiz 3"))
    quiz.due_at = utcnow() - timedelta(minutes=5)  # time passes
    feed.last_fetched_at = utcnow() - timedelta(hours=2)
    db.session.commit()
    runs = db.session.scalar(select(func.count(SyncRun.id)))
    assert feeds.refresh(feed)["unchanged"] is True
    assert hits == [None, '"v1"']
    assert db.session.scalar(select(func.count(SyncRun.id))) == runs, "304: no import"
    db.session.expire_all()
    assert db.session.get(Assignment, quiz.id).status == "past_due"


def test_the_exam_planner_finds_a_midterm_from_a_link(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    found = {f.title: f for f in assessments.find(db.session.get(User, student.id))}
    assert "Midterm Exam" in found and found["Midterm Exam"].kind == "midterm"
    assert "Quiz 3" in found and found["Quiz 3"].kind == "quiz"
    assert "Quiz 3 - Available" not in found, "the opening isn't a second quiz"
    page = client.get("/study/exams/").get_data(as_text=True)
    assert "Found in Brightspace" in page and "Found in Canvas" not in page
    assert "Due dates and Brightspace events" in client.get("/calendar").get_data(as_text=True)


def test_google_calendar_and_ics_export_use_the_feed_due_dates(app, client, student, net, composio):
    composio.connect(student, "googlecalendar")
    db.session.add(Integration(user_id=student.id, kind="calendar", connected=True, settings={"enabled": True}))
    db.session.commit()
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    created = {a["summary"]: a for s, a in composio.calls if s == "GOOGLECALENDAR_CREATE_EVENT"}
    assert set(created) == {"Due: Quiz 3 · CHEM 1010-001", "Due: Lab Report 2 · CHEM 1010-001",
                            "Due: Essay 1 · HIST 2200-003", "Due: Midterm Exam · HIST 2200-003"}, "upcoming only"
    assert "Open in Brightspace" in created["Due: Quiz 3 · CHEM 1010-001"]["description"]
    user = db.session.get(User, student.id)
    ics = client.get(f"/calendar/{user.calendar_token}.ics").get_data(as_text=True)
    assert "Due: Quiz 3 (Fall 2026 CHEM 1010-001 LEC)" in ics

    # Removing the link takes its due dates off Google Calendar too.
    client.post(f"/settings/feeds/{db.session.scalar(select(CalendarFeed.id))}/remove")
    assert composio.events == {}
    assert "Quiz 3" not in client.get(f"/calendar/{user.calendar_token}.ics").get_data(as_text=True)


# ---------------------------------------------------------------- routes


def test_the_link_is_encrypted_and_never_shown(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    raw = db.session.execute(text("SELECT url FROM calendar_feed")).scalar()
    assert crypto.Keyring.is_field_ciphertext(raw) and TOKEN not in raw
    feed = db.session.scalar(select(CalendarFeed))
    assert feed.url == BS_URL and feed.url_hash == feeds.url_hash(BS_URL)
    db.session.get(User, student.id).is_admin = True  # the admin's view of links: shape only
    db.session.commit()
    for page in ("/settings/sync", "/dashboard", "/calendar", "/settings/data/export", "/admin/"):
        body = client.get(page).get_data(as_text=True)
        assert TOKEN not in body and "feed.ics" not in body, page
    admin = client.get("/admin/").get_data(as_text=True)
    assert "Calendar links" in admin and "5 due · 4 events · 2 classes" in admin and "Quiz 3" not in admin
    sync_page = client.get("/settings/sync").get_data(as_text=True)
    assert "learn.example.edu" in sync_page and "Brightspace" in sync_page
    export = json.loads(client.get("/settings/data/export").data)
    assert export["calendar_links"] == [{"lms": "Brightspace", "host": "learn.example.edu",
                                         "created_at": feed.created_at.isoformat(),
                                         "last_fetched_at": feed.last_fetched_at.isoformat()}]


def test_a_link_that_fails_is_not_kept(app, client, student, net):
    page = add_feed(client, BS_URL).get_data(as_text=True)  # the fake answers 404
    assert "The link stopped working (404). Copy a fresh link from Brightspace." in page
    assert "wasn&#39;t added" in page or "wasn't added" in page
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 0
    assert db.session.scalar(select(func.count(CanvasAccount.id))) == 0
    page = add_feed(client, "http://localhost/cal.ics").get_data(as_text=True)
    assert "private network" in page and db.session.scalar(select(func.count(CalendarFeed.id))) == 0
    page = add_feed(client, "ftp://example.com/cal.ics").get_data(as_text=True)
    assert "webcal://" in page


def test_five_links_at_most_and_no_duplicates(app, client, student, net):
    for n in range(5):
        url = f"https://cal{n}.example/feed.ics?token=t{n}"
        net.serve(url, FakeResponse(200, cal(ev(f"u{n}", f"Thing {n} - Due", z(at(2))))))
        add_feed(client, url)
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 5
    page = add_feed(client, "https://cal0.example/feed.ics?token=t0").get_data(as_text=True)
    assert "up to 5 calendar links" in page
    db.session.delete(db.session.scalar(select(CalendarFeed).where(CalendarFeed.host == "cal4.example")))
    db.session.commit()
    page = add_feed(client, "webcal://cal0.example/feed.ics?token=t0").get_data(as_text=True)
    assert "already added" in page
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 4


def test_refresh_and_remove_are_owner_only_and_remove_deletes_everything(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    account_id = feed.account_id
    other = make_user("riley")
    client.post("/logout")
    login(client, other)
    assert client.post(f"/settings/feeds/{feed.id}/refresh").status_code == 404
    assert client.post(f"/settings/feeds/{feed.id}/remove").status_code == 404
    assert db.session.get(CalendarFeed, feed.id) is not None
    client.post("/logout")
    login(client, student)

    calls = len(net.calls)
    page = client.post(f"/settings/feeds/{feed.id}/refresh", follow_redirects=True).get_data(as_text=True)
    assert "checked a moment ago" in page and len(net.calls) == calls, "Refresh now can't hammer the site"

    def checked_a_while_ago():
        db.session.get(CalendarFeed, feed.id).last_fetched_at = utcnow() - timedelta(minutes=2)
        db.session.commit()

    checked_a_while_ago()
    page = client.post(f"/settings/feeds/{feed.id}/refresh", follow_redirects=True).get_data(as_text=True)
    assert "Up to date: 5 due dates and 4 events in 2 classes" in page
    net.serve(BS_URL, FakeResponse(403))
    checked_a_while_ago()
    page = client.post(f"/settings/feeds/{feed.id}/refresh", follow_redirects=True).get_data(as_text=True)
    assert "Brightspace refused the link (403)" in page
    db.session.expire_all()
    assert db.session.get(CalendarFeed, feed.id).last_error.startswith("Brightspace refused the link")
    assert db.session.scalar(select(func.count(Assignment.id))) == 5, "a failed refresh keeps what was there"

    client.post(f"/settings/feeds/{feed.id}/remove")
    db.session.expire_all()
    assert db.session.get(CalendarFeed, feed.id) is None and db.session.get(CanvasAccount, account_id) is None
    for model in (Course, Assignment, CalendarEvent, SyncRun):
        assert db.session.scalar(select(func.count(model.id))) == 0, model.__name__


def test_deleting_the_account_deletes_its_links(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 1
    client.post("/settings/data/delete", data={"confirm": "sam", "password": "password123"})
    db.session.expire_all()
    assert db.session.get(User, student.id) is None
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 0
    assert db.session.scalar(select(func.count(CanvasAccount.id))) == 0


# ---------------------------------------------------------------- the hourly refresh


def test_only_stale_links_are_refetched_from_pages(app, client, student, net):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    calls = len(net.calls)
    for page in ("/dashboard", "/calendar", "/study/exams/", "/settings/sync"):
        assert client.get(page).status_code == 200
    assert len(net.calls) == calls, "fetched minutes ago: not again"

    feed = db.session.scalar(select(CalendarFeed))
    feed.last_fetched_at = utcnow() - timedelta(minutes=61)
    db.session.commit()
    assert client.get("/dashboard").status_code == 200
    assert len(net.calls) == calls + 1
    db.session.expire_all()
    assert utcnow() - db.session.get(CalendarFeed, feed.id).last_fetched_at < timedelta(minutes=1)


def test_a_broken_link_never_breaks_a_page(app, client, student, net, monkeypatch):
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    feed.last_fetched_at = utcnow() - timedelta(hours=3)
    db.session.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(feeds, "parse", explode)
    assert client.get("/dashboard").status_code == 200
    db.session.expire_all()
    feed = db.session.get(CalendarFeed, feed.id)
    assert feed.last_error == "Something went wrong reading this calendar. We'll try again later."
    assert db.session.scalar(select(func.count(Assignment.id))) == 5
    assert client.get("/dashboard").status_code == 200  # just checked: not retried on every page


def test_past_items_from_a_calendar_link_drop_off_instead_of_piling_up(app):
    from datetime import timedelta

    from app import queries
    from app.models import Assignment, CanvasAccount, Course, utcnow

    user = make_user("ics_overdue", "ics_overdue@example.com")
    account = CanvasAccount(user_id=user.id, host="school.brightspace.example", base_url="https://school.brightspace.example",
                            canvas_user_id="feed-1", lms="ics")
    db.session.add(account)
    db.session.flush()
    course = Course(user_id=user.id, account_id=account.id, canvas_id="c1", name="Biology", class_key="ics::biology",
                    room_key="school.brightspace.example:c1")
    db.session.add(course)
    db.session.flush()
    db.session.add_all([
        Assignment(course_id=course.id, canvas_id="a1", name="Lab report", due_at=utcnow() - timedelta(days=2), status="past_due"),
        Assignment(course_id=course.id, canvas_id="a2", name="Quiz 3", due_at=utcnow() + timedelta(days=2), status="upcoming"),
    ])
    db.session.commit()
    assert [a.name for a in queries.upcoming(user.id)] == ["Quiz 3"]


def test_calendar_link_classes_never_share_chat_rooms_or_identities(app, client):
    from app.models import CanvasAccount, Course
    from app.services import ingest

    rooms = []
    for name in ("ana", "ben"):
        user = make_user(name, f"{name}@example.com")
        snap = {"schema_version": 1, "base_url": "https://lms.district.example", "user": {"id": f"feed-{user.id}"},
                "courses": [{"id": "abc123", "name": "Calendar (lms.district.example)", "assignments": [],
                             "assignment_groups": []}], "calendar_events": []}
        ingest.ingest_snapshot(user, snap, [])
        db.session.commit()
        rooms.append(db.session.scalar(select(Course.room_key).where(Course.user_id == user.id)))
    assert rooms[0] != rooms[1] and all(r.startswith("ics/") for r in rooms), rooms
    # Nobody can claim a calendar link's identity through the extension API.
    from .conftest import api_token

    mallory = make_user("mallory", "mallory@example.com")
    r = client.post("/v1/snapshots", headers={"Authorization": f"Bearer {api_token(mallory)}"},
                    json={"snapshot": {"schema_version": 1, "base_url": "https://lms.district.example",
                                       "user": {"id": "feed-99"}, "courses": []}, "files": []})
    assert r.status_code == 400
    assert not db.session.scalar(select(CanvasAccount).where(CanvasAccount.canvas_user_id == "feed-99"))


def test_dashboard_pill_follows_canvas_even_with_a_fresher_calendar_link(app, client, snapshot, manifest):
    from app.models import CanvasAccount, utcnow
    from app.services import ingest

    from .conftest import api_token, sync

    user = make_user("both", "both@example.com")
    sync(client, api_token(user), snapshot, manifest)
    canvas = db.session.scalar(select(CanvasAccount).where(CanvasAccount.user_id == user.id))
    canvas.last_sync_at = utcnow() - timedelta(days=5)
    ingest.ingest_snapshot(user, {"schema_version": 1, "base_url": "https://school.brightspace.example",
                                  "user": {"id": f"feed-{user.id}"}, "courses": [], "calendar_events": []}, [])
    feed_account = db.session.scalar(select(CanvasAccount).where(CanvasAccount.canvas_user_id == f"feed-{user.id}"))
    feed_account.lms = "ics"
    db.session.commit()
    login(client, user)
    page = client.get("/dashboard").get_data(as_text=True)
    assert "sync-pill stale" in page and "5 days" in page


# ---------------------------------------------------------------- real sockets: the deadline and urllib3's logs


D2L = "PRODID:-//D2L//NONSGML v1.0//EN\r\nX-WR-CALNAME:All Courses - Example University\r\n"
HTTP_URL = f"http://learn.example.edu/d2l/le/calendar/feed/user/feed.ics?token={TOKEN}"  # plain http: tests only


@pytest.fixture
def local_server(monkeypatch):
    """A real server on 127.0.0.1, for what the fake network can't show (socket timeouts, urllib3's own
    logging). start(script, tls=None) serves one connection with script(conn, stop); every link, whatever
    its host, reaches it (the address check and the port are bypassed; nothing else is)."""
    servers = []
    real_open = feeds._open

    def start(script, tls: ssl.SSLContext | None = None) -> int:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        stop = threading.Event()

        def run():
            try:
                conn, _ = srv.accept()
                conn.settimeout(15)
                if tls is not None:
                    conn = tls.wrap_socket(conn, server_side=True)
                with conn:
                    conn.recv(65536)
                    script(conn, stop)
            except OSError:
                pass

        threading.Thread(target=run, daemon=True).start()
        servers.append((srv, stop))
        port = srv.getsockname()[1]
        monkeypatch.setattr(feeds, "_safe_address", lambda host, _port: "127.0.0.1")
        monkeypatch.setattr(feeds, "_open", lambda scheme, address, _port, host, target, headers:
                            real_open(scheme, address, port, host, target, headers))
        return port

    yield start
    for srv, stop in servers:
        stop.set()
        srv.close()


def trickle(head: bytes, data: bytes, gap: float = 0.2):
    """Send `head`, then `data` one byte per `gap` seconds: each read gets something well within its timeout."""
    def script(conn, stop):
        if head:
            conn.sendall(head)
        for i in range(len(data)):
            if stop.wait(gap):
                return
            conn.sendall(data[i:i + 1])
    return script


def tls_context(tmp_path, monkeypatch) -> ssl.SSLContext:
    """A server certificate for learn.example.edu that the fetch trusts (as certifi's bundle)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "learn.example.edu")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("learn.example.edu")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(cert_path))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@pytest.mark.parametrize("mode", ["headers", "body", "body without a length", "headers over tls"])
def test_a_server_that_trickles_bytes_is_cut_off_at_the_deadline(app, local_server, monkeypatch, tmp_path, mode):
    """Review 3, security#0: the deadline used to be checked only between 64 KiB chunks, so a byte every
    few seconds (headers or body) held a request thread for days. Now one wall-clock limit covers
    every phase. Over TLS the socket timer must work on a duplicate: TLS detaches the original."""
    monkeypatch.setattr(feeds, "TOTAL_SECONDS", 1.0)
    monkeypatch.setattr(feeds, "READ_TIMEOUT", 5)
    url = HTTP_URL
    if mode == "headers":
        local_server(trickle(b"", b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 400))
    elif mode == "body":
        local_server(trickle(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\nBEGIN:VCALENDAR\r\n", b"X" * 400))
    elif mode == "body without a length":  # cut off, it would look complete: it mustn't be imported
        local_server(trickle(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + cal(ev("a", "A - Due", z(at(2)))),
                             b"X" * 400))
    else:
        local_server(trickle(b"", b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 400), tls=tls_context(tmp_path, monkeypatch))
        url = BS_URL
    started = time.monotonic()
    with pytest.raises(feeds.FeedError, match="took too long") as raised:
        feeds.fetch_url(url)
    assert time.monotonic() - started < 3, "stopped at the deadline, not after READ_TIMEOUT per byte"
    assert raised.value.transient


def test_adding_a_link_to_a_slow_server_frees_the_request_at_the_deadline(app, client, student, local_server,
                                                                          monkeypatch):
    monkeypatch.setattr(feeds, "TOTAL_SECONDS", 1.0)
    local_server(trickle(b"", b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 400))
    started = time.monotonic()
    page = html.unescape(add_feed(client, HTTP_URL).get_data(as_text=True))
    assert time.monotonic() - started < 3
    assert "The calendar took too long to download. Try adding it again in a few minutes. The link wasn't added." in page
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 0


def test_a_slow_name_server_counts_against_the_deadline(app, monkeypatch):
    release = threading.Event()

    def slow_lookup(*args, **kwargs):
        release.wait(5)
        raise socket.gaierror("too late anyway")

    monkeypatch.setattr(feeds, "TOTAL_SECONDS", 0.5)
    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    started = time.monotonic()
    try:
        with pytest.raises(feeds.FeedError, match="took too long"):
            feeds.fetch_url("https://slow-dns.example/cal.ics")
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_fetches_past_the_limit_are_turned_away_not_queued(app, client, student, net):
    """Review 3, security#0: at most MAX_CONCURRENT_FETCHES run at once in a process; one more is
    refused at once (no thread waits), with a friendly message, and a background refresh that found no
    slot is due again right away."""
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    calls = len(net.calls)
    held = 0
    try:
        while feeds._fetch_slots.acquire(blocking=False):  # every slot busy with slow sites
            held += 1
        assert held == feeds.MAX_CONCURRENT_FETCHES
        started = time.monotonic()
        with pytest.raises(feeds.FeedBusy):
            feeds.fetch_url(BS_URL)
        assert time.monotonic() - started < 0.5, "refused, not queued"

        url = "https://cal.example/other.ics?token=x"
        page = html.unescape(add_feed(client, url).get_data(as_text=True))
        assert ("Lots of calendar links are being read right now. Try adding it again in a minute. "
                "The link wasn't added.") in page
        assert db.session.scalar(select(func.count(CalendarFeed.id))) == 1

        stale = utcnow() - timedelta(hours=2)
        feed.last_fetched_at = stale
        db.session.commit()
        assert client.get("/dashboard").status_code == 200  # the page-view refresh finds no slot
        db.session.expire_all()
        feed = db.session.get(CalendarFeed, feed.id)
        assert feed.last_fetched_at == stale and feed.last_error is None, "nothing was tried: still due"
        page = html.unescape(client.post(f"/settings/feeds/{feed.id}/refresh", follow_redirects=True)
                             .get_data(as_text=True))
        assert "Lots of calendar links are being read right now. Try again in a minute." in page
        assert len(net.calls) == calls
    finally:
        for _ in range(held):
            feeds._fetch_slots.release()
    assert client.get("/dashboard").status_code == 200
    assert len(net.calls) == calls + 1, "with a slot free, the stale link is read on the next page view"


def test_urllib3_never_writes_the_link_to_the_logs(app, local_server, caplog):
    """Review 3, security#2: on a malformed header line urllib3 logs 'Failed to parse headers (url=...)'
    with the whole link, and its DEBUG request line has the path and query. While a link is fetched,
    none of urllib3's lines get through; elsewhere its URLs lose their path and query."""
    body = cal(ev("a", "A - Due", z(at(2))))
    local_server(lambda conn, stop: conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/calendar\r\nX-Broken header line\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body))
    caplog.set_level(logging.DEBUG)
    got = feeds.fetch_url(HTTP_URL)
    assert got.status == 200 and got.body == body
    assert TOKEN not in caplog.text and "feed.ics" not in caplog.text
    assert "learn.example.edu" in caplog.text, "our own log line (host only) is still there"

    logging.getLogger("urllib3.connection").warning("Failed to parse headers (url=%s): %s", BS_URL, "x")
    logging.getLogger("urllib3.connectionpool").debug('%s://%s:%s "%s %s %s" %s %s', "https", "learn.example.edu",
                                                      443, "GET", "/d2l/feed.ics?token=" + TOKEN, "HTTP/1.1", 200, 0)
    assert TOKEN not in caplog.text and "Failed to parse headers (url=https://learn.example.edu/" in caplog.text


# ---------------------------------------------------------------- review 3: remove vs refresh, our own calendar


def test_removing_a_link_while_it_is_being_read_leaves_nothing_behind(app, client, student, net, monkeypatch):
    """Review 3, correctness#1: Remove pressed while a background refresh was fetching left the import
    behind as a 'Canvas' account nothing could remove."""
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    feed_id, account_id = feed.id, feed.account_id
    real_fetch = feeds.fetch

    def removed_meanwhile(f, etag=None):
        got = real_fetch(f, None)
        with db.engine.begin() as conn:  # the Remove button, in another request
            conn.execute(text("DELETE FROM canvas_account WHERE id = :id"), {"id": account_id})
            conn.execute(text("DELETE FROM calendar_feed WHERE id = :id"), {"id": feed_id})
        return got

    monkeypatch.setattr(feeds, "fetch", removed_meanwhile)
    assert feeds.refresh(feed, force=True) is None
    db.session.expire_all()
    for model in (CalendarFeed, CanvasAccount, Course, Assignment, CalendarEvent):
        assert db.session.scalar(select(func.count(model.id))) == 0, model.__name__
    assert "Quiz 3" not in client.get("/dashboard").get_data(as_text=True)


def test_a_link_removed_by_another_worker_during_the_import_is_cleaned_up(app, client, student, net, monkeypatch):
    from app.services import ingest

    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    db.session.execute(text("DELETE FROM canvas_account"))  # the next import creates the account anew
    db.session.commit()
    feed = db.session.scalar(select(CalendarFeed))
    feed_id = feed.id
    real_ingest = ingest.ingest_snapshot
    seen = {}

    def ingest_then_removed(user, snapshot, manifest):
        result = real_ingest(user, snapshot, manifest)
        # The account exists from before the import, already marked as a calendar link's.
        seen["lms"] = db.session.scalar(select(CanvasAccount.lms).where(CanvasAccount.canvas_user_id == f"feed-{feed_id}"))
        # remove() in this process would wait: the import holds the student's lock.
        lock = feeds._user_lock(student.id)
        probe = threading.Thread(target=lambda: seen.setdefault("free", lock.acquire(blocking=False)))
        probe.start()
        probe.join()
        with db.engine.begin() as conn:  # another worker deletes only the link row
            conn.execute(text("DELETE FROM calendar_feed WHERE id = :id"), {"id": feed_id})
        return result

    monkeypatch.setattr(ingest, "ingest_snapshot", ingest_then_removed)
    assert feeds.refresh(feed, force=True) is None
    assert seen == {"lms": "ics", "free": False}
    db.session.expire_all()
    for model in (CanvasAccount, Course, Assignment, CalendarEvent):
        assert db.session.scalar(select(func.count(model.id))) == 0, model.__name__


def test_remove_deletes_a_link_account_not_recorded_on_the_link_and_keeps_canvas(app, client, student, net):
    import copy

    from app.services import ingest

    from .conftest import BASE_SNAPSHOT

    snap = copy.deepcopy(BASE_SNAPSHOT)
    snap["base_url"] = "https://learn.example.edu"
    ingest.ingest_snapshot(db.session.get(User, student.id), snap, [])  # a Canvas account on the same host
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    feed.account_id = None  # e.g. an import that ended before recording it
    db.session.commit()
    client.post(f"/settings/feeds/{feed.id}/remove")
    db.session.expire_all()
    assert [(a.lms, a.canvas_user_id) for a in db.session.scalars(select(CanvasAccount))] == [("canvas", "501")]


def test_our_own_calendar_is_refused_wherever_it_comes_from(app, client, student, net):
    """Review 3, correctness#2: our own ICS export pasted as a link imported every due date back as a new
    one, and again on every refresh."""
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    before = db.session.scalar(select(func.count(Assignment.id)))
    user = db.session.get(User, student.id)
    export = client.get(f"/calendar/{user.calendar_token}.ics").data
    calls = len(net.calls)
    app.config["PUBLIC_URL"] = "https://hh.example.org"
    for url in (f"https://hatch.test/calendar/{user.calendar_token}.ics",  # the address in use
                "webcal://homeworkhatch.onrender.com/calendar/x.ics", "https://www.homeworkhatch.com/calendar/x.ics",
                "https://hh.example.org/calendar/x.ics"):  # PUBLIC_URL
        page = html.unescape(add_feed(client, url).get_data(as_text=True))
        assert "That's a Homework Hatch calendar" in page, url
    assert len(net.calls) == calls, "never fetched"
    # Served from another address, or reached through a redirect: still refused.
    net.serve("https://mirror.example/hh.ics", FakeResponse(200, export))
    net.serve("https://short.example/c", FakeResponse(302, headers={"Location": "https://hh.example.org/calendar/x.ics"}))
    for url in ("https://mirror.example/hh.ics", "https://short.example/c"):
        assert "That's a Homework Hatch calendar" in html.unescape(add_feed(client, url).get_data(as_text=True))
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 1
    assert db.session.scalar(select(func.count(Assignment.id))) == before


def test_due_dates_we_pushed_to_google_calendar_are_not_imported_back(app, client, student, net):
    app.config["PUBLIC_URL"] = "https://hh.example.org"
    body = cal(
        # What gcal.py puts on Google Calendar, as Google's secret iCal link gives it back.
        ev("a1@google.com", "Due: Quiz 3 · CHEM 1010-001", z(at(3)),
           DESCRIPTION='Fall 2026 CHEM 1010-001 LEC<br><a href="https://hh.example.org/courses/assignments/1">Open in Homework Hatch</a>'),
        ev("a2@google.com", "✓ Due: Essay 1 · HIST 2200-003", z(at(6)),
           DESCRIPTION='<a href="https://hh.example.org/courses/assignments/2">Open</a>'),
        ev("assignment-7@hatch.test", "Due: Midterm (Fall 2026 HIST)", z(at(10))),  # our export, re-published
        ev("event-8@homeworkhatch.onrender.com", "Lab meeting", z(at(2))),
        ev("own@google.com", "Problem Set 2 - Due", z(at(4))),
        ev("own2@google.com", "Soccer practice", z(at(5))),
        head="PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\nX-WR-CALNAME:Sam\r\n")
    url = "https://calendar.google.com/calendar/ical/sam%40example.com/private-abc/basic.ics"
    net.serve(url, lambda h: FakeResponse(200, body))
    page = add_feed(client, url).get_data(as_text=True)
    assert "found 1 due date and 1 event" in page
    feed = db.session.scalar(select(CalendarFeed))
    for _ in range(3):
        feeds.refresh(feed, force=True)
    db.session.expire_all()
    assert [a.name for a in db.session.scalars(select(Assignment))] == ["Problem Set 2"]
    assert [e.title for e in db.session.scalars(select(CalendarEvent))] == ["Soccer practice"]


# ---------------------------------------------------------------- review 3: what the planner and calendar see


def test_a_late_window_end_is_not_a_second_test(app, client, student, net):
    """Review 3, correctness#4: Brightspace's End Date days after the Due Date (Moodle's 'should be
    completed' after 'closes') stayed in the class and the exam planner found the test twice."""
    host = "learn.example.edu"
    chem = "Fall 2026 CHEM 1010-001 LEC"
    quiz = f"Quizzes:\\nUnit 5 Test - https://{host}/d2l/lms/quizzing/quizzing.d2l?ou=111&qi=9"
    body = cal(ev("u1@x", "Unit 5 Test - Due", z(at(3)), z(at(3)), LOCATION=chem, DESCRIPTION=quiz),
               ev("u2@x", "Unit 5 Test - Availability Ends", z(at(10)), z(at(10)), LOCATION=chem, DESCRIPTION=quiz),
               ev("u3@x", "Unit 5 Test - Available", z(at(1)), z(at(1)), LOCATION=chem, DESCRIPTION=quiz), head=D2L)
    net.serve(BS_URL, FakeResponse(200, body))
    add_feed(client, BS_URL)
    found = [(f.title, f.kind) for f in assessments.find(db.session.get(User, student.id))]
    assert len(found) == 1 and found[0][0] == "Unit 5 Test", found
    ends = db.session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Unit 5 Test - Availability Ends"))
    assert ends is not None and ends.course_id is None, "still on the calendar, outside the class"

    moodle = cal(ev("1@m", "Unit 6 Test closes", z(at(4)), z(at(4)), CATEGORIES="BIO110"),
                 ev("2@m", "Unit 6 Test should be completed", z(at(11)), z(at(11)), CATEGORIES="BIO110"),
                 head="PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024100700//EN\r\n")
    snap, _ = snapshot_for("moodle", moodle, "moodle.example.edu")
    assert set(assignments(snap)) == {"Unit 6 Test"}
    assert events(snap)["Unit 6 Test should be completed"]["course"] is None


def test_multi_day_all_day_events_show_on_every_day(app, client, student, net):
    """Review 3, correctness#5: a week-long break was stored as one date and showed on its first day only."""
    from app.blueprints.main import _event_days

    first = at(3).date()
    day = lambda d: ";VALUE=DATE:" + d.strftime("%Y%m%d")  # noqa: E731
    body = cal(ev("brk@x", "Spring Break", day(first), day(first + timedelta(days=5)), LOCATION="Example University"),
               ev("one@x", "Reading Day", day(first + timedelta(days=7)), day(first + timedelta(days=8)),
                  LOCATION="Example University"),
               ev("q@x", "Quiz - Due", z(at(2)), z(at(2)), LOCATION="Fall 2026 CHEM 1010-001 LEC"), head=D2L)
    net.serve(BS_URL, FakeResponse(200, body))
    add_feed(client, BS_URL)
    zone = ZoneInfo("America/New_York")
    brk = db.session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Spring Break"))
    assert brk.all_day and brk.all_day_date is None, "all day for the planner, no single date"
    shown = _event_days(brk, zone)
    assert [d for d, *_ in shown] == [first + timedelta(days=i) for i in range(5)]
    assert {label for _d, _w, label, _l in shown} == {"All day"}
    one = db.session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Reading Day"))
    assert one.all_day_date == first + timedelta(days=7)
    assert [d for d, *_ in _event_days(one, zone)] == [first + timedelta(days=7)]


def test_the_repeat_budget_is_shared_so_no_series_vanishes(app):
    """Review 3, correctness#6: series were expanded in file order from one budget, so a few daily
    routines used it up and a weekly due date after them vanished (and its stored rows were deleted)."""
    now = utcnow()
    start = (now - timedelta(days=60)).strftime("%Y%m%d")
    routines = [ev(f"daily{n}@google.com", f"Routine {n}", f";TZID=America/New_York:{start}T0{6 + n}0000",
                   RRULE="FREQ=DAILY") for n in range(4)]
    pset = ev("pset@google.com", "Problem set - Due", f";TZID=America/New_York:{start}T235900", RRULE="FREQ=WEEKLY")
    body = cal(*routines, pset, head="X-WR-CALNAME:Sam\r\n")
    parsed = parse(body)
    per_series = Counter(i.uid for i in parsed.items)
    weeks_ahead = (feeds.LOOKAHEAD.days // 7)
    assert per_series["pset@google.com"] >= weeks_ahead + 1
    assert all(per_series[f"daily{n}@google.com"] for n in range(4))
    assert len(parsed.items) <= feeds.MAX_OCCURRENCES and parsed.shape["occurrences_capped"]
    for n in range(4):  # a series that was cut keeps what's nearest: its next occurrence is there
        upcoming = [i.start for i in parsed.items if i.uid == f"daily{n}@google.com" and i.start >= now]
        assert upcoming and min(upcoming) - now < timedelta(days=1)
    _snap, counts = snapshot_for("other", body, "calendar.google.com")
    assert counts["due"] >= weeks_ahead + 1

    # More series than slices: due-looking series go first, then what's nearest to now.
    many = cal(*(ev(f"r{n}@google.com", f"Routine {n}", f";TZID=America/New_York:{start}T080000", RRULE="FREQ=DAILY")
                 for n in range(60)), pset)
    parsed = parse(many)
    assert len(parsed.items) == feeds.MAX_OCCURRENCES
    assert Counter(i.uid for i in parsed.items)["pset@google.com"] == per_series["pset@google.com"]
    assert max(i.start for i in parsed.items if i.uid != "pset@google.com") < now + timedelta(days=12)


# ---------------------------------------------------------------- review 3: messages


def test_a_link_that_fails_to_be_added_never_promises_a_retry(app, client, student, net):
    """Review 3, ui#2: a failed add said "We'll try again later" and "The link wasn't added" together."""
    net.serve(BS_URL, FakeResponse(503))
    page = html.unescape(add_feed(client, BS_URL).get_data(as_text=True))
    assert ("Brightspace didn't answer properly (503). Try adding it again in a few minutes. "
            "The link wasn't added.") in page
    assert "try again later" not in page.lower()
    net.serve(BS_URL, FakeResponse(404))
    page = html.unescape(add_feed(client, BS_URL).get_data(as_text=True))
    assert "The link stopped working (404). Copy a fresh link from Brightspace. The link wasn't added." in page
    assert db.session.scalar(select(func.count(CalendarFeed.id))) == 0

    # A link that was kept is retried, and says so.
    net.serve(BS_URL, FakeResponse(200, brightspace_ics()))
    add_feed(client, BS_URL)
    feed = db.session.scalar(select(CalendarFeed))
    net.serve(BS_URL, FakeResponse(503))
    assert feeds.refresh(feed, force=True) is None
    db.session.expire_all()
    assert db.session.get(CalendarFeed, feed.id).last_error == "Brightspace didn't answer properly (503). We'll try again later."


# ---------------------------------------------------------------- review 3: refreshing without page views


def _stale_feed(user, n: int, hours_ago: float | None, net) -> CalendarFeed:
    url = f"https://learn.example.edu/d2l/le/calendar/feed/user/feed.ics?token=t{n}"
    net.serve(url, FakeResponse(200, cal(ev(f"u{n}@x", f"Item {n} - Due", z(at(2)), z(at(2)),
                                            LOCATION="Fall 2026 CHEM 1010-001 LEC"), head=D2L)))
    feed = CalendarFeed(user_id=user.id, url=url, url_hash=feeds.url_hash(url), lms="brightspace",
                        host="learn.example.edu",
                        last_fetched_at=None if hours_ago is None else utcnow() - timedelta(hours=hours_ago))
    db.session.add(feed)
    db.session.commit()
    return feed


def test_health_refreshes_the_stalest_links_of_every_student(app, client, net):
    """Review 3, ui#0: links only refreshed when their student opened a page, though the Connect page and
    the privacy policy promise about-hourly refreshes and Google Calendar delivery. The keep-alive ping
    to /health now sweeps the stalest links of everyone, a few at a time, at most every 5 minutes."""
    ana, ben, gone, busy = (make_user(n, f"{n}@example.com") for n in ("ana", "ben", "gone", "busy"))
    gone.active = False
    db.session.commit()
    feeds_by_age = [_stale_feed(ana, 0, 2, net), _stale_feed(ben, 1, 3, net), _stale_feed(ana, 2, 4, net),
                    _stale_feed(ben, 3, 5, net), _stale_feed(ana, 4, 6, net), _stale_feed(ben, 5, None, net),
                    _stale_feed(ana, 6, 0.5, net)]  # fresh: not due
    inactive = _stale_feed(gone, 7, None, net)
    in_progress = _stale_feed(busy, 8, 9, net)  # a page view is refreshing this student's links already
    ids = [f.id for f in feeds_by_age]
    feeds._running.add(busy.id)
    try:
        assert client.get("/health").get_json()["ok"] is True
    finally:
        feeds._running.discard(busy.id)
    db.session.expire_all()
    fetched = {c["target"].rsplit("=", 1)[1] for c in net.calls}
    assert fetched == {"t5", "t4", "t3", "t2", "t1"}, "the five stalest (never checked first), anyone's"
    assert all(db.session.get(CalendarFeed, i).account_id for i in ids[1:6])
    assert db.session.get(CalendarFeed, inactive.id).last_fetched_at is None
    assert db.session.get(CalendarFeed, in_progress.id).account_id is None

    calls = len(net.calls)
    assert client.get("/health").status_code == 200
    assert len(net.calls) == calls, "at most one sweep every 5 minutes"

    result = app.test_cli_runner().invoke(args=["feeds", "refresh-stale"])  # by hand, for everything stale
    assert result.exit_code == 0 and "Refreshed 2 calendar links." in result.output
    db.session.expire_all()
    assert {c["target"].rsplit("=", 1)[1] for c in net.calls[calls:]} == {"t0", "t8"}
    assert db.session.get(CalendarFeed, inactive.id).last_fetched_at is None


def test_the_sweep_runs_on_its_own_thread_and_never_breaks_health(app, client, monkeypatch):
    calls, release = [], threading.Event()

    def slow_sweep(limit):
        calls.append(limit)
        release.wait(5)
        return 0

    monkeypatch.setattr(feeds, "refresh_stale", slow_sweep)
    app.config["EXTRACT_INLINE"] = False
    try:
        started = time.monotonic()
        assert feeds.tick(app) is True
        assert time.monotonic() - started < 0.5, "returns at once"
        app.extensions["hh_feeds_tick"]["last"] = None
        assert feeds.tick(app) is False, "one sweep at a time"
    finally:
        release.set()
    for _ in range(50):
        if not app.extensions["hh_feeds_tick"]["running"]:
            break
        time.sleep(0.05)
    assert calls == [feeds.TICK_BATCH] and not app.extensions["hh_feeds_tick"]["running"]

    app.config["EXTRACT_INLINE"] = True
    app.extensions["hh_feeds_tick"]["last"] = None

    def broken(limit):
        raise RuntimeError("database down")

    monkeypatch.setattr(feeds, "refresh_stale", broken)
    assert client.get("/health").get_json()["ok"] is True


def test_the_sweep_delivers_new_due_dates_to_google_calendar(app, client, student, net, composio):
    composio.connect(student, "googlecalendar")
    db.session.add(Integration(user_id=student.id, kind="calendar", connected=True, settings={"enabled": True}))
    db.session.commit()
    body = [brightspace_ics()]
    net.serve(BS_URL, lambda h: FakeResponse(200, body[0]))
    add_feed(client, BS_URL)
    client.post("/logout")  # the student doesn't open anything after this
    body[0] = body[0].replace(b"END:VCALENDAR", ev("new@x", "Project - Due", z(at(9)), z(at(9)),
                                                   LOCATION="Fall 2026 CHEM 1010-001 LEC").encode() + b"END:VCALENDAR")
    db.session.scalar(select(CalendarFeed)).last_fetched_at = utcnow() - timedelta(minutes=61)
    db.session.commit()
    client.get("/health")
    created = [a["summary"] for s, a in composio.calls if s == "GOOGLECALENDAR_CREATE_EVENT"]
    assert "Due: Project · CHEM 1010-001" in created
