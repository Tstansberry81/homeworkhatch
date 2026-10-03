"""Learn mode, Test mode, spaced repetition, and Quizlet / Anki / CSV import and export.

Synthetic card content only (Spanish vocab and invented biology), never real course material.
"""

import json
import random
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import Card, CoinTransaction, Deck, DeckTest, PracticeQuiz, StudyPlan, utcnow
from app.services import cards_io, learn, srs

from .conftest import login, make_user

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

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
    parsed = cards_io.parse(many)
    assert len(parsed.cards) == cards_io.MAX_CARDS and parsed.dropped == 5 and parsed.total == cards_io.MAX_CARDS + 5
    assert parsed.skipped == [], "cards left out by the limit aren't 'lines we couldn't read'"
    assert cards_io.parse_cards(many)[0] == parsed.cards
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


# ---------------------------------------------------------------- fixes from the adversarial review
# Timings assert generous bounds (the fixed code runs these in milliseconds; before, they took
# seconds to days), so they don't flake on a slow machine.


def _seconds(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


def _node(mode: str, payload: dict):
    out = subprocess.run([NODE, str(ROOT / "tests/js/learn_client.mjs"), mode], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


ADVERSARIAL_PASTES = {  # about 200 KB each
    "unclosed clozes": "{{c1::" * 33_000,  # cubic before: 12 KB took two minutes
    "cloze separators": "{{c1::" + "::" * 100_000,
    "cloze hints": "{{c1::a::" * 22_000,
    "cloze digits": "{{c" + "1" * 200_000,
    "open tags": "#separator:tab\n" + "<b " * 66_000,
    "trailing spaces": "#separator:tab\n#html:true\nf\t" + " " * 200_000 + "x",
    "open sounds": "#separator:tab\n#html:true\nf\t" + "[sound:" * 28_000,
    "open list items": "#separator:tab\n#html:true\nf\t" + "<li " * 50_000,
    "open breaks": "#separator:tab\n#html:true\nf\t<br" + " " * 200_000,
    "quoted csv": ',"a"b' * 40_000,
    "lone quote": ',"' + "x" * 200_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_PASTES))
def test_parser_is_linear_on_adversarial_pastes(name):
    text = ADVERSARIAL_PASTES[name]
    assert len(text) >= 150_000
    _, seconds = _seconds(cards_io.parse, text)
    assert seconds < 1, f"{name}: {seconds:.2f} s"


def test_long_quizlet_definitions_are_joined_once():
    text = "a\tb\n" + "x\n" * 1_000_000  # 2 MB of continuation lines: quadratic before (about 30 s)
    parsed, seconds = _seconds(cards_io.parse, text)
    assert seconds < 2 and len(parsed.cards) == 1 and cards_io.MAX_BACK - 1 <= len(parsed.cards[0][1]) <= cards_io.MAX_BACK


def test_cloze_pattern_keeps_its_groups():
    assert cards_io.CLOZE.search("{{c3::a::b::c}}").groups() == ("3", "a", "b::c")
    assert cards_io.CLOZE.search("$x^{2}$ and {{c1::$x^{2}$}}").groups() == ("1", "$x^{2}$", None)
    assert cards_io.cloze_cards("{{c1::Paris}} is in {{c2::France::country}}") == [
        ["[...] is in France", "Paris is in France"], ["Paris is in [country]", "Paris is in France"]]


def test_cloze_expansion_and_parsing_stop_at_the_card_limit():
    one_note = "".join(f"{{{{c{i}::abcdefghijkl}}}}" for i in range(1, 9000))  # 200 KB, 8,999 clozes
    parsed, seconds = _seconds(cards_io.parse, one_note)
    assert seconds < 1 and len(parsed.cards) < 300, "the note is cut to MAX_BACK before each cloze copies it"
    assert all(len(f) <= cards_io.MAX_FRONT and len(b) <= cards_io.MAX_BACK for f, b in parsed.cards)
    lines = "\n".join(" ".join(f"{{{{c{i}::w}}}}" for i in range(1, 31)) for _ in range(3000))
    parsed, seconds = _seconds(cards_io.parse, lines)
    assert seconds < 2 and len(parsed.cards) == cards_io.MAX_CARDS
    assert parsed.dropped == 3000 * 30 - cards_io.MAX_CARDS and parsed.skipped == []
    anki = "#separator:tab\n#html:true\n" + "\n".join(f"<b>term {i}</b>\tdefinition<br>{i}" for i in range(3000))
    parsed = cards_io.parse(anki)
    assert len(parsed.cards) == cards_io.MAX_CARDS and parsed.dropped == 1000
    assert parsed.cards[0] == ("term 0", "definition\n0")


def test_free_learn_preview_takes_small_json_bodies_only(app, client):
    from app.blueprints.main import FREE_PREVIEW_MAX_BYTES

    assert 20 * 1024 <= FREE_PREVIEW_MAX_BYTES <= 30 * 1024
    r = client.post("/free-learn/preview", data="{}" + " " * FREE_PREVIEW_MAX_BYTES, content_type="application/json")
    assert r.status_code == 413
    r = client.post("/free-learn/preview", data=json.dumps({"text": "a\tb"}), content_type="text/plain")
    assert r.status_code == 415, "another site's 'simple' cross-site POST can't reach the parser"
    worst = json.dumps({"text": "{{c1::" * 4_500})  # 27 KB: minutes of backtracking before the fix
    assert len(worst) < FREE_PREVIEW_MAX_BYTES
    r, seconds = _seconds(lambda: client.post("/free-learn/preview", data=worst, content_type="application/json"))
    assert r.status_code == 200 and seconds < 1
    page = client.get("/free-learn").get_data(as_text=True)
    assert f'data-preview-max="{FREE_PREVIEW_MAX_BYTES}"' in page, "the page sends only the start of a longer paste"


def test_import_routes_cap_their_text(app, student, client, monkeypatch):
    from app.blueprints import study as study_bp

    assert study_bp.MAX_IMPORT_CHARS <= 2_000_000 and study_bp.IMPORT_PREVIEW_MAX_CHARS <= 300_000
    page = client.get("/study/decks/import").get_data(as_text=True)
    assert f'data-preview-max="{study_bp.IMPORT_PREVIEW_MAX_CHARS}"' in page
    assert f'data-import-max="{study_bp.MAX_IMPORT_CHARS}"' in page
    deck = _deck(student)
    monkeypatch.setattr(study_bp, "MAX_IMPORT_CHARS", 100)
    monkeypatch.setattr(study_bp, "IMPORT_PREVIEW_MAX_CHARS", 100)
    text = "uno\tone\n" * 20  # 160 characters
    r = client.post("/study/decks/new", data={"title": "Too long", "cards": text})
    assert r.status_code == 400 and b"more than we can import" in r.data
    r = client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": text}, follow_redirects=True)
    assert b"more than we can import" in r.data
    r = client.post("/study/decks/import", data={"title": "Too long", "text": text})
    assert r.status_code == 400 and b"more than we can import" in r.data
    r = client.post("/study/decks/import/preview", json={"text": text})
    assert r.status_code == 413 and "preview" in r.get_json()["error"]
    assert db.session.query(Deck).count() == 1 and len(db.session.get(Deck, deck.id).cards) == 8


def test_oversized_bodies_are_refused_before_anything_reads_them(app, student, client, monkeypatch):
    from app.blueprints import study as study_bp

    deck = _deck(student)
    assert app.view_functions["study.import_deck"].max_body[0] == study_bp.MAX_IMPORT_BODY
    for endpoint in ("study.new_deck", "study.deck", "study.import_deck"):
        monkeypatch.setattr(app.view_functions[endpoint], "max_body", (2_000, study_bp.TOO_BIG, False))
    monkeypatch.setattr(app.view_functions["study.import_preview"], "max_body", (2_000, "Too long to preview.", True))
    big = "uno\tone\n" * 500  # 4 KB
    app.config["WTF_CSRF_ENABLED"] = True  # no token sent: the size check comes before CSRF reads the form
    try:
        r = client.post("/study/decks/new?plan=1", data={"title": "Big", "cards": big})
        assert r.status_code == 302 and r.headers["Location"].endswith("/study/decks/new?plan=1")
        assert client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": big}).status_code == 302
        assert client.post("/study/decks/import", data={"title": "Big", "text": big}).status_code == 302
        r = client.post("/study/decks/import/preview", json={"text": big})
        assert r.status_code == 413 and r.get_json() == {"error": "Too long to preview."}
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert db.session.query(Deck).count() == 1 and len(db.session.get(Deck, deck.id).cards) == 8
    assert "more than we can import" in client.get("/study/decks/new").get_data(as_text=True)
    assert client.post("/study/decks/new", data={"title": "Small", "cards": "uno\tone"}).status_code == 302


def test_early_reviews_never_stretch_the_interval():
    now = datetime(2026, 3, 1, 12)
    c = _card()
    for _ in range(40):  # a starred-only drill twice a day for 20 days
        srs.grade(c, True, now=now)
        now += timedelta(hours=12)
    # Each step only when the card was actually due: 1, 3, 8, 20 days (before: 1,190,238 days).
    assert c.interval_days == 20 and c.review_count == 40 and c.due_at < now + timedelta(days=20)
    srs.grade(c, False, now=now)
    assert (c.repetitions, c.interval_days, c.due_at) == (0, 0, now + srs.RELEARN), "a miss still counts"
    # An exam added since still brings an early-reviewed card back before it.
    c = _card(repetitions=4, interval_days=40, review_count=4, last_reviewed_at=now - timedelta(days=2),
              due_at=now + timedelta(days=38))
    srs.grade(c, True, now=now, exam_at=now + timedelta(days=10))
    assert (c.repetitions, c.interval_days, c.due_at) == (4, 5, now + timedelta(days=5))


def test_intervals_are_capped_so_the_due_date_cant_overflow(student, client):
    now = datetime(2026, 3, 1, 12)
    c = _card(repetitions=16, interval_days=1_190_238, review_count=16, last_reviewed_at=now - timedelta(days=9),
              due_at=now - timedelta(days=1))
    srs.grade(c, True, now=now)
    assert c.interval_days == srs.MAX_INTERVAL_DAYS and c.due_at == now + timedelta(days=srs.MAX_INTERVAL_DAYS)
    # The route: a card already scheduled that far out no longer 500s (and rolls back) its batch.
    deck = _deck(student)
    far, other = deck.cards[:2]
    far.repetitions, far.interval_days, far.review_count = 17, 2_975_595, 17
    far.last_reviewed_at, far.due_at = utcnow() - timedelta(days=30), utcnow() - timedelta(days=1)
    db.session.commit()
    r = client.post("/study/learn/answers", json={"answers": [{"card_id": far.id, "correct": True},
                                                              {"card_id": other.id, "correct": True}]})
    assert r.status_code == 200 and r.get_json()["graded"] == 2
    db.session.refresh(far)
    db.session.refresh(other)
    assert far.interval_days == srs.MAX_INTERVAL_DAYS and other.review_count == 1


# (given, expected, strict, verdict). Python grades Test mode, answers.js grades Learn: same results.
ANSWER_VECTORS = [
    ("かき", "かぎ", False, "wrong"),  # persimmon / key: kana voicing marks change the word
    ("はか", "ばか", False, "wrong"),
    ("カート", "カード", False, "wrong"),
    ("かっこう", "がっこう", False, "almost"),  # 4+ characters: one off is a typo to confirm, like "libary"
    ("がっこう", "がっこう", False, "right"),
    ("कम", "काम", False, "wrong"),  # Indic vowel signs change the word too
    ("दन", "दिन", False, "wrong"),
    ("café", "cafe", False, "right"),  # Latin accents don't count
    ("cafe", "café", False, "right"),
    ("résumé", "resume", False, "right"),
    ("Viet Nam", "Việt Nam", False, "right"),
    ("שלום", "שָׁלוֹם", False, "right"),  # Hebrew points are optional
    ("كتب", "كَتَبَ", False, "right"),  # so are Arabic harakat
    ("ｃａｔ", "cat", False, "right"),  # full-width letters
    ("café", "café", True, "right"),  # composed and decomposed are the same, even when strict
    ("el rio", "el río", True, "wrong"),
    ("the cta", "the cat", False, "almost"),
    ("1946", "1947", False, "wrong"),
]


@pytest.mark.parametrize("given,expected,strict,verdict", ANSWER_VECTORS)
def test_answer_vectors_in_python(given, expected, strict, verdict):
    assert learn.check_answer(given, expected, strict=strict) == verdict


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_answer_vectors_in_the_browser_copy():
    payload = {"marks": [list(r) for r in learn.IGNORED_MARKS],
               "vectors": [[given, expected, strict] for given, expected, strict, _ in ANSWER_VECTORS]}
    assert _node("answers", payload) == [verdict for *_, verdict in ANSWER_VECTORS]


def test_learn_page_gets_the_ignored_marks_from_the_server(student, client):
    deck = _deck(student)
    page = client.get(f"/study/learn?deck={deck.id}").get_data(as_text=True)
    assert _learn_data(page)["marks"] == [list(r) for r in learn.IGNORED_MARKS]
    assert page.index("js/answers.js") < page.index("js/learn.js")
    learn_js = (ROOT / "app/static/js/learn.js").read_text()
    assert "hhAnswers.checker(data.marks)" in learn_js and "\\p{M}" not in learn_js


def test_huge_numeric_answers_dont_break_learn_or_test(student, client):
    rng = random.Random(1)
    assert learn.pick_options(0, ["9" * 309, "the cat"], rng) == []
    assert learn.pick_options(0, ["1" + "0" * 20, "the cat"], rng) == []
    assert all(learn.is_number(o) for o in learn.pick_options(0, ["1946", "the cat"], rng))
    deck = _deck(student, n=3)
    deck.cards[0].back = "9" * 309
    deck.cards[1].front = "9" * 309
    db.session.commit()
    assert client.get(f"/study/learn?deck={deck.id}").status_code == 200
    assert client.get(f"/study/test?deck={deck.id}").status_code == 200


def test_unicode_digits_never_reach_int(student, client):
    from app.services import sources

    deck = _deck(student)
    assert client.get("/study/learn?deck=%C2%B2").status_code == 302
    assert client.get(f"/study/test?deck={deck.id}&card=%C2%B9").status_code == 200
    r = client.post("/study/learn/answers", json={"answers": [{"card_id": "²"}, {"card_id": "٣"}, {"card_id": True}]})
    assert r.status_code == 200 and r.get_json()["graded"] == 0
    quiz = PracticeQuiz(user_id=student.id, title="Five", questions=[
        {"question": f"q{i}", "choices": ["a", "b"], "answer": 0, "explanation": ""} for i in range(5)])
    db.session.add(quiz)
    db.session.commit()
    r = client.post(f"/study/quizzes/{quiz.id}", data={f"q{i}": "²" for i in range(5)})
    assert r.status_code == 200 and b"0 / 5" in r.data
    assert client.post(f"/study/decks/{deck.id}", data={"action": "edit", "card_id": "²", "front": "x"}).status_code == 302
    assert client.get("/study/generate?kind=file&ref=%C2%B2").status_code == 200
    assert sources.parse(["file:²", "page:٣", "file:3"]) == [("file", 3)]
    assert cards_io.parse("#separator:tab\n#tags column:²\nuno\tone\tbio").cards == [("uno", "one")]


def test_a_quote_separator_header_doesnt_crash(app, student, client):
    text = '#separator:"\nuno"one\ndos"two'
    assert cards_io.parse(text).cards == [("uno", "one"), ("dos", "two")]
    r = app.test_client().post("/free-learn/preview", json={"text": text})
    assert r.status_code == 200 and r.get_json()["count"] == 2
    assert client.post("/study/decks/import/preview", json={"text": text}).status_code == 200
    assert client.post("/study/decks/new", data={"title": "Quotes", "cards": text}).status_code == 302
    assert client.post("/study/decks/import", data={"title": "Quotes 2", "text": text}).status_code == 302


def test_one_cloze_line_doesnt_swallow_a_front_back_paste(student, client):
    text = "hola :: hello\nadios :: goodbye\nThe {{c1::cat}} sat :: el gato"
    parsed = cards_io.parse(text)
    assert parsed.detected == "lines" and parsed.skipped == []
    assert parsed.cards == [("hola", "hello"), ("adios", "goodbye"), ("The [...] sat", "The cat sat\n\nel gato")]
    deck = _deck(student, n=0)
    r = client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": text + "\nnot a card"},
                    follow_redirects=True)
    assert "Added 3 cards. 1 line couldn" in r.get_data(as_text=True)
    # Headerless Anki cloze notes still become cloze cards, with the Extra field on the back.
    cards = cards_io.parse("{{c1::Floraxin}} is made in the {{c2::petal vault::organelle}}\tSee chapter 0").cards
    assert cards == [("[...] is made in the petal vault", "Floraxin is made in the petal vault\n\nSee chapter 0"),
                     ("Floraxin is made in the [organelle]", "Floraxin is made in the petal vault\n\nSee chapter 0")]
    assert cards_io.parse("The {{c1::quill cell}} stores ink.\nThe {{c1::moon-fern}} glows.").detected == "anki"


