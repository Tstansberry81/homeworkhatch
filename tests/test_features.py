from datetime import timedelta

from sqlalchemy import func, select

from app.extensions import db
from app.models import (AIUsage, Card, CanvasFile, ChatMessage, Course, Deck, LiveSession, Page, PracticeQuiz,
                        SharedGeneration, TutorMessage, User, utcnow)
from app.services import ai, coins

from .conftest import api_token, login, make_user, sync

# ---------------------------------------------------------------- AI generation


def test_generate_flashcards_from_a_synced_file(synced_user, client, fake_ai):
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    r = client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": [f"file:{f.id}"], "count": 10})
    assert r.status_code == 302
    deck = db.session.scalar(select(Deck))
    assert deck.source == "ai" and deck.title == "Derivatives"
    assert [c.front for c in deck.cards] == ["d/dx sin x", "Power rule"], "blank cards are filtered out"
    call = fake_ai.calls[-1]
    assert "multiply the outer derivative" in call["messages"][0]["content"], "the file's text is the material"
    assert call["schema"]["properties"]["cards"] and call["effort"] == "low"
    assert db.session.scalar(select(AIUsage.kind)) == "flashcards"


def test_generate_quiz_from_topic_search_and_take_it(synced_user, client):
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    r = client.post("/study/generate", data={"output": "quiz", "mode": "course", "course_id": course.id, "topic": "chain rule"})
    assert r.status_code == 302
    quiz = db.session.scalar(select(PracticeQuiz))
    assert len(quiz.questions) == 2, "malformed questions are dropped"
    before = coins.balance(synced_user.id)
    r = client.post(f"/study/quizzes/{quiz.id}", data={"q0": "1", "q1": "0"})
    assert r.status_code == 200 and b"2 / 2" in r.data
    assert coins.balance(synced_user.id) == before, "quizzes under 5 questions don't pay coins"
    five = PracticeQuiz(user_id=synced_user.id, title="Five", questions=[
        {"question": f"q{i}", "choices": ["a", "b"], "answer": 0, "explanation": ""} for i in range(5)])
    db.session.add(five)
    db.session.commit()
    client.post(f"/study/quizzes/{five.id}", data={f"q{i}": "0" for i in range(5)})
    assert coins.balance(synced_user.id) == before + 5
    client.post(f"/study/quizzes/{five.id}", data={f"q{i}": "0" for i in range(5)})
    assert coins.balance(synced_user.id) == before + 5, "quiz coins pay once per quiz per day"


def test_quiz_editor_round_trips_multiline_text(synced_user, client):
    from app.blueprints.study import parse_quiz_text, quiz_to_text

    quiz = PracticeQuiz(user_id=synced_user.id, title="Multi", questions=[
        {"question": "Solve:\nx + 1 = 3", "choices": ["x = 1", "x = 2\n(check it)", "", "x = 3"], "answer": 1,
         "explanation": "Subtract 1.\nThen done."}])
    # blank choice dropped, answer index follows the right choice
    from app.services.study import valid_questions

    cleaned = valid_questions(quiz.questions)
    assert cleaned[0]["choices"][cleaned[0]["answer"]] == "x = 2\n(check it)"
    quiz.questions = cleaned
    again = parse_quiz_text(quiz_to_text(quiz))
    assert again == cleaned


def test_generate_reports_unusable_sources(synced_user, client):
    r = client.post("/study/generate", data={"output": "deck", "mode": "paste", "pasted": "too short"})
    assert r.status_code == 400 and b"Paste at least" in r.data
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9001"))  # fake PDF bytes -> no text
    r = client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": [f"file:{f.id}"]})
    assert r.status_code == 400 and b"readable text" in r.data


