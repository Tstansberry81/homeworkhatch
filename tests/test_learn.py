"""Learn mode, Test mode, spaced repetition, and Quizlet / Anki / CSV import and export.

Synthetic card content only (Spanish vocab and invented biology), never real course material.
"""

import random
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import Card, CoinTransaction, Deck, DeckTest, StudyPlan, utcnow
from app.services import cards_io, learn, srs

from .conftest import login, make_user

# ---------------------------------------------------------------- parsing


def test_quizlet_tab_export_with_multiline_definitions():
    text = ("el gato\tthe cat\n"
            "escribir\tto write\n(also: to spell)\n\n"
            "la manzana\tthe apple\r\n")
    cards, skipped, detected = cards_io.parse_cards(text)
    assert detected == "quizlet"
    assert cards == [("el gato", "the cat"), ("escribir", "to write\n(also: to spell)"), ("la manzana", "the apple")]
    assert skipped == []


def test_quizlet_comma_semicolon_and_custom_separators():
    cards, _, detected = cards_io.parse_cards("perro,the dog;gato,the cat, a pet;")
    assert detected == "quizlet"
    assert cards == [("perro", "the dog"), ("gato", "the cat, a pet")], "split on the first comma only"
    cards, _, _ = cards_io.parse_cards("río,the river\nbiblioteca,the library")
    assert cards == [("río", "the river"), ("biblioteca", "the library")]
    # Custom separators chosen by hand, including escapes typed as \t and \n.
    cards, skipped, _ = cards_io.parse_cards("año - the year ## cuchara - the spoon ## junk", term_sep=" - ", card_sep=" ## ")
    assert cards == [("año", "the year"), ("cuchara", "the spoon")]
    assert skipped == ["junk"]
    cards, _, _ = cards_io.parse_cards("a|b\nc|d", term_sep="custom", card_sep="newline") or ([], [], "")
    assert cards == [], "'custom' without a custom value isn't a separator"
    cards, _, _ = cards_io.parse_cards("a|b\nc|d", term_sep="|", card_sep="\\n")
    assert cards == [("a", "b"), ("c", "d")]
    cards, _, _ = cards_io.parse_cards("x\ty;z\tw", term_sep="tab", card_sep="semicolon")
    assert cards == [("x", "y"), ("z", "w")]


def test_anki_plain_text_with_headers_tags_html_and_cloze():
    text = ("#separator:tab\n#html:true\n#notetype:Cloze\n#deck:Invented biology\n#tags column:3\n"
            "The <b>glimmerase</b> enzyme\tturns starlight<br>into sugar &amp; light\tbio::enzymes\n"
            "{{c1::Floraxin}} is made in the {{c2::petal vault::organelle}}\tSee chapter 0\tbio\n"
            "\"quoted\tfront\"\t\"back with \"\"quotes\"\"\"\ttag\n"
            "lonely field\t\ttag\n")
    cards, skipped, detected = cards_io.parse_cards(text)
    assert detected == "anki"
    assert cards[0] == ("The glimmerase enzyme", "turns starlight\ninto sugar & light")
    assert ("[...] is made in the petal vault", "Floraxin is made in the petal vault\n\nSee chapter 0") in cards
    assert ("Floraxin is made in the [organelle]", "Floraxin is made in the petal vault\n\nSee chapter 0") in cards
    assert ("quoted\tfront", 'back with "quotes"') in cards
    assert all("bio" not in back for _, back in cards[:1]), "the tags column is dropped"
    assert len(cards) == 4 and skipped == ["lonely field tag"], "skipped lines are tidied for display"


