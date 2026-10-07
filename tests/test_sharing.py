"""Opt-in sharing of decks and quizzes (services/sharing.py, blueprints/shared.py)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.extensions import db
from app.models import Card, ContentChunk, Course, Deck, LiveSession, PracticeQuiz, ShareReport, User
from app.services import sharing

from .conftest import api_token, login, make_user, sync

SLIDE = "The chain rule says multiply the outer derivative by the inner."  # file 9002's text
SYLLABUS = "Exams are 60% of the grade. Late work loses 10% per day."


def _deck(user, cards, title="Derivatives", origin="student", **fields) -> Deck:
    deck = Deck(user_id=user.id, title=title, **fields)
    deck.cards = [Card(front=f, back=b, position=i, origin=origin) for i, (f, b) in enumerate(cards)]
    db.session.add(deck)
    db.session.commit()
    return deck


OWN = [(f"term {i}", f"my own words about idea number {i}") for i in range(8)]


def _share(client, kind, item, mode="link", own_work=True):
    data = {"mode": mode}
    if own_work:
        data["own_work"] = "1"
    return client.post(f"/study/{kind}/{item.id}/share", data=data)


def _classmate(app, snapshot, manifest, name="alice"):
    """Another student who synced the same Canvas class (same school, same course id)."""
    user = make_user(name)
    mine = dict(snapshot, user={"id": f"7{len(name)}{ord(name[0])}", "name": name.title(), "short_name": name.title()})
    sync(app.test_client(), api_token(user, raw=f"hh_test_token_{name}_" + "y" * 20), mine, manifest)
    c = app.test_client()
    login(c, user)
    return user, c


def test_sharing_a_deck_by_link(app, synced_user, client):
    deck = _deck(synced_user, OWN)
    r = _share(client, "deck", deck, own_work=False)
    assert db.session.get(Deck, deck.id).share_mode == "private", "the student confirms it's their own work"
    _share(client, "deck", deck)
    deck = db.session.get(Deck, deck.id)
    assert deck.share_mode == "link" and deck.share_token and deck.shared_at
    assert all(c.share_block is None for c in deck.cards)

    # Signed out: a short preview, not indexed.
    anon = app.test_client()
    r = anon.get(f"/s/{deck.share_token}")
    assert r.status_code == 200 and "noindex" in r.headers["X-Robots-Tag"]
    page = r.get_data(as_text=True)
    assert "term 4" in page and "term 5" not in page and "3 more cards" in page
    assert "Sam" not in page and synced_user.username not in page, "owners are never named"

    # Signed in: everything, and a copy of their own.
    alice = make_user("alice")
    c = app.test_client()
    login(c, alice)
    assert "term 7" in c.get(f"/s/{deck.share_token}").get_data(as_text=True)
    r = c.post(f"/s/{deck.share_token}/copy")
    copy = db.session.scalar(select(Deck).where(Deck.user_id == alice.id))
    assert r.status_code == 302 and copy.copied_from_id == deck.id and copy.source == "copy"
    assert len(copy.cards) == 8 and {x.origin for x in copy.cards} == {"copy"} and copy.share_mode == "private"
    assert sharing.copy_count(deck) == 1

    # The copy is Alice's to study and play live, not to share as her own.
    r = c.post(f"/study/deck/{copy.id}/share", data={"mode": "link", "own_work": "1"})
    assert db.session.get(Deck, copy.id).share_mode == "private"
    assert {x.share_block for x in db.session.get(Deck, copy.id).cards} == {"not_yours"}
    r = c.post(f"/live/host-deck/{copy.id}", data={"own_material": "1"})
    assert r.status_code == 302 and "/live/" in r.headers["Location"]

    # Turning sharing off kills the link (copies stay with whoever saved them), and sharing again
    # makes a new one.
    old = deck.share_token
    _share(client, "deck", deck, mode="private")
    assert anon.get(f"/s/{old}").status_code == 404
    assert c.get(f"/s/{old}").status_code == 404
    assert db.session.get(Deck, copy.id) is not None
    _share(client, "deck", deck)
    assert db.session.get(Deck, deck.id).share_token != old and anon.get(f"/s/{old}").status_code == 404


def test_ai_cards_from_class_files_stay_private_until_rewritten(app, synced_user, client):
    f = db.session.scalar(select(ContentChunk).where(ContentChunk.source_type == "file"))
    assert f is not None, "the synced file's text is searchable material"
    from app.models import CanvasFile

    cf = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": [f"file:{cf.id}"]})
    deck = db.session.scalar(select(Deck).where(Deck.source == "ai"))
    assert {c.origin for c in deck.cards} == {"ai_files"}
    r = _share(client, "deck", deck)
    assert db.session.get(Deck, deck.id).share_mode == "private"
    assert {c.share_block for c in deck.cards} == {"ai_unedited"}

    first, second = deck.cards[0], deck.cards[1]
    original = (second.front, second.back)
    # A small fix isn't a rewrite...
    client.post(f"/study/decks/{deck.id}", data={"action": "edit", "card_id": first.id, "front": first.front + "!",
                                                 "back": first.back})
    assert not db.session.get(Card, first.id).rewritten
    # ...putting it in your own words is, and undoing that puts the card back on hold.
    edit = {"action": "edit", "card_id": second.id, "front": "Power rule in my words",
            "back": "bring the exponent down front and lower it by one"}
    client.post(f"/study/decks/{deck.id}", data=edit)
    assert db.session.get(Card, second.id).rewritten
    client.post(f"/study/decks/{deck.id}", data={"action": "edit", "card_id": second.id, "front": original[0],
                                                 "back": original[1]})
    assert not db.session.get(Card, second.id).rewritten
    client.post(f"/study/decks/{deck.id}", data=edit)
    _share(client, "deck", deck)
    deck = db.session.get(Deck, deck.id)
    assert deck.share_mode == "link"
    page = app.test_client().get(f"/s/{deck.share_token}").get_data(as_text=True)
    assert "Power rule in my words" in page and "d/dx sin x" not in page
    info = client.get(f"/study/decks/{deck.id}").get_data(as_text=True)
    assert "AI wording from your class files" in info
    assert "notes.txt" not in page, "the generator's 'Generated from <file>' line is never shown to others"

    # Exporting and importing the AI's cards doesn't make them the student's.
    exported = client.get(f"/study/decks/{deck.id}/export.txt").get_data(as_text=True)
    client.post("/study/decks/import", data={"title": "Round trip", "text": exported})
    again = db.session.scalar(select(Deck).where(Deck.title == "Round trip"))
    by_front = {c.front: c.origin for c in again.cards}
    assert by_front["d/dx sin x!"] == "ai_files" and by_front["Power rule in my words"] == "student"

    # Pasted notes are the student's own material.
    client.post("/study/generate", data={"output": "deck", "mode": "paste", "pasted": "My notes on limits " * 5})
    pasted = db.session.scalar(select(Deck).where(Deck.description == "Generated from Pasted notes"))
    assert {c.origin for c in pasted.cards} == {"ai"}


def test_word_for_word_copies_are_held_back(app, synced_user, client):
    deck = _deck(synced_user, [("Chain rule", SLIDE), ("Grading", f"From the syllabus: {SYLLABUS.upper()}"),
                               ("Chain rule (short)", "multiply outer by inner derivative")])
    _share(client, "deck", deck)
    blocks = {c.front: c.share_block for c in db.session.get(Deck, deck.id).cards}
    assert blocks == {"Chain rule": "verbatim", "Grading": "verbatim", "Chain rule (short)": None}
    page = app.test_client().get(f"/s/{deck.share_token}").get_data(as_text=True)
    assert "outer by inner" in page and "outer derivative by the inner" not in page

    # Adding a copied card to a shared deck: checked at once, never shown.
    client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": f"Copied :: {SLIDE}\nMine :: my words here"})
    added = {c.front: c.share_block for c in db.session.get(Deck, deck.id).cards}
    assert added["Copied"] == "verbatim" and added["Mine"] is None

    # A deck where everything is copied can't be shared at all.
    only = _deck(synced_user, [("x", SLIDE)], title="Only copied")
    _share(client, "deck", only)
    assert db.session.get(Deck, only.id).share_mode == "private"


def test_quiz_sharing_rules(app, synced_user, client):
    q_ok = {"question": "What's 2+2?", "choices": ["3", "4"], "answer": 1, "explanation": ""}
    q_copied = {"question": SLIDE, "choices": ["yes", "no"], "answer": 0, "explanation": ""}
    q_files = {"question": "What's 3+3?", "choices": ["6", "7"], "answer": 0, "explanation": ""}
    from_files = PracticeQuiz(user_id=synced_user.id, title="AI", questions=[q_files], source="ai", from_course_files=True)
    mixed = PracticeQuiz(user_id=synced_user.id, title="Mixed", questions=[q_ok, q_copied])
    mine = PracticeQuiz(user_id=synced_user.id, title="Mine", questions=[q_ok])
    db.session.add_all([from_files, mixed, mine])
    db.session.commit()
    _share(client, "quiz", from_files)
    assert db.session.get(PracticeQuiz, from_files.id).share_mode == "private"
    r = _share(client, "quiz", mixed)
    assert db.session.get(PracticeQuiz, mixed.id).share_mode == "private"
    assert db.session.get(PracticeQuiz, mixed.id).share_blocked == [1]
    assert "number 2" in client.get(r.headers["Location"]).get_data(as_text=True)
    _share(client, "quiz", mine)
    mine = db.session.get(PracticeQuiz, mine.id)
    assert mine.share_mode == "link"
    anon = app.test_client().get(f"/s/{mine.share_token}").get_data(as_text=True)
    assert "2+2" in anon and "Save to my quizzes" not in anon

    # Editing a shared quiz to copy class materials turns sharing off: shared whole or not at all.
    body = f"Q: What's 2+2?\n- 3\n* 4\n\nQ: {SLIDE}\n* yes\n- no"
    client.post(f"/study/quizzes/{mine.id}/edit", data={"title": "Mine", "body": body, "seconds": "20"})
    assert db.session.get(PracticeQuiz, mine.id).share_mode == "private"

    # Hosting it live is refused for the same reason.
    r = client.post(f"/live/host/{mine.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"word for word" in r.data and db.session.scalar(select(func.count(LiveSession.id))) == 0

    # A quiz shared before matching material was synced goes private when hosting finds the copy.
    later = PracticeQuiz(user_id=synced_user.id, title="Later", questions=[q_ok, {**q_ok, "question": "Novel words here "
                                                                                    "that nobody has written down yet ok"}])
    db.session.add(later)
    db.session.commit()
    _share(client, "quiz", later)
    later = db.session.get(PracticeQuiz, later.id)
    assert later.share_mode == "link"
    course = db.session.scalar(select(Course).where(Course.user_id == synced_user.id))
    db.session.add(ContentChunk(user_id=synced_user.id, course_id=course.id, source_type="upload", source_id=999, title="x",
                                text="Novel words here that nobody has written down yet ok"))
    db.session.commit()
    client.post(f"/live/host/{later.id}", data={"own_material": "1"})
    assert db.session.get(PracticeQuiz, later.id).share_mode == "private"
    assert app.test_client().get(f"/s/{later.share_token}").status_code == 404

    # A quiz pasted from an AI quiz made from class files is still one.
    body = "Q: What's 3+3?\n* 6\n- 7"
    client.post("/study/quizzes/new", data={"title": "Pasted", "body": body})
    pasted = db.session.scalar(select(PracticeQuiz).where(PracticeQuiz.title == "Pasted"))
    assert pasted.pasted_from == "files" and not pasted.from_course_files
    _share(client, "quiz", pasted)
    assert db.session.get(PracticeQuiz, pasted.id).share_mode == "private"
    # Editing the pasted question out clears it; pasting it into an existing quiz sets it.
    client.post(f"/study/quizzes/{pasted.id}/edit", data={"title": "Pasted", "body": "Q: My own?\n* yes\n- no"})
    assert db.session.get(PracticeQuiz, pasted.id).pasted_from is None
    client.post(f"/study/quizzes/{pasted.id}/edit", data={"title": "Pasted", "body": body})
    assert db.session.get(PracticeQuiz, pasted.id).pasted_from == "files"


def test_class_sharing_lists_sets_for_classmates_only(app, synced_user, client, snapshot, manifest):
    course = db.session.scalar(select(Course).where(Course.user_id == synced_user.id))
    deck = _deck(synced_user, OWN)
    _share(client, "deck", deck, mode="class")
    assert db.session.get(Deck, deck.id).share_mode == "private", "class sharing needs the deck's class"
    client.post(f"/study/decks/{deck.id}", data={"action": "course", "course_id": course.id})
    _share(client, "deck", deck, mode="class")
    assert db.session.get(Deck, deck.id).share_mode == "class"

    alice, ac = _classmate(app, snapshot, manifest)
    acourse = db.session.scalar(select(Course).where(Course.user_id == alice.id))
    assert acourse.room_key == course.room_key
    assert "Derivatives" in ac.get(f"/courses/{acourse.id}").get_data(as_text=True)
    assert "Derivatives" not in client.get(f"/courses/{course.id}").get_data(as_text=True), "not your own"

    # A student at another school with the same course id doesn't see it.
    other = dict(snapshot, base_url="https://other.instructure.com")
    bob, bc = _classmate(app, other, manifest, name="bob")
    bcourse = db.session.scalar(select(Course).where(Course.user_id == bob.id))
    assert "Derivatives" not in bc.get(f"/courses/{bcourse.id}").get_data(as_text=True)

    # Saving it files the copy under the classmate's own section.
    ac.post(f"/s/{deck.share_token}/copy")
    assert db.session.scalar(select(Deck).where(Deck.user_id == alice.id)).course_id == acourse.id

    # Moving the deck out of the class drops it to link-only.
    client.post(f"/study/decks/{deck.id}", data={"action": "course", "course_id": ""})
    assert db.session.get(Deck, deck.id).share_mode == "link"


def test_reports_hide_and_takedowns_strike(app, synced_user, client):
    decks = [_deck(synced_user, OWN, title=f"Set {i}") for i in range(3)]
    for d in decks:
        _share(client, "deck", d)
    tokens = [db.session.get(Deck, d.id).share_token for d in decks]
    assert app.test_client().get(f"/s/{tokens[0]}/report").status_code == 302, "reports need an account"
    assert client.get(f"/s/{tokens[0]}/report").status_code == 404, "not your own set"

    alice, bob = make_user("alice"), make_user("bob")
    ac, bc = app.test_client(), app.test_client()
    login(ac, alice)
    login(bc, bob)
    # A copy saved before the takedown goes with it.
    ac.post(f"/s/{tokens[0]}/copy")
    assert db.session.scalar(select(func.count(Deck.id)).where(Deck.user_id == alice.id)) == 1

    # Exam content: hidden at once.
    ac.post(f"/s/{tokens[0]}/report", data={"reason": "exam", "details": "These are from our midterm"})
    assert db.session.get(Deck, decks[0].id).share_hidden and ac.get(f"/s/{tokens[0]}").status_code == 404
    # Other reasons: hidden once two people report it.
    ac.post(f"/s/{tokens[1]}/report", data={"reason": "other"})
    ac.post(f"/s/{tokens[1]}/report", data={"reason": "other"})  # the same person twice counts once
    assert not db.session.get(Deck, decks[1].id).share_hidden
    bc.post(f"/s/{tokens[1]}/report", data={"reason": "other"})
    assert db.session.get(Deck, decks[1].id).share_hidden

    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    page = adm.get("/admin/reports").get_data(as_text=True)
    assert "Set 0" in page and "These are from our midterm" in page
    adm.post("/admin/reports", data={"kind": "deck", "item_id": decks[1].id, "action": "dismiss"})
    assert not db.session.get(Deck, decks[1].id).share_hidden and ac.get(f"/s/{tokens[1]}").status_code == 200
    adm.post("/admin/reports", data={"kind": "deck", "item_id": decks[0].id, "action": "take_down", "remove_copies": "1"})
    d0 = db.session.get(Deck, decks[0].id)
    assert d0.taken_down_at and d0.share_mode == "private"
    assert db.session.scalar(select(func.count(Deck.id)).where(Deck.user_id == alice.id)) == 0, "copies removed"
    assert db.session.get(User, synced_user.id).share_strikes == 1
    _share(client, "deck", d0)
    assert db.session.get(Deck, d0.id).share_mode == "private", "taken down for good"
    assert db.session.scalar(select(func.count(ShareReport.id)).where(ShareReport.resolved.is_(False))) == 0

    # Three strikes: no more sharing, and everything shared goes private.
    owner = db.session.get(User, synced_user.id)
    owner.share_strikes = 2
    db.session.commit()
    bc.post(f"/s/{tokens[2]}/report", data={"reason": "copyright"})
    adm.post("/admin/reports", data={"kind": "deck", "item_id": decks[2].id, "action": "take_down"})
    owner = db.session.get(User, synced_user.id)
    assert owner.sharing_blocked and owner.share_strikes == 3
    assert db.session.get(Deck, decks[1].id).share_mode == "private" and ac.get(f"/s/{tokens[1]}").status_code == 404
    fresh = _deck(synced_user, OWN, title="Fresh")
    _share(client, "deck", fresh)
    assert db.session.get(Deck, fresh.id).share_mode == "private"


def test_live_game_from_a_deck_uses_only_shareable_cards(app, synced_user, client):
    few = _deck(synced_user, OWN[:3], title="Few")
    r = client.post(f"/live/host-deck/{few.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"at least 4" in r.data
    assert client.post(f"/live/host-deck/{few.id}").status_code == 400, "the host confirms it's their own material"
    deck = _deck(synced_user, OWN[:5] + [("Copied", SLIDE)], title="Mixed")
    _deck(synced_user, [("AI card", "from the slides")], title="AI", origin="ai_files")
    r = client.post(f"/live/host-deck/{deck.id}", data={"own_material": "1"})
    assert r.status_code == 302 and "/live/" in r.headers["Location"]
    quiz = db.session.scalar(select(PracticeQuiz).where(PracticeQuiz.source == "deck"))
    assert len(quiz.questions) == 5 and all(SLIDE not in str(q) for q in quiz.questions)
    for q in quiz.questions:
        assert len(q["choices"]) == 4 and q["choices"][q["answer"]].startswith("my own words")
    assert db.session.scalar(select(LiveSession)).quiz_id == quiz.id


def test_signing_up_from_a_shared_link_comes_back_to_it(app, synced_user, client):
    deck = _deck(synced_user, OWN)
    _share(client, "deck", deck)
    token = db.session.get(Deck, deck.id).share_token
    anon = app.test_client()
    anon.get(f"/s/{token}")
    new = make_user("newbie")
    new.onboarded = False
    db.session.commit()
    login(anon, new)
    r = anon.post("/welcome", data={"display_name": "Newbie", "timezone": "America/New_York"})
    assert r.headers["Location"].endswith(f"/s/{token}")


def test_hidden_from_strangers_when_owner_cannot_share(app, synced_user, client):
    deck = _deck(synced_user, OWN)
    _share(client, "deck", deck)
    token = db.session.get(Deck, deck.id).share_token
    assert app.test_client().get(f"/s/{token}").status_code == 200
    owner = db.session.get(User, synced_user.id)
    owner.active = False
    db.session.commit()
    assert app.test_client().get(f"/s/{token}").status_code == 404
    assert app.test_client().get("/s/not-a-real-token").status_code == 404


def test_edited_enough():
    assert not sharing.edited_enough("The derivative of sin x is cos x", "the derivative of sin(x) is cos(x).")
    assert sharing.edited_enough("The derivative of sin x is cos x", "sine's slope at any point equals cosine there")


def test_reported_sets_stay_reviewable(app, synced_user, client):
    deck = _deck(synced_user, OWN)
    _share(client, "deck", deck)
    token = db.session.get(Deck, deck.id).share_token
    client.post(f"/live/host-deck/{deck.id}", data={"own_material": "1"})  # a game made before any report
    game = db.session.scalar(select(PracticeQuiz).where(PracticeQuiz.from_deck_id == deck.id))
    assert game is not None
    alice = make_user("alice")
    ac = app.test_client()
    login(ac, alice)

    # Reporting again with a more serious reason updates the report and hides the set.
    ac.post(f"/s/{token}/report", data={"reason": "other"})
    assert not db.session.get(Deck, deck.id).share_hidden
    ac.post(f"/s/{token}/report", data={"reason": "exam", "details": "midterm questions"})
    reports = db.session.scalars(select(ShareReport)).all()
    assert len(reports) == 1 and reports[0].reason == "exam" and db.session.get(Deck, deck.id).share_hidden

    # The owner can't delete it (or play it live) before review, so the report and strike can't be dodged.
    r = client.post(f"/study/decks/{deck.id}/delete", follow_redirects=True)
    assert b"be deleted until we do" in r.data and db.session.get(Deck, deck.id) is not None
    r = client.post(f"/live/host-deck/{deck.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"until we review it" in r.data and db.session.scalar(select(func.count(LiveSession.id))) == 1

    # The reporter deleting their account keeps the report (without them) in the queue.
    db.session.delete(db.session.get(User, alice.id))
    db.session.commit()
    report = db.session.scalar(select(ShareReport))
    assert report is not None and report.reporter_id is None
    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    assert "deleted account" in adm.get("/admin/reports").get_data(as_text=True)

    # The earlier game can't be hosted while its deck waits for review, or shared at all; it goes down
    # with the deck, and a second takedown adds no strike.
    r = client.post(f"/live/host/{game.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"until we review it" in r.data
    _share(client, "quiz", game)
    assert db.session.get(PracticeQuiz, game.id).share_mode == "private", "share the deck, not the game"
    for _ in range(2):
        adm.post("/admin/reports", data={"kind": "deck", "item_id": deck.id, "action": "take_down"})
    assert db.session.get(User, synced_user.id).share_strikes == 1
    assert db.session.get(PracticeQuiz, game.id).taken_down_at is not None
    r = client.post(f"/live/host/{game.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"taken down" in r.data
    assert client.post(f"/study/decks/{deck.id}/delete").status_code == 302 and db.session.get(Deck, deck.id) is None


def test_blocked_accounts_cannot_host_live(app, synced_user, client):
    deck = _deck(synced_user, OWN)
    sharing.block_sharing(db.session.get(User, synced_user.id))
    db.session.commit()
    r = client.post(f"/live/host-deck/{deck.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"host live games" in r.data and db.session.scalar(select(func.count(LiveSession.id))) == 0


def test_word_check_handles_big_and_repetitive_input(app, synced_user):
    import time

    start = time.perf_counter()
    long_ai = " ".join(["a"] * 1500)
    assert not sharing.edited_enough(long_ai, long_ai + " " + " ".join(["a b"] * 1500))
    assert not sharing.edited_enough("one two three four five six seven eight nine ten eleven",
                                     "one two three four five six seven eight nine ten eleven plus a lot of padding "
                                     "words added on the end to dilute it " * 3)
    assert time.perf_counter() - start < 1
    texts = [SLIDE] + [f"unique card number {i} with enough words to make several runs here" for i in range(3000)]
    old = sharing.MAX_RUNS_PER_PASS
    sharing.MAX_RUNS_PER_PASS = 500  # several passes over the material
    try:
        assert sharing.copied_texts(synced_user.id, texts) == {0}
    finally:
        sharing.MAX_RUNS_PER_PASS = old


def test_copies_games_and_takedowns_stay_consistent(app, synced_user, client, snapshot, manifest):
    """A classmate shares a deck that (unknown to them) copies Sam's synced slide; Sam saves it."""
    from app.blueprints.study import quiz_to_text

    bob, bc = _classmate(app, snapshot, manifest, name="bob")
    deck = _deck(bob, OWN[:5] + [("Chain rule", SLIDE)], title="Bob's set")
    _share(bc, "deck", deck)
    token = db.session.get(Deck, deck.id).share_token
    client.post(f"/s/{token}/copy")
    copy = db.session.scalar(select(Deck).where(Deck.user_id == synced_user.id, Deck.source == "copy"))

    # Sam's game from the copy leaves out the card that copies Sam's own class materials.
    client.post(f"/live/host-deck/{copy.id}", data={"own_material": "1"})
    game = db.session.scalar(select(PracticeQuiz).where(PracticeQuiz.from_deck_id == copy.id))
    assert len(game.questions) == 5 and SLIDE not in str(game.questions) and game.pasted_from == "copy"
    # Pasting the game into a new quiz doesn't make the questions Sam's.
    client.post("/study/quizzes/new", data={"title": "Laundered", "body": quiz_to_text(game)})
    laundered = db.session.scalar(select(PracticeQuiz).where(PracticeQuiz.title == "Laundered"))
    assert laundered.pasted_from == "copy"
    _share(client, "quiz", laundered)
    assert db.session.get(PracticeQuiz, laundered.id).share_mode == "private"

    # One open report, even one that doesn't hide the set, stops live games from it and its copies.
    alice = make_user("alice")
    ac = app.test_client()
    login(ac, alice)
    ac.post(f"/s/{token}/report", data={"reason": "personal", "details": "a name on card 3"})
    assert not db.session.get(Deck, deck.id).share_hidden
    r = client.post(f"/live/host/{game.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"until we review it" in r.data
    report = db.session.scalar(select(ShareReport))
    assert [c[0] for c in report.snapshot][:1] == ["term 0"], "what was shared is kept with the report"

    # Taking Bob's deck down removes Sam's copy and the game Sam made from it.
    admin = make_user("admin", is_admin=True)
    adm = app.test_client()
    login(adm, admin)
    adm.post("/admin/reports", data={"kind": "deck", "item_id": deck.id, "action": "take_down"})
    assert db.session.get(Deck, copy.id) is None and db.session.get(PracticeQuiz, game.id) is None
    assert adm.post("/admin/reports", data={"kind": "deck", "item_id": "²", "action": "take_down"}).status_code == 302

    # Bob can't bring it back by exporting and importing it.
    exported = bc.get(f"/study/decks/{deck.id}/export.txt").get_data(as_text=True)
    bc.post("/study/decks/import", data={"title": "Back again", "text": exported})
    again = db.session.scalar(select(Deck).where(Deck.title == "Back again"))
    assert {c.origin for c in again.cards} == {"removed"}
    _share(bc, "deck", again)
    assert db.session.get(Deck, again.id).share_mode == "private"
    r = bc.post(f"/live/host-deck/{again.id}", data={"own_material": "1"}, follow_redirects=True)
    assert b"at least 4" in r.data


def test_generated_titles_never_name_the_source(app, synced_user, client, fake_ai):
    import json

    from app.models import CanvasFile

    original = fake_ai.complete

    def no_title(**kw):
        result = original(**kw)
        data = json.loads(result.text)
        data["title"] = ""
        result.text = json.dumps(data)
        return result

    fake_ai.complete = no_title
    cf = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": [f"file:{cf.id}"]})
    deck = db.session.scalar(select(Deck).where(Deck.source == "ai"))
    assert deck.title == "Flashcards"


def test_rewrite_rule():
    photo = ("Photosynthesis\nThe process by which green plants use sunlight, water and carbon dioxide to make glucose "
             "and release oxygen.")
    assert not sharing.edited_enough(photo, photo.replace("by which", "through which").replace("sunlight", "light")
                                     .replace("make", "create")), "a few synonyms isn't a rewrite"
    assert sharing.edited_enough(photo, "Photosynthesis\nPlants turn light, CO2 and H2O into sugar, giving off O2")
    assert sharing.edited_enough("ATP\nEnergy", "ATP\nThe molecule cells use to store and move power")
