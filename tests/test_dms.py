"""Class chat rooms (grouped by the real course, never its name) and direct messages between classmates."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.extensions import db
from app.models import Course, DirectMessage, DirectReport, DirectThread, User, UserBlock, utcnow
from app.services import dms

from .conftest import api_token, login, make_user, sync


def _student(app, snapshot, manifest, name, canvas_id, birth_year=2004, courses=None, base_url=None, new=False):
    user = make_user(name, birth_year=birth_year)
    if not new:  # brand-new accounts wait a day before sending message requests
        user.created_at = utcnow() - timedelta(days=2)
        db.session.commit()
    snap = dict(snapshot, user={"id": canvas_id, "name": name.title(), "short_name": name.title()})
    if courses is not None:
        snap["courses"] = courses
    if base_url:
        snap["base_url"] = base_url
    sync(app.test_client(), api_token(user, raw=f"hh_test_token_{name}_" + "z" * 20), snap, manifest)
    c = app.test_client()
    login(c, user)
    c.post("/chat/rules")
    return user, c


def _calc(user) -> Course:
    return db.session.scalar(select(Course).where(Course.user_id == user.id, Course.canvas_id == "101"))


def _say(client, course, body):
    r = client.post(f"/chat/course/{course.id}/messages", json={"body": body})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["message"]["id"]


def test_rooms_follow_the_real_course_not_its_name(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "701")
    bob, cb = _student(app, snapshot, manifest, "bob", "702")
    # Bob renames the class, and his LMS later renames it too: still the same room.
    cb.post(f"/courses/{_calc(bob).id}/customize", data={"name": "Calc w/ Dr K", "code": "CALC"})
    renamed = [dict(snapshot["courses"][0], name="Calculus I (Fall, renamed)")] + snapshot["courses"][1:]
    sync(app.test_client(), api_token(bob, raw="hh_test_token_bob2_" + "y" * 20),
         dict(snapshot, user={"id": "702", "name": "Bob", "short_name": "Bob"}, courses=renamed), manifest)
    assert _calc(bob).name == "Calc w/ Dr K" and _calc(bob).chat_key == _calc(alice).chat_key
    _say(ca, _calc(alice), "hello class")
    assert [m["body"] for m in cb.get(f"/chat/course/{_calc(bob).id}/messages").get_json()["messages"]] == ["hello class"]
    # A class with the same name at another school, or a different course id, is another room.
    carl, cc = _student(app, snapshot, manifest, "carl", "703", base_url="https://other.instructure.com")
    assert _calc(carl).chat_key != _calc(alice).chat_key
    assert cc.get(f"/chat/course/{_calc(carl).id}/messages").get_json()["messages"] == []


def test_teachers_and_calendar_links_have_no_room(app, snapshot, manifest):
    teach_course = dict(snapshot["courses"][0], enrollment_role="teacher")
    tina, ct = _student(app, snapshot, manifest, "tina", "801", birth_year=1980, courses=[teach_course])
    assert _calc(tina).enrollment_role == "teacher"
    assert ct.get(f"/chat/course/{_calc(tina).id}").status_code == 404
    assert ct.get(f"/chat/course/{_calc(tina).id}/messages").status_code == 404
    alice, ca = _student(app, snapshot, manifest, "alice", "802")
    assert dms.member_count(_calc(alice).chat_key) == 1, "the teacher isn't counted"


def test_direct_messages_request_accept_and_block(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "701")
    bob, cb = _student(app, snapshot, manifest, "bob", "702")
    mid = _say(cb, _calc(bob), "anyone want to study for the midterm?")
    # The room tells Alice she can message Bob, and Bob can't message himself.
    msgs = ca.get(f"/chat/course/{_calc(alice).id}/messages").get_json()["messages"]
    assert msgs[0]["dm"] is True
    assert cb.get(f"/chat/course/{_calc(bob).id}/messages").get_json()["messages"][0]["dm"] is False
    r = ca.post(f"/chat/dm/from/{mid}", data={"body": "me! library at 4?"})
    thread = db.session.scalar(select(DirectThread))
    assert r.status_code == 302 and thread.status == "request" and thread.started_by == alice.id
    # Nothing more until Bob accepts.
    assert ca.post(f"/chat/dm/{thread.id}/messages", json={"body": "hello??"}).status_code == 400
    assert dms.unread_count(bob.id) == 1 and dms.unread_count(alice.id) == 0
    assert "wants to message you" in cb.get(f"/chat/dm/{thread.id}").get_data(as_text=True)
    assert dms.unread_count(bob.id) == 0, "opening it reads it"
    # Replying accepts.
    assert cb.post(f"/chat/dm/{thread.id}/messages", json={"body": "sure, see you there"}).status_code == 200
    assert db.session.get(DirectThread, thread.id).status == "active"
    assert ca.post(f"/chat/dm/{thread.id}/messages", json={"body": "great"}).status_code == 200
    bodies = [m["body"] for m in ca.get(f"/chat/dm/{thread.id}/messages").get_json()["messages"]]
    assert bodies == ["me! library at 4?", "sure, see you there", "great"]
    # Strangers can't see it.
    eve, ce = _student(app, snapshot, manifest, "eve", "703")
    assert ce.get(f"/chat/dm/{thread.id}").status_code == 404
    assert ce.get(f"/chat/dm/{thread.id}/messages").status_code == 404
    # Reports go to admins with a little context.
    dm_id = db.session.scalar(select(DirectMessage.id).where(DirectMessage.sender_id == alice.id).order_by(DirectMessage.id.desc()))
    assert ca.post(f"/chat/dm/messages/{dm_id}/report").status_code == 400, "you can't report your own"
    assert cb.post(f"/chat/dm/messages/{dm_id}/report", json={"reason": "creepy"}).status_code == 200
    report = db.session.scalar(select(DirectReport))
    assert [m["body"] for m in dms.report_context(report)][-1] == "great"
    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    page = adm.get("/admin/reports").get_data(as_text=True)
    assert "Reported direct messages" in page and "creepy" in page
    # Blocking ends it both ways and takes it out of the inbox (it stays readable for the blocker).
    cb.post(f"/chat/dm/{thread.id}/respond", data={"action": "block"})
    assert ca.post(f"/chat/dm/{thread.id}/messages", json={"body": "hey"}).status_code == 400
    assert "You blocked" in cb.get(f"/chat/dm/{thread.id}").get_data(as_text=True)
    assert dms.threads(bob.id) == []


def test_who_can_message_whom(app, snapshot, manifest):
    adult, ca = _student(app, snapshot, manifest, "ann", "711", birth_year=2000)
    teen, ct = _student(app, snapshot, manifest, "tom", "712", birth_year=utcnow().year - 15)
    teen2, ct2 = _student(app, snapshot, manifest, "tia", "713", birth_year=utcnow().year - 16)
    teen_msg = _say(ct, _calc(teen), "who's in study group?")
    adult_msg = _say(ca, _calc(adult), "me")
    # Adults and under-18s can never message each other, either way, and nothing hints at why: the
    # "message" link shows on every post, and every refusal reads the same.
    assert ca.get(f"/chat/course/{_calc(adult).id}/messages").get_json()["messages"][0]["dm"] is True
    page = ca.get(f"/chat/dm/from/{teen_msg}").get_data(as_text=True)
    assert "taking messages from you" in page
    assert ca.post(f"/chat/dm/from/{teen_msg}", data={"body": "hi"}).status_code == 400
    assert ct.post(f"/chat/dm/from/{adult_msg}", data={"body": "hi"}).status_code == 400
    assert db.session.scalar(select(func.count(DirectThread.id))) == 0
    # Two teens in the same class can.
    assert ct2.post(f"/chat/dm/from/{teen_msg}", data={"body": "me!"}).status_code == 302
    # Someone who turned requests off gets none; someone outside the class can't reach the message at all.
    other_course = [dict(snapshot["courses"][0], id="555", name="Other")]
    out, co = _student(app, snapshot, manifest, "otto", "714", birth_year=utcnow().year - 15, courses=other_course)
    assert co.post(f"/chat/dm/from/{teen_msg}", data={"body": "hi"}).status_code == 404
    db.session.get(User, teen2.id).allow_dms = False
    db.session.commit()
    tia_msg = _say(ct2, _calc(teen2), "studying tonight")
    teen3, ct3 = _student(app, snapshot, manifest, "tess", "715", birth_year=utcnow().year - 15)
    assert ct3.post(f"/chat/dm/from/{tia_msg}", data={"body": "hey"}).status_code == 400
    # Without agreeing to the rules there's no messaging.
    newbie = make_user("nina", birth_year=utcnow().year - 15)
    assert dms.refusal(newbie, teen) == "Agree to the chat rules first."


def test_request_limits_and_export(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "721")
    others = [_student(app, snapshot, manifest, f"u{i}", f"73{i}") for i in range(11)]
    from datetime import timedelta

    sent = 0
    for user, client in others:
        mid = _say(client, _calc(user), "hi")
        if ca.post(f"/chat/dm/from/{mid}", data={"body": "hey"}).status_code == 302:
            sent += 1
        for m in db.session.scalars(select(DirectMessage)):  # step past the spam limit, not the daily one
            m.created_at = m.created_at - timedelta(minutes=1)
        db.session.commit()
    assert sent == dms.NEW_REQUESTS_PER_DAY
    data = ca.get("/settings/data/export").get_json()
    assert len(data["direct_messages_sent"]) == dms.NEW_REQUESTS_PER_DAY


def test_account_deletion_removes_conversations(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "741")
    bob, cb = _student(app, snapshot, manifest, "bob", "742")
    mid = _say(cb, _calc(bob), "hi")
    ca.post(f"/chat/dm/from/{mid}", data={"body": "hey bob"})
    cb.post(f"/chat/dm/{db.session.scalar(select(DirectThread.id))}/respond", data={"action": "block"})
    db.session.delete(db.session.get(User, alice.id))
    db.session.commit()
    assert db.session.scalar(select(func.count(DirectThread.id))) == 0
    assert db.session.scalar(select(func.count(DirectMessage.id))) == 0
    assert db.session.scalar(select(func.count(UserBlock.id))) == 0


def test_rooms_need_proof_of_being_in_the_class(app, snapshot, manifest):
    """A hand-made sync can name a real course, but without its Canvas uuid it can't get into the room."""
    alice, ca = _student(app, snapshot, manifest, "alice", "751", birth_year=utcnow().year - 15)
    _say(ca, _calc(alice), "hi everyone")
    no_proof = [{k: v for k, v in snapshot["courses"][0].items() if k != "uuid_hash"}]
    mal, cm = _student(app, snapshot, manifest, "mal", "752", birth_year=utcnow().year - 15, courses=no_proof)
    assert _calc(mal).chat_key is None and cm.get(f"/chat/course/{_calc(mal).id}/messages").status_code == 404
    assert "Update the Homework Hatch extension" in cm.get("/chat/").get_data(as_text=True)
    guessed = [dict(snapshot["courses"][0], uuid_hash="0" * 64)]
    max_, cx = _student(app, snapshot, manifest, "max", "753", birth_year=utcnow().year - 15, courses=guessed)
    assert _calc(max_).chat_key != _calc(alice).chat_key
    assert cx.get(f"/chat/course/{_calc(max_).id}/messages").get_json()["messages"] == []
    assert dms.shared_room(max_.id, alice.id) is None
    # Hiding a class takes it out of chat too.
    ca.post(f"/courses/{_calc(alice).id}/visibility")
    assert ca.get(f"/chat/course/{_calc(alice).id}/messages").status_code == 404