def test_anki_separators_and_cloze_without_headers():
    cards, _, detected = cards_io.parse_cards("#separator:pipe\nmoon-fern|a fern that glows\nzorbic acid|pH 2")
    assert detected == "anki" and cards == [("moon-fern", "a fern that glows"), ("zorbic acid", "pH 2")]
    cards, _, _ = cards_io.parse_cards("#separator:Semicolon\n#html:false\na;<b>kept as text</b>")
    assert cards == [("a", "<b>kept as text</b>")], "#html:false leaves the text alone"
    cards, _, detected = cards_io.parse_cards("The {{c1::quill cell}} stores ink.")
    assert detected == "anki" and cards == [("The [...] stores ink.", "The quill cell stores ink.")]


def test_csv_with_quotes_and_header():
    text = 'term,definition\n"zorbic acid","a made-up acid, pH 2"\n"quill cell","stores ink\nin the squid"\nonly-one-column\n'
    cards, skipped, detected = cards_io.parse_cards(text)
    assert detected == "csv"
    assert cards == [("zorbic acid", "a made-up acid, pH 2"), ("quill cell", "stores ink\nin the squid")]
    assert skipped == ["only-one-column"]
    cards, _, detected = cards_io.parse_cards('"moon-fern","glows, faintly"\n"sun-moss","hums"')
    assert detected == "csv" and cards[0] == ("moon-fern", "glows, faintly")


def test_legacy_lines_and_skipped_garbage():
    cards, skipped, detected = cards_io.parse_cards("hola :: hello\nadios\tgoodbye\nbroken line\n :: no front")
    assert detected == "lines"
    assert cards == [("hola", "hello"), ("adios", "goodbye")]
    assert "broken line" in skipped and len(skipped) == 2
    cards, skipped, detected = cards_io.parse_cards("just some words\nand more words")
    assert cards == [] and detected == "unknown" and len(skipped) == 2
    assert cards_io.parse_cards("   \n ") == ([], [], "empty")
    # Leading lines before the first Quizlet card can't be continuation lines: reported, not glued.
    cards, skipped, _ = cards_io.parse_cards("My set title\nel gato\tthe cat\nel perro\tthe dog")
    assert cards == [("el gato", "the cat"), ("el perro", "the dog")] and skipped == ["My set title"]


def test_parser_limits():
    many = "\n".join(f"word {i}\tmeaning {i}" for i in range(cards_io.MAX_CARDS + 5))
    cards, skipped, _ = cards_io.parse_cards(many)
    assert len(cards) == cards_io.MAX_CARDS
    assert any("5 more cards" in s for s in skipped)
    cards, skipped, _ = cards_io.parse_cards("x" * 2500 + "\t" + "y" * 5000)
    assert len(cards[0][0]) == cards_io.MAX_FRONT and len(cards[0][1]) == cards_io.MAX_BACK
    assert any("Trimmed" in s for s in skipped)


def test_old_deck_parser_still_works():
    from app.blueprints.study import _parse_cards

    assert _parse_cards("a :: 1\nb :: 2") == [("a", "1"), ("b", "2")]
    assert _parse_cards(None) == []


def test_export_round_trips():
    original = [("el gato", "the cat"), ("tab\there", "new\nline"), ('quote "x"', "comma, here"),
                ("#hashtag front", "back"), ("$\\sin x$", "$\\cos x$"), ("ñandú", "rhea")]
    for exporter in (cards_io.export_csv, cards_io.export_anki):
        out = exporter(original)
        cards, skipped, _ = cards_io.parse_cards(out)
        assert cards == original, exporter.__name__
        assert skipped == []
    anki = cards_io.export_anki(original)
    assert anki.startswith("#separator:tab\n#html:false\n#columns:Front\tBack\n")
    assert '"#hashtag front"' in anki, "Anki would read an unquoted # line as a comment"
    assert cards_io.export_csv(original).startswith("front,back\n")


# ---------------------------------------------------------------- answer checking and distractors


