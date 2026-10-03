"""Calendar links (app/services/feeds.py): reading each LMS's iCal feed, turning it into classes, due
dates and events through the normal ingest, the SSRF guards, the routes and the hourly refresh.

Every calendar here is synthetic, written in the shape each LMS produces (see the comments in
feeds.py for the real feeds the shapes come from). No network: DNS and HTTP are faked.
"""

from __future__ import annotations

import ipaddress
import json
import socket
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
    assert "Essay 1 - Availability Ends" in e and e["Essay 1 - Availability Ends"]["course"] == "Fall 2026 HIST 2200-003 LEC"
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