def test_age_bands_and_locked_birth_year(app, snapshot, manifest):
    eighteen = make_user("eddie", birth_year=utcnow().year - 18)
    assert eighteen.age_band == "edge", "a birth year alone can't tell an 18-year-old from a 17-year-old"
    month_after = (utcnow().month % 12) + 1
    young = make_user("yara", birth_year=utcnow().year - 18)
    young.birth_month = month_after if month_after > utcnow().month else None
    assert young.age_band in ("minor", "edge")
    adult, ca = _student(app, snapshot, manifest, "ann", "761", birth_year=1990)
    ca.post("/settings/", data={"action": "profile", "display_name": "Ann", "birth_year": str(utcnow().year - 14),
                                "timezone": "UTC"})
    assert db.session.get(User, adult.id).birth_year == 1990, "birth year can't be edited after sign-up"


def test_contact_details_stay_out_of_rooms_and_teen_messages(app, snapshot, manifest):
    teen, ct = _student(app, snapshot, manifest, "tom", "771", birth_year=utcnow().year - 15)
    r = ct.post(f"/chat/course/{_calc(teen).id}/messages", json={"body": "add me on snap: tom_15"})
    assert r.status_code == 400 and "can't be shared here" in r.get_json()["error"]
    teen2, ct2 = _student(app, snapshot, manifest, "tia", "772", birth_year=utcnow().year - 16)
    mid = _say(ct, _calc(teen), "who wants to study?")
    assert ct2.post(f"/chat/dm/from/{mid}", data={"body": "text me 555-123-4567"}).status_code == 400
    assert db.session.scalar(select(func.count(DirectThread.id))) == 0