def test_lenient_and_strict_answer_checking():
    assert learn.check_answer("  The Cat! ", "the cat") == "right"
    assert learn.check_answer("el rio", "el río") == "right", "accents don't count"
    assert learn.check_answer("cos(x)", "$\\cos x$") == "right", "spaces and punctuation don't count"
    assert learn.check_answer("the cta", "the cat") == "almost", "a swapped pair is one typo"
    assert learn.check_answer("libary", "library") == "almost"
    assert learn.check_answer("-2", "2") == "wrong" and learn.check_answer("314", "3.14") == "wrong"
    assert learn.check_answer("1946", "1947") == "wrong", "numbers are never 'almost'"
    assert learn.check_answer("dog", "cat") == "wrong" and learn.check_answer("", "cat") == "wrong"
    assert learn.check_answer("el rio", "el río", strict=True) == "wrong"
    assert learn.check_answer("EL RÍO ", "el río", strict=True) == "right"
    assert learn.check_answer("libary", "library", strict=True) == "wrong"


def test_distractors_come_from_the_set_and_numbers_get_numbers():
    rng = random.Random(1)
    answers = ["the cat", "the dog", "the cat", "the river", "the apple", "1947"]
    options = learn.pick_options(0, answers, rng)
    assert len(options) == 3 and 2 not in options and 0 not in options, "no duplicate of the right answer"
    numeric = learn.pick_options(5, answers, rng)
    assert all(isinstance(o, str) and learn.is_number(o) for o in numeric), numeric
    assert "1947" not in numeric
    assert learn.pick_options(0, ["only"], rng) == []
    assert set(learn.pick_options(0, ["2.5", "1,200", "7"], random.Random(3))) <= {1, 2} | {"2.4", "2.6", "2.3", "2.7",
                                                                                        "2.2", "2.8", "2.0", "3.0", "1.5", "3.5"}


# ---------------------------------------------------------------- SM-2


def _card(**kw) -> Card:
    defaults = dict(front="f", back="b", position=0, ease=2.5, interval_days=0, repetitions=0, lapses=0,
                    review_count=0, due_at=datetime(2026, 1, 1), last_reviewed_at=None)
    defaults.update(kw)
    return Card(**defaults)


def test_sm2_intervals_lapse_and_ease_floor():
    now = datetime(2026, 3, 1, 12)
    c = _card()
    srs.grade(c, True, now=now)
    assert (c.repetitions, c.interval_days, c.review_count) == (1, 1, 1)
    assert c.due_at == now + timedelta(days=1) and c.last_reviewed_at == now
    now += timedelta(days=1)
    srs.grade(c, True, now=now)
    assert c.interval_days == 3
    now += timedelta(days=3)
    srs.grade(c, True, now=now)
    assert c.interval_days == round(3 * c.ease) == 8
    now += timedelta(days=8)
    srs.grade(c, True, almost=True, now=now)
    assert c.ease == pytest.approx(2.36) and c.interval_days == round(8 * 2.36)
    # A lapse: back in 10 minutes, repetitions reset, ease drops.
    now += timedelta(days=c.interval_days)
    srs.grade(c, False, now=now)
    assert (c.repetitions, c.interval_days, c.lapses) == (0, 0, 1)
    assert c.due_at == now + timedelta(minutes=10)
    assert c.ease == pytest.approx(2.36 - 0.54)
    for _ in range(5):  # forgetting again and again never pushes ease under 1.3
        now += timedelta(days=2)
        srs.grade(c, True, now=now)
        now += timedelta(days=2)
        srs.grade(c, False, now=now)
    assert c.ease == srs.EASE_FLOOR
    assert c.review_count == 15


def test_sm2_new_cards_and_same_session_repeats():
    now = datetime(2026, 3, 1, 12)
    c = _card()
    srs.grade(c, False, now=now)
    assert c.lapses == 0 and c.ease == 2.5, "missing a brand-new card isn't a lapse"
    srs.grade(c, True, now=now + timedelta(minutes=11))
    assert c.repetitions == 1 and c.interval_days == 1
    srs.grade(c, True, now=now + timedelta(minutes=12))  # typed right after multiple choice
    assert c.repetitions == 1 and c.interval_days == 1 and c.review_count == 3, "counted, not stretched"