def test_free_trial_runs_out_but_shared_sets_stay_free(app, synced_user, client, fake_ai):
    from app.services import ai

    notes = {"output": "deck", "mode": "paste", "pasted": "Notes about the chain rule " * 5}
    for _ in range(5):  # the whole free trial, used last month: it doesn't reset
        db.session.add(AIUsage(user_id=synced_user.id, kind="x", created_at=utcnow() - timedelta(days=40)))
    db.session.commit()
    assert ai.remaining(synced_user) == 0
    r = client.post("/study/generate", data=notes)
    assert r.status_code == 400 and b"used your 5 free AI actions" in r.data and not fake_ai.calls

    classmate = make_user("kim", plan="plus", plan_status="active")
    kim = app.test_client()
    login(kim, classmate)
    r = kim.post("/study/generate", data=notes)
    assert r.status_code == 302 and len(fake_ai.calls) == 1
    assert ai.remaining(classmate) == 149

    # The same request from the same material is served from the saved result: no call, no action used.
    r = client.post("/study/generate", data=notes, follow_redirects=True)
    assert r.status_code == 200 and b"no AI action used" in r.data and len(fake_ai.calls) == 1
    decks = db.session.scalars(select(Deck).order_by(Deck.id)).all()
    assert [d.user_id for d in decks] == [classmate.id, synced_user.id], "each student gets their own editable copy"
    assert [c.front for c in decks[0].cards] == [c.front for c in decks[1].cards]
    shared = db.session.scalar(select(SharedGeneration))
    assert shared.kind == "flashcards" and shared.uses == 1

    # Asking for a new version really generates (and costs the classmate an action).
    kim.post("/study/generate", data={**notes, "fresh": "1"})
    assert len(fake_ai.calls) == 2 and ai.remaining(classmate) == 148
    assert db.session.scalar(select(func.count(SharedGeneration.id))) == 1, "the first saved version stays"

    # Different material never matches.
    kim.post("/study/generate", data={**notes, "pasted": "Notes about the product rule " * 5})
    assert len(fake_ai.calls) == 3


def test_shared_summaries_and_scanned_pdf_readings(app, synced_user, fake_ai):
    from app.services import study

    material = study.Material("Lecture 1", "Limits describe what a function approaches. " * 20, False, None)
    first = study.summarize(synced_user, material)
    assert study.summarize(synced_user, material) == first and len(fake_ai.calls) == 1
    study.summarize(synced_user, material, fresh=True)
    assert len(fake_ai.calls) == 2
    pdf = b"%PDF-1.4 scanned handout"
    text = study.transcribe_pdf(synced_user, pdf, "handout.pdf")
    other = make_user("kim")
    assert study.transcribe_pdf(other, pdf, "renamed.pdf") == text and len(fake_ai.calls) == 3
    assert db.session.scalar(select(func.count(AIUsage.id)).where(AIUsage.user_id == other.id)) == 0


def test_summary_is_rendered_safely(synced_user, client):
    page = db.session.scalar(select(Page))
    r = client.post(f"/courses/summarize/page/{page.id}")
    assert r.status_code == 302
    html = client.get(f"/courses/pages/{page.id}").get_data(as_text=True)
    assert "<h2>Overview</h2>" in html and "<strong>summary</strong>" in html


def test_tutor_streams_with_citations_and_saves_history(synced_user, client, fake_ai):
    fake_ai.close_session_before_streaming = True  # as in production: the answer outlives the request
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    r = client.post("/tutor/new", data={"course_id": course.id})
    conv_url = r.headers["Location"]
    conv_id = int(conv_url.rstrip("/").split("/")[-1])
    r = client.post(f"/tutor/{conv_id}/message", json={"text": "How does the chain rule work?"})
    assert r.mimetype == "text/event-stream"
    body = r.get_data(as_text=True)
    assert "event: delta" in body and "event: done" in body
    assert 'class=\\"cite\\"' in body, "[S1] becomes a link"
    prompt = fake_ai.calls[-1]["messages"][-1]["content"]
    assert "<materials" in prompt and "[S1]" in prompt and "chain rule" in prompt.lower()
    msgs = db.session.scalars(select(TutorMessage).order_by(TutorMessage.id)).all()
    assert [m.role for m in msgs] == ["user", "assistant"] and msgs[1].sources
    page = client.get(conv_url).get_data(as_text=True)
    assert "chain rule multiplies" in page
    # Second turn includes the history.
    client.post(f"/tutor/{conv_id}/message", json={"text": "And for sin(x^2)?"}).get_data()
    assert [m["role"] for m in fake_ai.calls[-1]["messages"]] == ["user", "assistant", "user"]