def test_reports_survive_block_decline_and_deletion(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "781")
    bob, cb = _student(app, snapshot, manifest, "bob", "782")
    mid = _say(cb, _calc(bob), "hi")
    ca.post(f"/chat/dm/from/{mid}", data={"body": "you're so weird lol"})
    thread = db.session.scalar(select(DirectThread))
    bad = db.session.scalar(select(DirectMessage))
    # Bob blocks and reports straight from the request card.
    cb.post(f"/chat/dm/{thread.id}/respond", data={"action": "block_report"})
    report = db.session.scalar(select(DirectReport))
    assert report is not None and report.sender_id == alice.id and report.snapshot[0]["body"] == "you're so weird lol"
    # Even if Alice deletes her account, the report keeps what she said.
    db.session.delete(db.session.get(User, alice.id))
    db.session.commit()
    report = db.session.get(DirectReport, report.id)
    assert report.message_id is None and report.snapshot[0]["body"] == "you're so weird lol"
    assert report.sender_name == "alice" and report.sender_band == "adult", "who sent it outlives their account"
    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    assert "alice (account deleted)" in adm.get("/admin/reports").get_data(as_text=True)
    # Unblock is on the Chat page.
    cb.post(f"/chat/blocks/{alice.id}/unblock")
    assert db.session.scalar(select(func.count(UserBlock.id))) == 0