def test_sm2_exam_clamp():
    now = datetime(2026, 3, 1, 12)
    c = _card(repetitions=5, interval_days=30, ease=2.5, review_count=5, last_reviewed_at=now - timedelta(days=30))
    exam = now + timedelta(days=10, hours=3)
    srs.grade(c, True, now=now, exam_at=exam)
    assert c.interval_days == 5 and c.due_at < exam - timedelta(days=1)
    c2 = _card(repetitions=3, interval_days=10, review_count=3, last_reviewed_at=now - timedelta(days=10))
    srs.grade(c2, True, now=now, exam_at=now + timedelta(hours=30))
    assert c2.interval_days == 1
    c3 = _card(repetitions=3, interval_days=10, review_count=3, last_reviewed_at=now - timedelta(days=10))
    srs.grade(c3, True, now=now, exam_at=now - timedelta(days=1))
    assert c3.interval_days == 25, "a past exam doesn't clamp"


def test_due_queue_caps_and_spreads_a_backlog():
    now = datetime(2026, 3, 1, 12)
    overdue = [_card(position=i, repetitions=2, interval_days=3, review_count=2, last_reviewed_at=now - timedelta(days=40),
                     due_at=now - timedelta(days=30 - i % 30)) for i in range(300)]
    relearn = _card(position=999, repetitions=0, review_count=4, last_reviewed_at=now - timedelta(minutes=20),
                    due_at=now - timedelta(minutes=10))
    later = _card(position=1000, repetitions=3, review_count=3, last_reviewed_at=now, due_at=now + timedelta(days=4))
    new = [_card(position=2000 + i) for i in range(20)]
    cards = overdue + [relearn, later] + new
    queue = srs.due_queue(cards, now)
    assert len(queue) == srs.DAILY_CAP == 150
    assert queue[0] is relearn, "missed-recently cards first"
    assert queue[1].due_at == min(c.due_at for c in overdue), "then most overdue first"
    assert later not in queue and not any(c in queue for c in new), "a backlog leaves no room for new cards today"
    assert len(srs.due_queue(cards, now, reviewed_today=100)) == 50
    assert srs.due_queue(cards, now, reviewed_today=500) == []
    light = [relearn] + new
    assert len(srs.due_queue(light, now, new_per_day=5)) == 6
    assert srs.not_due(cards, now) == [later]
    assert srs.readiness([later, relearn]) == 50


# ---------------------------------------------------------------- routes


def _deck(user, title="Spanish", n=8, plan=None, **kw) -> Deck:
    vocab = [("el gato", "the cat"), ("el perro", "the dog"), ("la manzana", "the apple"), ("el río", "the river"),
             ("escribir", "to write"), ("la ventana", "the window"), ("correr", "to run"), ("el año", "the year"),
             ("la cuchara", "the spoon"), ("la biblioteca", "the library")]
    deck = Deck(user_id=user.id, title=title, plan_id=plan.id if plan else None,
                cards=[Card(front=f, back=b, position=i) for i, (f, b) in enumerate(vocab[:n])], **kw)
    db.session.add(deck)
    db.session.commit()
    return deck


def _plan(user, days=4, **kw) -> StudyPlan:
    plan = StudyPlan(user_id=user.id, title="Calc Midterm 2", kind="midterm", exam_at=utcnow() + timedelta(days=days), **kw)
    db.session.add(plan)
    db.session.commit()
    return plan


@pytest.fixture
def student(app, client):
    user = make_user()
    login(client, user)
    return user