def test_tutor_rejects_when_quota_used(synced_user, client):
    for _ in range(25):
        db.session.add(AIUsage(user_id=synced_user.id, kind="x"))
    db.session.commit()
    r = client.post("/tutor/new", data={})
    conv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    r = client.post(f"/tutor/{conv_id}/message", json={"text": "hi"})
    assert r.status_code == 402


def test_ai_errors_are_shown_not_crashed(synced_user, client, fake_ai):
    fake_ai.fail_with = ai.AIError("The AI is busy right now. Try again in a minute.")
    r = client.post("/study/generate", data={"output": "deck", "mode": "paste", "pasted": "Notes about the chain rule " * 5})
    assert r.status_code == 400 and b"busy right now" in r.data


# ---------------------------------------------------------------- flashcards


def test_manual_deck_and_flip_through_study(synced_user, client):
    from app.models import CoinTransaction

    r = client.post("/study/decks/new", data={"title": "Vocab", "cards": "hola :: hello\nadios\tgoodbye\nbroken line"})
    deck = db.session.scalar(select(Deck).where(Deck.title == "Vocab"))
    assert len(deck.cards) == 2
    page = client.get(f"/study/decks/{deck.id}/review").get_data(as_text=True)
    assert "hola" in page and "goodbye" in page and 'id="next"' in page and 'id="flip"' in page, "every card, arrows and flip"
    assert client.get(f"/study/decks/{deck.id}/cram").status_code == 302, "old cram links land on the viewer"

    # Reaching the last card of a real deck pays today's study coins, once.
    before = db.session.query(CoinTransaction).count()
    client.post(f"/study/decks/{deck.id}/studied")
    assert db.session.query(CoinTransaction).count() == before, "decks under 5 cards don't pay"
    client.post(f"/study/decks/{deck.id}", data={"action": "add", "cards": "a :: 1\nb :: 2\nc :: 3"})
    client.post(f"/study/decks/{deck.id}/studied")
    client.post(f"/study/decks/{deck.id}/studied")
    assert db.session.query(CoinTransaction).count() == before + 1


# ---------------------------------------------------------------- live quiz


def test_live_quiz_full_game(app, synced_user, client):
    quiz = PracticeQuiz(user_id=synced_user.id, title="Live", seconds_per_question=20, questions=[
        {"question": "2+2", "choices": ["3", "4"], "answer": 1, "explanation": ""},
        {"question": "3+3", "choices": ["6", "7"], "answer": 0, "explanation": ""}])
    db.session.add(quiz)
    db.session.commit()
    r = client.post(f"/live/host/{quiz.id}")
    code = r.headers["Location"].split("/")[-2]
    session = db.session.scalar(select(LiveSession))
    assert session.code == code

    # Two signed-in classmates and one guest join.
    players = []
    for name in ("alice", "bob"):
        make_user(name)
        c = app.test_client()
        login(c, db.session.scalar(select(User).where(User.username == name)))
        assert c.post("/live/join", data={"code": code, "nickname": name}).status_code == 302
        players.append(c)
    guest = app.test_client()
    assert guest.post("/live/join", data={"code": code, "nickname": "guest"}).status_code == 302
    assert guest.post("/live/join", data={"code": code, "nickname": "alice"}).status_code in (302, 400)
    players.append(guest)

    host_state = client.get(f"/live/{code}/state").get_json()
    assert host_state["state"] == "lobby" and host_state["players"] == 3

    client.post(f"/live/{code}/control", json={"action": "start"})
    st = players[0].get(f"/live/{code}/state").get_json()
    assert st["state"] == "question" and "correct" not in st, "answers stay secret until reveal"
    assert players[0].post(f"/live/{code}/answer", json={"choice": 1}).status_code == 200
    assert players[0].post(f"/live/{code}/answer", json={"choice": 1}).status_code == 409, "one answer per question"
    players[1].post(f"/live/{code}/answer", json={"choice": 0})
    players[2].post(f"/live/{code}/answer", json={"choice": 1})
    st = players[0].get(f"/live/{code}/state").get_json()
    assert st["state"] == "reveal", "closes when everyone answered"
    assert st["correct"] == 1 and st["distribution"] == [1, 2] and st["you"]["correct"] is True
    assert 500 <= st["you"]["points"] <= 1000

    client.post(f"/live/{code}/control", json={"action": "next"})
    players[1].post(f"/live/{code}/answer", json={"choice": 0})
    client.post(f"/live/{code}/control", json={"action": "next"})  # past the last question -> finished
    final = players[0].get(f"/live/{code}/state").get_json()
    assert final["state"] == "finished"
    alice = db.session.scalar(select(User).where(User.username == "alice"))
    bob = db.session.scalar(select(User).where(User.username == "bob"))
    assert coins.balance(alice.id) >= 10 and coins.balance(bob.id) >= 10, "signed-in players split the prizes"
    assert players[0].get(f"/live/{code}/state").status_code == 200
    assert app.test_client().get(f"/live/{code}/state").status_code == 403, "strangers can't peek"