def test_declined_requests_and_deleted_messages(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "791")
    bob, cb = _student(app, snapshot, manifest, "bob", "792")
    bob_msg, alice_msg = _say(cb, _calc(bob), "hi"), _say(ca, _calc(alice), "hey")
    ca.post(f"/chat/dm/from/{bob_msg}", data={"body": "study?"})
    thread = db.session.scalar(select(DirectThread))
    cb.post(f"/chat/dm/{thread.id}/respond", data={"action": "decline"})
    assert ca.post(f"/chat/dm/from/{bob_msg}", data={"body": "please?"}).status_code in (302, 400)
    assert db.session.get(DirectThread, thread.id).status == "declined", "the requester can't re-ask"
    # Bob changes his mind: reaching out himself opens a new request from him.
    assert cb.post(f"/chat/dm/from/{alice_msg}", data={"body": "actually yes"}).status_code == 302
    t = db.session.get(DirectThread, thread.id)
    assert t.status == "request" and t.started_by == bob.id
    # A deleted message stops counting as unread and leaves the other window.
    assert dms.unread_count(alice.id) == 1
    last = db.session.scalar(select(DirectMessage).where(DirectMessage.sender_id == bob.id))
    cb.post(f"/chat/dm/messages/{last.id}/delete")
    assert dms.unread_count(alice.id) == 0
    assert last.id in ca.get(f"/chat/dm/{thread.id}/messages").get_json()["deleted"]