def test_import_creates_a_deck_linked_to_an_exam(student, client):
    plan = _plan(student)
    page = client.get(f"/study/decks/import?plan={plan.id}").get_data(as_text=True)
    assert "Which exam is this for?" in page and "Calc Midterm 2" in page and "hh_import_text" not in page
    r = client.post("/study/decks/import", data={
        "title": "Imported vocab", "plan_id": str(plan.id), "term_sep": "auto", "card_sep": "auto",
        "text": "el gato\tthe cat\nescribir\tto write\n(also: to spell)\nla manzana\tthe apple"})
    assert r.status_code == 302
    deck = db.session.scalar(select(Deck).where(Deck.title == "Imported vocab"))
    assert deck.source == "import" and deck.plan_id == plan.id and deck.user_id == student.id
    assert [(c.front, c.back) for c in deck.cards][1] == ("escribir", "to write\n(also: to spell)")
    r = client.post("/study/decks/import", data={"title": "Empty", "text": "no separators here"})
    assert r.status_code == 400 and b"couldn" in r.data
    other_plan = _plan(make_user("mallory"))
    r = client.post("/study/decks/import", data={"title": "Sneaky", "plan_id": str(other_plan.id), "text": "a\tb"})
    assert r.status_code == 404, "can't attach a deck to someone else's exam"


def test_import_preview_endpoint(student, client):
    r = client.post("/study/decks/import/preview", json={"text": "uno\tone\ndos\ttwo\nbasura", "term_sep": "auto"})
    body = r.get_json()
    assert body["count"] == 2 and body["detected"] == "quizlet" and body["label"] == "Quizlet export"
    assert body["cards"][0] == {"front": "uno", "back": "one"}
    r = client.post("/study/decks/import/preview", json={"text": "uno|one", "term_sep": "custom", "term_sep_custom": "|"})
    assert r.get_json()["count"] == 1
    assert client.post("/study/decks/import/preview", data="nope").status_code == 400


def test_export_downloads(student, client):
    deck = _deck(student, title="Spanish / básico")
    r = client.get(f"/study/decks/{deck.id}/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"] and r.headers["Cache-Control"] == "no-store"
    assert cards_io.parse_cards(r.get_data(as_text=True))[0] == [(c.front, c.back) for c in deck.cards]
    r = client.get(f"/study/decks/{deck.id}/export.txt")
    assert r.get_data(as_text=True).startswith("#separator:tab")
    assert client.get(f"/study/decks/{deck.id}/export.pdf").status_code == 404
    other = app_client_for(client, "mallory")
    assert other.get(f"/study/decks/{deck.id}/export.csv").status_code == 404


def app_client_for(client, username):
    c2 = client.application.test_client()
    login(c2, make_user(username))
    return c2


def test_star_toggle_and_owner_checks(student, client):
    deck = _deck(student)
    card = deck.cards[0]
    assert client.post(f"/study/cards/{card.id}/star").get_json() == {"ok": True, "starred": True}
    assert client.post(f"/study/cards/{card.id}/star", json={"starred": True}).get_json()["starred"] is True
    assert client.post(f"/study/cards/{card.id}/star").get_json()["starred"] is False
    other = app_client_for(client, "mallory")
    assert other.post(f"/study/cards/{card.id}/star").status_code == 404
    db.session.refresh(card)
    assert card.starred is False
    # Answering for someone else's cards changes nothing.
    r = other.post("/study/learn/answers", json={"answers": [{"card_id": c.id, "correct": True} for c in deck.cards]})
    assert r.get_json()["graded"] == 0
    db.session.refresh(card)
    assert card.review_count == 0
    assert other.get(f"/study/learn?deck={deck.id}").status_code == 404
    assert other.get(f"/study/learn?card={card.id}").status_code == 404


def test_deck_page_links_and_exam_select(student, client):
    plan = _plan(student)
    deck = _deck(student)
    page = client.get(f"/study/decks/{deck.id}").get_data(as_text=True)
    for bit in (f"/study/learn?deck={deck.id}", f"/study/decks/{deck.id}/review", f"/study/test?deck={deck.id}",
                "export.csv", "export.txt", "Which exam is this for?", "data-star-url"):
        assert bit in page, bit
    client.post(f"/study/decks/{deck.id}", data={"action": "plan", "plan_id": str(plan.id)})
    assert db.session.get(Deck, deck.id).plan_id == plan.id
    client.post(f"/study/decks/{deck.id}", data={"action": "plan", "plan_id": ""})
    assert db.session.get(Deck, deck.id).plan_id is None