def test_imports_over_the_limit_say_how_many_cards_were_left_out(student, client, monkeypatch):
    monkeypatch.setattr(cards_io, "MAX_CARDS", 20)
    fifty = "\n".join(f"palabra {i}\tword {i}" for i in range(50))
    r = client.post("/study/decks/import", data={"title": "Big set", "text": fifty}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Imported 20 cards. Only the first 20 of 50 cards were imported." in page and "line couldn" not in page
    body = client.post("/study/decks/import/preview", json={"text": fifty}).get_json()
    assert (body["count"], body["dropped"], body["total"], body["skipped_count"]) == (20, 30, 50, 0)
    assert body["note"] == "Only the first 20 of 50 cards will be imported."
    r = client.post("/study/decks/new", data={"title": "Typed", "cards": fifty}, follow_redirects=True)
    assert "Added 20 cards. Only the first 20 of 50 cards were added." in r.get_data(as_text=True)
    deck = db.session.scalar(select(Deck).where(Deck.title == "Typed"))
    r = client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": fifty}, follow_redirects=True)
    assert "Added 20 cards. Only the first 20 of 50 cards were added." in r.get_data(as_text=True)
    assert len(db.session.get(Deck, deck.id).cards) == 40


def _answer_test(app, client, token) -> tuple[DeckTest, int]:
    """Hand in a test with every answer right; the saved DeckTest and how many questions it had."""
    from app.blueprints.study import _test_signer

    with app.test_request_context():
        questions = _test_signer().loads(token)["q"]
    by_id = {c.id: c for c in db.session.scalars(select(Card))}
    form = {"token": token}
    for i, q in enumerate(questions):
        card = by_id[q["c"]]
        if q["k"] == "mc":
            form[f"a{i}"] = str(q["o"].index(card.id))
        elif q["k"] == "tf":
            form[f"a{i}"] = "true" if q["s"] == card.id else "false"
        else:
            form[f"a{i}"] = card.back
    assert client.post("/study/test", data=form).status_code == 302
    return db.session.scalars(select(DeckTest).order_by(DeckTest.id.desc())).first(), len(questions)


def test_only_a_test_of_the_whole_exam_counts_as_readiness(app, student, client):
    from app.services import planner

    plan = _plan(student)
    deck = _deck(student, n=10, plan=plan)
    second = _deck(student, title="Second deck", n=3, plan=plan)
    deck.cards[3].starred = second.cards[0].starred = True
    db.session.commit()
    a, b = deck.cards[:2]
    for url in (f"/study/test?card={a.id}&card={b.id}&n=2",  # two picked cards
                f"/study/test?deck={deck.id}&n=20",  # one deck of a two-deck exam
                f"/study/test?plan={plan.id}&starred=1",  # only the starred cards
                f"/study/test?plan={plan.id}&n=2",  # the whole exam, but 2 questions
                f"/study/test?plan={plan.id}&n=12&check=1",  # the planner's ungraded quick check
                f"/study/test?plan={plan.id}&n=12&kind=pretest"):
        test, _ = _answer_test(app, client, _test_form(client, url)[1])
        assert test.plan_id is None and test.score == test.total, url
    assert planner.readiness(plan) == {"percent": 0, "source": "cards recalled", "cards": 13}
    page = client.get(f"/study/test?plan={plan.id}&n=12&check=1").get_data(as_text=True)
    assert "Quick check" in page and "Not graded" in page and page.count('class="card test-q"') == 12
    for url in (f"/study/test?deck={deck.id}&deck={second.id}&n=10", f"/study/test?plan={plan.id}&n=10"):
        test, asked = _answer_test(app, client, _test_form(client, url)[1])
        assert test.plan_id == plan.id and asked == 10, url
    assert planner.readiness(plan) == {"percent": 100, "source": "practice test", "cards": 13}
    small = _plan(student, days=9)  # a 4-card exam: a test of every card counts
    _deck(student, title="Small exam deck", n=4, plan=small)
    assert _answer_test(app, client, _test_form(client, f"/study/test?plan={small.id}&n=3")[1])[0].plan_id is None
    assert _answer_test(app, client, _test_form(client, f"/study/test?plan={small.id}&n=4")[1])[0].plan_id == small.id


def test_test_yourself_link_stays_under_the_request_line_limit(student, client):
    deck = Deck(user_id=student.id, title="Big", cards=[Card(front=f"w{i}", back=f"m{i}", position=i) for i in range(400)])
    db.session.add(deck)
    db.session.commit()
    r = client.post("/study/learn", data={"deck": str(deck.id), "card": [str(c.id) for c in deck.cards]})
    assert r.status_code == 200
    href = re.search(r'href="([^"]+)">Test yourself</a>', r.get_data(as_text=True)).group(1)
    ids = re.findall(r"card=(\d+)", href)
    assert len(ids) == len(set(ids)) == 150 and {int(i) for i in ids} <= {c.id for c in deck.cards}
    worst = len("GET /study/test?deck=123456&session=123456 HTTP/1.1") + 150 * len("&card=123456")
    assert worst < 4094, "even 6-digit ids fit gunicorn's request line"


def test_test_page_timer_isnt_a_live_region(student, client):
    deck = _deck(student)
    page = client.get(f"/study/test?deck={deck.id}&minutes=15").get_data(as_text=True)
    bar = re.search(r'<div class="test-bar card"[^>]*>', page).group(0)
    assert "role=" not in bar and "aria-live" not in bar, "the whole bar was re-announced every second"
    timer = re.search(r'<span class="timer"[^>]*>', page).group(0)
    assert 'role="timer"' in timer and 'aria-live="off"' in timer
    assert re.search(r'<div class="sr-only" id="test-announce" aria-live="polite"', page)
    assert "10 minutes left." in page and "1 minute left." in page


def test_pages_expose_the_signed_in_student_to_the_pasted_set_check(app, student, client):
    assert f'<body data-uid="{student.id}">' in client.get("/study/").get_data(as_text=True)
    assert "data-uid" not in app.test_client().get("/free-learn").get_data(as_text=True)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_browser_helpers_signed_out_saves_and_the_pasted_set():
    out = _node("app", {})
    post = out["post"]
    assert post["toLogin"] == {"ok": False, "message": "You were signed out — sign in again and retry.", "signedOut": True}
    assert post["html200"]["signedOut"] is True, "a 200 that isn't JSON isn't a save either"
    assert post["json200"] == {"ok": True, "data": {"ok": True}}
    assert post["error400"] == {"ok": False, "message": "Send JSON.", "signedOut": False}
    assert post["error500"]["message"] == "Request failed (500)"
    # A set pasted on /free-learn: kept through sign-in, bound to that student, gone after sign-out.
    assert out["saved"]["text"] == "A's set" and isinstance(out["saved"]["ts"], int)
    assert out["onLogin"] == out["signedInA"] == "A's set" and out["boundTo"] == "7"
    assert out["afterLogout"] is None and out["storageAfterLogout"] is None
    assert out["otherStudent"] is None and out["storageAfterOther"] is None, "never offered to the next student"
    assert out["expired"] is None and out["onTerms"] is None and out["legacy"] is None
    assert out["onRegister"] == "kept" and out["onAge"] == "kept"


def test_new_deck_and_generate_take_the_exam_from_the_planner(student, client):
    plan = _plan(student)
    theirs = _plan(make_user("mallory"))
    page = client.get(f"/study/decks/new?plan={plan.id}").get_data(as_text=True)
    assert "Which exam is this for?" in page and f'<option value="{plan.id}" selected>' in page
    r = client.post(f"/study/decks/new?plan={plan.id}", data={"title": "For the exam", "cards": "uno :: one",
                                                              "plan_id": str(plan.id)})
    assert r.status_code == 302
    assert db.session.scalar(select(Deck).where(Deck.title == "For the exam")).plan_id == plan.id
    r = client.post("/study/decks/new", data={"title": "Not theirs", "cards": "uno :: one", "plan_id": str(theirs.id)})
    assert r.status_code == 302, "someone else's exam is ignored, not an error"
    assert db.session.scalar(select(Deck).where(Deck.title == "Not theirs")).plan_id is None
    assert f'<option value="{theirs.id}"' not in client.get(f"/study/decks/new?plan={theirs.id}").get_data(as_text=True)
    page = client.get(f"/study/generate?plan={plan.id}").get_data(as_text=True)
    assert "Which exam is this for?" in page and f'<option value="{plan.id}" selected>' in page
    notes = {"mode": "paste", "pasted": "Notes about the moon-fern and its glimmerase enzyme. " * 5}
    assert client.post("/study/generate", data={**notes, "output": "deck", "plan_id": str(plan.id)}).status_code == 302
    assert client.post("/study/generate", data={**notes, "output": "quiz", "plan_id": str(plan.id)}).status_code == 302
    assert client.post("/study/generate", data={**notes, "output": "quiz", "plan_id": str(theirs.id)}).status_code == 302
    assert db.session.scalar(select(Deck).where(Deck.source == "ai")).plan_id == plan.id
    quizzes = db.session.scalars(select(PracticeQuiz).order_by(PracticeQuiz.id)).all()
    assert [q.plan_id for q in quizzes] == [plan.id, None]


def test_practice_quiz_coins_are_capped_at_three_a_day(student, client):
    from app.utils import local_now

    quizzes = [PracticeQuiz(user_id=student.id, title=f"Quiz {n}", questions=[
        {"question": f"q{i}", "choices": ["a", "b"], "answer": 0, "explanation": ""} for i in range(5)])
        for n in range(5)]
    db.session.add_all(quizzes)
    db.session.commit()
    paid = []
    for quiz in quizzes:
        r = client.post(f"/study/quizzes/{quiz.id}", data={f"q{i}": "0" for i in range(5)})
        assert r.status_code == 200 and b"5 / 5" in r.data
        paid.append(b"+5 Buddy Coins" in r.data)
    assert paid == [True, True, True, False, False], "making more quizzes doesn't make more coins"
    today = local_now(student).date().isoformat()
    awards = db.session.scalars(select(CoinTransaction).where(CoinTransaction.ref.like("quiz-day:%"))).all()
    assert sorted(t.ref for t in awards) == [f"quiz-day:{today}:{k}" for k in range(3)]
    assert sum(t.amount for t in awards) == 15


def test_headerless_anki_cloze_export_and_half_emoji():
    from app.services import cards_io

    parsed = cards_io.parse("{{c1::<b>Paris</b>}} is the capital&nbsp;of France\tSee <i>atlas</i>\n"
                            "{{c1::Rome}} is the capital of<br>Italy\textra")
    assert parsed.detected == "anki"
    assert parsed.cards[0] == ("[...] is the capital of France", "Paris is the capital of France\n\nSee atlas")
    # A cut paste can end in half an emoji: it's replaced, never a 500.
    parsed = cards_io.parse("#html:true\nuno\t\ud800 one")
    assert parsed.cards and "\ud800" not in parsed.cards[0][1]
    many = "".join(f"{{{{c{i}::w{i}}}}} " for i in range(1, 400))
    assert len(cards_io.cloze_cards(many)) == cards_io.MAX_CLOZES_PER_NOTE