BLOCKED = ["add me on snap: mal_1985", "text 555-123-4567", "call +1 (434) 555 0199", "discord.gg/abc", "me@example.com",
           "my insta is cool.kid", "dm @jason_22", "wa.me/15551234567", "mal_1985 on snap", "\U0001F47B mal1985",
           "mal at gmail dot com", "text me 555-1234", "ig: mal.1985", "+44 7700 900123",
           # unformatted and oddly split numbers, zero-width characters, more handle and link forms
           "hmu 4105551234", "(410)5551234", "410/555/1234", "4 1 0 5 5 5 1 2 3 4", "410\u200b555\u200b1234",
           "hmu on ig x_y", "snap mal_1985", "snap me malcolm_x", "snap \u2014 mal_1985", "my telegram is malcolmx",
           "telegram: malcolmx", "follow me on insta @malcolm", "discord mal#1985", "jake2004 @ gmail.com",
           "jake2004 at gmail", "my gmail is jake2004", "vm.tiktok.com/ZMabc123", "linktr.ee/malx", "m.me/malx"]
ALLOWED = ["problems 1 3 5 7 9 11 for tomorrow", "due 2026-10-15 at midnight", "pi is 3.14159265", "isbn 978-0-13-468599-1",
           "x = 1234567890", "signal transduction pathway in cell bio", "Zimmermann Telegram was sent in 1917",
           "oh snap that's due tomorrow", "the facebook case study in econ", "my tiktok feed is all calc memes",
           "@Sarah what did you get for #4?", "@everyone quiz moved to friday", "https://us02web.zoom.us/j/81234567890",
           "meeting id 812 3456 7890", "read pages 112-118", "we can talk on discord about it", "HW 3 is due 10/14 at 11:59",
           "snap judgment question on the quiz", "email the professor about it", "Sc has atomic number 21",
           # everyday chat the first version blocked
           "Discord is down for me", "ig is fine", "ig: that makes sense", "oh snap - forgot the lab report",
           "I saw hw3 on tiktok lol", "the review is at 7pm on discord", "lecture @10am tomorrow",
           "The block is at rest. Net force is zero.", "Avogadro's number is 6.02214076 x 10^23", "sum is 3+0.1234567891",
           "data: 120 135 1500", "just call t.mean(dim=0)", "y_hat = X@self.weights", "git clone git@github.com:cs2150/lab.git",
           "happy halloween \U0001F47B who's going", "prof's email is jones@virginia.edu", "my discord is down"]


def test_contact_filter_catches_handles_not_class_talk(app):
    from app.services import moderation

    assert [t for t in BLOCKED if not moderation.has_contact(t)] == []
    assert [t for t in ALLOWED if moderation.has_contact(t)] == []
    # Display names are shown to classmates too.
    for name in ("snap: mal_1985", "mal@example.com", "call 555-123-4567"):
        try:
            moderation.clean_name(name)
        except moderation.Rejected:
            continue
        raise AssertionError(name)
    assert moderation.clean_name("Sam S.") == "Sam S."
    assert moderation.name_has_contact("4345550199") and moderation.name_has_contact("snap-mal.1985")
    assert not moderation.name_has_contact("sam_2007")
    assert moderation.name_has_contact("snap_mal1985") and moderation.name_has_contact("mal1985 on snap")
    # Long inputs stay fast (names are cut to 80 characters before any check).
    import time

    t = time.perf_counter()
    moderation.has_contact("a." * 20000)
    try:
        moderation.clean_name("a." * 20000)
    except moderation.Rejected:
        pass
    assert time.perf_counter() - t < 1


def test_deleted_messages_stay_reportable(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "801")
    bob, cb = _student(app, snapshot, manifest, "bob", "802")
    mid = _say(ca, _calc(alice), "study group?")
    cb.post(f"/chat/dm/from/{mid}", data={"body": "sure"})
    thread = db.session.scalar(select(DirectThread))
    ca.post(f"/chat/dm/{thread.id}/messages", json={"body": "cool"})
    assert cb.post(f"/chat/dm/{thread.id}/messages", json={"body": "something nasty"}).status_code == 200
    nasty = db.session.scalar(select(DirectMessage).order_by(DirectMessage.id.desc()).limit(1))  # bodies are encrypted
    cb.post(f"/chat/dm/messages/{nasty.id}/delete")
    # Alice sees that a message was deleted (never its text) and can still report it.
    seen = {m["id"]: m for m in ca.get(f"/chat/dm/{thread.id}/messages").get_json()["messages"]}
    assert seen[nasty.id]["deleted"] is True and seen[nasty.id]["body"] == ""
    assert ca.post(f"/chat/dm/messages/{nasty.id}/report", json={"reason": "mean"}).status_code == 200
    assert db.session.scalar(select(DirectReport)).snapshot[-1]["body"] == "something nasty"
    # "Block and report" works in an accepted conversation too, on their latest message even if deleted.
    db.session.delete(db.session.scalar(select(DirectReport)))
    db.session.commit()
    assert "Block and report" in ca.get(f"/chat/dm/{thread.id}").get_data(as_text=True)
    ca.post(f"/chat/dm/{thread.id}/respond", data={"action": "block_report"})
    assert db.session.scalar(select(DirectReport)).message_id == nasty.id