def test_learn_page_for_deck_plan_cards_and_starred(app, student, client):
    plan = _plan(student, days=3)
    deck = _deck(student, plan=plan)
    _deck(student, title="Second deck for the exam", n=3, plan=plan)
    page = client.get(f"/study/learn?deck={deck.id}").get_data(as_text=True)
    assert 'id="learn-data"' in page and "learn.js" in page and "Calc Midterm 2" in page and "% ready" in page
    data = _learn_data(page)
    assert len(data["cards"]) == 8 and data["today"] == 8 and data["answerWith"] == "definition"
    assert all(len(c["o"]) == 3 for c in data["cards"]), "three distractors from the same set"
    plan_page = client.get(f"/study/learn?plan={plan.id}&answer_with=term").get_data(as_text=True)
    assert len(_learn_data(plan_page)["cards"]) == 11 and _learn_data(plan_page)["answerWith"] == "term"
    picked = _learn_data(client.get(f"/study/learn?deck={deck.id}&card={deck.cards[2].id}&card={deck.cards[5].id}")
                         .get_data(as_text=True))
    assert [c["id"] for c in picked["cards"]] == [deck.cards[2].id, deck.cards[5].id]
    deck.cards[4].starred = True
    db.session.commit()
    starred = _learn_data(client.get(f"/study/learn?deck={deck.id}&starred=1").get_data(as_text=True))
    assert [c["id"] for c in starred["cards"]] == [deck.cards[4].id]
    # The flip viewer's "don't know" list can be too long for a URL: same page by POST.
    r = client.post("/study/learn", data={"deck": str(deck.id), "card": [str(deck.cards[0].id)]})
    assert r.status_code == 200 and len(_learn_data(r.get_data(as_text=True))["cards"]) == 1
    assert client.get("/study/learn").status_code == 302
    # Card text is data, not markup: escaped in the JSON, sanitized in the HTML copies.
    deck.cards[0].front = "</script><script>alert(1)</script>"
    db.session.commit()
    page = client.get(f"/study/learn?deck={deck.id}").get_data(as_text=True)
    assert "<script>alert(1)" not in page
    # A session that isn't yours (or doesn't exist): no link back to it.
    page = client.get(f"/study/learn?deck={deck.id}&session=12").get_data(as_text=True)
    assert "Back to your study session" not in page


def test_learn_page_links_back_to_the_study_session(app, client):
    from app.models import StudySession

    student = make_user()
    login(client, student)
    plan = _plan(student, days=6)
    session = StudySession(plan_id=plan.id, user_id=student.id, day="2026-10-05", role="learn", minutes=30)
    db.session.add(session)
    db.session.commit()
    deck = _deck(student, plan=plan)
    page = client.get(f"/study/learn?deck={deck.id}&session={session.id}").get_data(as_text=True)
    assert "Back to your study session" in page and f"/study/exams/session/{session.id}" in page
    assert f"session={session.id}" in page, "Test keeps the session too"


def _learn_data(page: str) -> dict:
    import json

    raw = re.search(r'<script type="application/json" id="learn-data">(.*?)</script>', page, re.S).group(1)
    return json.loads(raw)