# ---------------------------------------------------------------- class chat


def test_class_chat_membership_and_moderation(app, client, snapshot, manifest):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    sync(client, api_token(alice), snapshot, manifest)
    snapshot["user"]["id"] = "602"
    sync(client, api_token(bob), snapshot, manifest)
    snapshot["courses"] = [dict(snapshot["courses"][0], id="999", name="Other class")]
    snapshot["user"]["id"] = "603"
    sync(client, api_token(eve), snapshot, manifest)

    ca, cb, ce = app.test_client(), app.test_client(), app.test_client()
    login(ca, alice), login(cb, bob), login(ce, eve)
    a_course = db.session.scalar(select(Course).where(Course.user_id == alice.id, Course.canvas_id == "101"))
    b_course = db.session.scalar(select(Course).where(Course.user_id == bob.id, Course.canvas_id == "101"))
    assert ca.post(f"/chat/course/{a_course.id}/messages", json={"body": "anyone done HW 3? this is shit"}).status_code == 200
    msgs = cb.get(f"/chat/course/{b_course.id}/messages").get_json()["messages"]
    assert [m["body"] for m in msgs] == ["anyone done HW 3? this is s***"] and msgs[0]["author"] == "Alice"
    assert ce.get(f"/chat/course/{a_course.id}/messages").status_code == 404, "not your course, not your room"
    bad = cb.post(f"/chat/course/{b_course.id}/messages", json={"body": "kys"})
    assert bad.status_code == 400
    mid = msgs[0]["id"]
    assert ce.post(f"/chat/messages/{mid}/report").status_code == 404, "outsiders can't even report"
    assert cb.post(f"/chat/messages/{mid}/report", json={"reason": "language"}).status_code == 200
    assert cb.post(f"/chat/messages/{mid}/delete").status_code == 403, "can't delete someone else's message"
    assert ca.post(f"/chat/messages/{mid}/delete").status_code == 200
    assert db.session.get(ChatMessage, mid).deleted is True


def test_scanned_pdf_can_be_read_with_ai(synced_user, client, fake_ai):
    from app.models import ContentChunk

    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9001"))  # fake PDF bytes -> no text layer
    assert f.text_status in ("empty", "error")
    page = client.get(f"/courses/files/{f.id}").get_data(as_text=True)
    assert "Read this scan with AI" in page
    r = client.post(f"/courses/files/{f.id}/transcribe")
    assert r.status_code == 302
    db.session.refresh(f)
    assert f.text_status == "ai" and "chain rule" in f.text
    sent = fake_ai.calls[-1]["messages"][0]["content"]
    assert sent[0]["type"] == "document" and sent[0]["source"]["media_type"] == "application/pdf"
    assert db.session.scalar(select(ContentChunk.id).where(ContentChunk.source_type == "file",
                                                           ContentChunk.source_id == f.id)), "now searchable by the tutor"
    assert db.session.scalar(select(AIUsage.kind).where(AIUsage.kind == "transcribe")) == "transcribe"
    other = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    assert client.post(f"/courses/files/{other.id}/transcribe").status_code == 302  # not a PDF: refused politely