def test_refusals_never_say_which_rule(app, snapshot, manifest):
    other_course = [dict(snapshot["courses"][0], id="556", name="Other", uuid_hash="ab" * 32)]
    adult, _ = _student(app, snapshot, manifest, "ann", "811", birth_year=2000)
    teen, _ = _student(app, snapshot, manifest, "tom", "812", birth_year=utcnow().year - 15, courses=other_course)
    adult2, _ = _student(app, snapshot, manifest, "abe", "813", birth_year=2000, courses=other_course)
    # Outside a shared class, everyone hears the same thing, whatever their age.
    assert dms.refusal(adult, teen) == dms.refusal(adult, adult2) == "You can only message classmates."


def test_blocking_hides_room_posts_and_explains_the_link(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "821")
    bob, cb = _student(app, snapshot, manifest, "bob", "822")
    mid = _say(cb, _calc(bob), "hi")
    ca.post(f"/chat/dm/from/{mid}", data={"body": "hey"})
    cb.post(f"/chat/dm/{db.session.scalar(select(DirectThread.id))}/respond", data={"action": "block"})
    alice_post = _say(ca, _calc(alice), "anyone?")
    assert alice_post not in [m["id"] for m in cb.get(f"/chat/course/{_calc(bob).id}/messages").get_json()["messages"]]
    assert mid in [m["id"] for m in ca.get(f"/chat/course/{_calc(alice).id}/messages").get_json()["messages"]]
    page = cb.get(f"/chat/dm/from/{alice_post}")
    assert page.status_code == 200 and "You blocked" in page.get_data(as_text=True)


def test_new_accounts_wait_a_day(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "831")
    fresh, cf = _student(app, snapshot, manifest, "fred", "832", new=True)
    mid = _say(ca, _calc(alice), "hi")
    r = cf.post(f"/chat/dm/from/{mid}", data={"body": "hey"})
    assert r.status_code == 400 and db.session.scalar(select(func.count(DirectThread.id))) == 0
    assert "a day after signing up" in cf.get(f"/chat/dm/from/{mid}").get_data(as_text=True)
    # Reports from brand-new accounts don't count toward auto-hiding a room post.
    for i in range(3):
        _, c = _student(app, snapshot, manifest, f"n{i}", f"84{i}", new=True)
        c.post(f"/chat/messages/{mid}/report")
    from app.models import ChatMessage

    assert db.session.get(ChatMessage, mid).deleted is False
    for i in range(3):
        _, c = _student(app, snapshot, manifest, f"o{i}", f"85{i}")
        c.post(f"/chat/messages/{mid}/report")
    assert db.session.get(ChatMessage, mid).deleted is True


def test_unclear_age_can_add_birth_month_once(app, snapshot, manifest):
    eddie, ce = _student(app, snapshot, manifest, "eddie", "861", birth_year=utcnow().year - 18)
    assert db.session.get(User, eddie.id).age_band == "edge"
    assert "Add your birth month" in ce.get("/chat/").get_data(as_text=True)
    month = 1 if utcnow().month > 1 else 12
    ce.post("/chat/birth-month", data={"birth_month": str(month)})
    user = db.session.get(User, eddie.id)
    assert user.birth_month == month and user.age_band != "edge"
    ce.post("/chat/birth-month", data={"birth_month": "6"})
    assert db.session.get(User, eddie.id).birth_month == month, "set once"