def test_learn_answers_update_sm2_and_pay_coins_once_a_day(student, client):
    plan = _plan(student, days=6)
    deck = _deck(student, plan=plan)
    cards = deck.cards
    r = client.post("/study/learn/answers", json={"answers": [
        {"card_id": cards[0].id, "correct": True}, {"card_id": cards[1].id, "correct": False},
        {"card_id": cards[2].id, "correct": True, "almost": True}, {"card_id": cards[3].id, "correct": "yes"}]})
    assert r.get_json() == {"ok": True, "graded": 4, "coins": False}, "fewer than 5 answers: no coins"
    for c in cards[:4]:
        db.session.refresh(c)
    assert cards[0].repetitions == 1 and cards[0].review_count == 1 and cards[0].last_reviewed_at is not None
    assert cards[1].repetitions == 0 and cards[1].due_at <= utcnow() + timedelta(minutes=10)
    assert cards[2].ease == pytest.approx(2.36)
    assert cards[3].repetitions == 0, "only a real true counts as correct"
    r = client.post("/study/learn/answers", json=[{"card_id": c.id, "correct": True} for c in cards[3:8]])
    assert r.get_json()["coins"] is True
    r = client.post("/study/learn/answers", json={"answers": [{"card_id": c.id, "correct": True} for c in cards]})
    assert r.get_json()["coins"] is False, "once a day"
    assert db.session.query(CoinTransaction).filter(CoinTransaction.ref.like("cards:%")).count() == 1
    assert client.post("/study/learn/answers", json={"nope": 1}).status_code == 400
    # Achievements read review_count, which Learn now writes.
    from app.services.coins import achievements

    shark = next(a for a in achievements(student) if a.key == "cards_100")
    assert shark.progress.startswith(str(sum(c.review_count for c in db.session.get(Deck, deck.id).cards)))


def test_learn_answers_clamp_to_the_exam(student, client):
    plan = _plan(student, days=4)
    deck = _deck(student, plan=plan)
    card = deck.cards[0]
    card.repetitions, card.interval_days, card.review_count = 4, 20, 4
    card.last_reviewed_at = utcnow() - timedelta(days=20)
    db.session.commit()
    client.post("/study/learn/answers", json={"answers": [{"card_id": card.id, "correct": True}]})
    db.session.refresh(card)
    assert card.interval_days <= 2 and card.due_at < plan.exam_at


def _test_form(client, url):
    page = client.get(url).get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    return page, token


def test_test_mode_is_scored_on_the_server(app, student, client):
    from app.blueprints.study import _test_signer

    plan = _plan(student)
    deck = _deck(student, n=10, plan=plan)
    page, token = _test_form(client, f"/study/test?plan={plan.id}&n=10&minutes=7")
    assert page.count('class="card test-q"') == 10 and 'id="timer"' in page and "data-seconds=\"420\"" in page
    with app.test_request_context():
        questions = _test_signer().loads(token)["q"]
    kinds = {q["k"] for q in questions}
    assert kinds <= {"mc", "typed", "tf"} and len(kinds) >= 2
    by_id = {c.id: c for c in deck.cards}
    form = {"token": token, "plan": str(plan.id)}
    right = 0
    for i, q in enumerate(questions):
        card = by_id[q["c"]]
        want_right = i % 2 == 0
        if q["k"] == "mc":
            correct_index = next(n for n, o in enumerate(q["o"]) if o == card.id)
            form[f"a{i}"] = str(correct_index if want_right else (correct_index + 1) % len(q["o"]))
        elif q["k"] == "tf":
            truth = q["s"] == card.id
            form[f"a{i}"] = "true" if truth == want_right else "false"
        else:
            form[f"a{i}"] = (card.back.upper() + "!") if want_right else "something else"
        # A client "claiming" correctness changes nothing: the server never reads these.
        form[f"correct{i}"] = "true"
        right += want_right
    r = client.post("/study/test", data=form)
    assert r.status_code == 302 and "/study/tests/" in r.headers["Location"]
    test = db.session.scalar(select(DeckTest))
    assert (test.score, test.total) == (right, 10) and test.plan_id == plan.id and test.user_id == student.id
    assert all(set(a) >= {"card_id", "kind", "correct", "given"} for a in test.answers)
    result = client.get(r.headers["Location"]).get_data(as_text=True)
    assert f"{right} / 10" in result and "What you missed" in result and "Star the ones I missed" in result
    missed = [a["card_id"] for a in test.answers if not a["correct"]]
    client.post(f"/study/tests/{test.id}/star", data={"plan": str(plan.id)})
    assert {c.id for c in db.session.get(Deck, deck.id).cards if c.starred} == set(missed)
    assert db.session.query(CoinTransaction).filter(CoinTransaction.ref.like("cards:%")).count() == 1
    # Tampered or expired tokens aren't scored.
    r = client.post("/study/test", data={"token": token[:-3] + "abc", "a0": "0"})
    assert r.status_code == 302 and db.session.query(DeckTest).count() == 1
    other = app_client_for(client, "mallory")
    assert other.get(f"/study/tests/{test.id}").status_code == 404
    assert other.post(f"/study/tests/{test.id}/star").status_code == 404
    assert other.post("/study/test", data=form).status_code == 302
    assert db.session.query(DeckTest).count() == 1, "a test token only works for the student it was made for"