def test_unclear_ages_cant_message_anyone(app, snapshot, manifest):
    """A birth year alone can't tell 18 from 17, so two such accounts can't message each other either."""
    a, ca = _student(app, snapshot, manifest, "ava", "871", birth_year=utcnow().year - 18)
    b, cb = _student(app, snapshot, manifest, "ben", "872", birth_year=utcnow().year - 18)
    assert a.age_band == b.age_band == "edge"
    mid = _say(cb, _calc(b), "hi")
    assert ca.post(f"/chat/dm/from/{mid}", data={"body": "hey"}).status_code == 400
    assert dms.refusal(a, b) == dms.NOT_TAKING


def test_blocker_can_still_delete_and_report(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "881")
    bob, cb = _student(app, snapshot, manifest, "bob", "882")
    mid = _say(cb, _calc(bob), "hi")
    ca.post(f"/chat/dm/from/{mid}", data={"body": "my address is 12 Elm"})
    thread = db.session.scalar(select(DirectThread))
    cb.post(f"/chat/dm/{thread.id}/messages", json={"body": "weird"})
    ca.post(f"/chat/dm/{thread.id}/respond", data={"action": "block"})
    # Alice can open it read-only, delete what she overshared, and report Bob's message.
    page = ca.get(f"/chat/dm/{thread.id}").get_data(as_text=True)
    assert "You blocked" in page and 'id="composer"' not in page
    mine = db.session.scalar(select(DirectMessage).where(DirectMessage.sender_id == alice.id))
    assert ca.post(f"/chat/dm/messages/{mine.id}/delete").status_code == 200
    seen = {m["id"]: m for m in cb.get(f"/chat/dm/{thread.id}/messages").get_json()["messages"]}
    assert seen[mine.id]["deleted"] and seen[mine.id]["body"] == ""
    theirs = db.session.scalar(select(DirectMessage).where(DirectMessage.sender_id == bob.id))
    assert ca.post(f"/chat/dm/messages/{theirs.id}/report", json={"reason": "x"}).status_code == 200
    assert ca.post(f"/chat/dm/{thread.id}/messages", json={"body": "hi"}).status_code == 400
    # Nobody can delete someone else's message.
    assert cb.post(f"/chat/dm/messages/{mine.id}/delete").status_code == 404


def test_moderator_removal_and_honest_block_and_report(app, snapshot, manifest):
    alice, ca = _student(app, snapshot, manifest, "alice", "891")
    bob, cb = _student(app, snapshot, manifest, "bob", "892")
    mid = _say(cb, _calc(bob), "hi")
    ca.post(f"/chat/dm/from/{mid}", data={"body": "hey"})
    thread = db.session.scalar(select(DirectThread))
    cb.post(f"/chat/dm/{thread.id}/respond", data={"action": "accept"})
    # Bob hasn't said anything, so Alice gets no "Block and report" and no false "thanks for reporting".
    assert "Block and report" not in ca.get(f"/chat/dm/{thread.id}").get_data(as_text=True)
    cb.post(f"/chat/dm/{thread.id}/messages", json={"body": "bad"})
    bad = db.session.scalar(select(DirectMessage).where(DirectMessage.sender_id == bob.id))
    ca.post(f"/chat/dm/messages/{bad.id}/report", json={"reason": "mean"})
    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    report = db.session.scalar(select(DirectReport))
    adm.post("/admin/reports", data={"kind": "dm", "report_id": report.id, "action": "remove"})
    seen = {m["id"]: m for m in cb.get(f"/chat/dm/{thread.id}/messages").get_json()["messages"]}
    assert seen[bad.id]["removed"] is True and seen[bad.id]["body"] == ""
    assert db.session.get(DirectThread, thread.id).last_sender_id == alice.id
    # Odd JSON is a 400, not a crash.
    assert ca.post(f"/chat/dm/{thread.id}/messages", json={"body": 5}).status_code == 400
    assert ca.post(f"/chat/dm/{thread.id}/messages", json=["x"]).status_code == 400
    assert ca.post(f"/chat/dm/messages/{bad.id}/report", json={"reason": 5}).status_code == 200
    assert ca.post(f"/chat/course/{_calc(alice).id}/messages", json={"body": ["x"]}).status_code == 400