def test_test_mode_answer_with_term_and_small_sets(student, client):
    deck = _deck(student, n=4)
    page, _ = _test_form(client, f"/study/test?deck={deck.id}&answer_with=term&n=99")
    assert page.count('class="card test-q"') == 4, "one question per card at most"
    tiny = _deck(student, title="Tiny", n=1)
    r = client.get(f"/study/test?deck={tiny.id}")
    assert r.status_code == 302


def test_free_learn_page_and_preview_work_signed_out(app, client):
    landing = client.get("/").get_data(as_text=True)
    assert "/free-learn" in landing
    page = client.get("/free-learn").get_data(as_text=True)
    assert "Quizlet&#39;s Learn mode, free" in page or "Quizlet's Learn mode, free" in page
    assert "Copy text" in page and "Notes in Plain Text" in page and "data-keep-text" in page
    r = client.post("/free-learn/preview", json={"text": "\n".join(f"palabra {i}\tword {i}" for i in range(80))})
    body = r.get_json()
    assert r.status_code == 200 and body["count"] == 80 and len(body["cards"]) == 50
    r = client.post("/free-learn/preview", data="x" * (200 * 1024 + 1), content_type="application/json")
    assert r.status_code == 413
    assert client.post("/free-learn/preview", data="not json", content_type="application/json").status_code == 400
    assert db.session.query(Deck).count() == 0, "the public preview stores nothing"
    # Signed in, the page sends you straight to the import page.
    login(client, make_user())
    assert client.get("/free-learn").headers["Location"].endswith("/study/decks/import")


def test_free_learn_preview_needs_no_csrf_token(app):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        c = app.test_client()
        assert c.post("/free-learn/preview", json={"text": "a\tb"}).status_code == 200
        assert c.post("/study/decks/import/preview", json={"text": "a\tb"}).status_code in (302, 400), \
            "other endpoints keep their CSRF (and login) checks"
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_study_tabs_highlight_import(student, client):
    def active_tabs(path):
        nav = re.search(r'<nav class="tabs".*?</nav>', client.get(path).get_data(as_text=True), re.S).group(0)
        return re.findall(r'<a href="([^"]+)" class="active"', nav)

    assert active_tabs("/study/decks/import") == ["/study/decks/import"]
    assert active_tabs("/study/") == ["/study/"]
    assert active_tabs("/study/generate") == ["/study/generate"]


def test_import_separator_panel_opens_only_when_picked(student, client):
    assert 'class="import-seps" open' not in client.get("/study/decks/import").get_data(as_text=True)
    page = client.post("/study/decks/import", data={"title": "x", "text": "zz", "term_sep": "custom",
                                                    "term_sep_custom": "|"}).get_data(as_text=True)
    assert 'class="import-seps" open' in page and 'value="|"' in page, "a failed save keeps the chosen separators"
