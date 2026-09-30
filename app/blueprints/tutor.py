"""AI tutor: chat about one class (or all of them), grounded in the student's synced materials.

Answers stream to the browser as server-sent events. Relevant course material is
retrieved per question and passed in with numbered labels [S1], [S2]..., which the answer
cites and the page turns into links back to the source.
"""

from __future__ import annotations

import html
import json
import re

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, stream_with_context, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import queries
from ..extensions import db
from ..models import ContentChunk, TutorConversation, TutorMessage, utcnow
from ..services import ai, retrieval
from ..services.study import render_markdown

bp = Blueprint("tutor", __name__, url_prefix="/tutor")

HISTORY_MESSAGES = 12
SYSTEM = """You are Homework Hatch's study tutor. You help one student learn their own coursework.

How to help:
- Teach. Explain concepts, work through similar examples, check the student's reasoning, and give hints that
  move them forward. Ask a short question back when it would help them think.
- Academic integrity: don't produce work the student would hand in as their own — finished essays, complete
  solutions to their assigned problems, or answers to take-home tests. Guide them step by step instead, and say
  briefly why when you do that.
- Course materials: the user message includes excerpts from their Canvas course in <materials>, labeled [S1],
  [S2]... Use them when relevant and cite them inline like [S1]. If the materials don't cover the question, say
  so and answer from general knowledge, marked as such. Never invent course policies, due dates, or grades.
- Format with Markdown. Use LaTeX between $...$ (inline) or $$...$$ (display) for math. Keep answers focused."""


def _conversation(conversation_id: int) -> TutorConversation:
    conv = db.session.get(TutorConversation, conversation_id)
    if conv is None or conv.user_id != current_user.id:
        abort(404)
    return conv


def _source_link(chunk: ContentChunk) -> str | None:
    if chunk.source_type == "file":
        return url_for("courses.file_detail", file_id=chunk.source_id)
    if chunk.source_type == "page":
        return url_for("courses.page", page_id=chunk.source_id)
    if chunk.source_type == "assignment":
        return url_for("courses.assignment", assignment_id=chunk.source_id)
    if chunk.source_type == "announcement":
        return url_for("courses.detail", course_id=chunk.course_id, tab="announcements")
    return url_for("courses.detail", course_id=chunk.course_id)


def render_answer(text: str, sources: list[dict] | None) -> str:
    rendered = render_markdown(text)
    by_n = {s["n"]: s for s in sources or []}

    def link(m):
        s = by_n.get(int(m.group(1)))
        if not s:
            return m.group(0)
        return (f'<a class="cite" href="{html.escape(s["url"] or "#")}" title="{html.escape(s["title"])}">'
                f'S{s["n"]}</a>')

    return re.sub(r"\[S(\d+)\]", link, rendered)


@bp.app_template_global()
def tutor_render(message: TutorMessage) -> str:
    if message.role == "assistant":
        return render_answer(message.content, message.sources)
    return html.escape(message.content).replace("\n", "<br>")


@bp.route("/")
@login_required
def index():
    latest = db.session.scalar(select(TutorConversation).where(TutorConversation.user_id == current_user.id)
                               .order_by(TutorConversation.updated_at.desc()).limit(1))
    if latest:
        return redirect(url_for("tutor.conversation", conversation_id=latest.id))
    return render_template("tutor/empty.html", courses=queries.visible_courses(current_user.id))


@bp.route("/new", methods=["POST"])
@login_required
def new():
    course_id = request.form.get("course_id", type=int)
    if course_id:
        queries.owned_course(current_user.id, course_id)
    conv = TutorConversation(user_id=current_user.id, course_id=course_id or None)
    db.session.add(conv)
    db.session.commit()
    return redirect(url_for("tutor.conversation", conversation_id=conv.id))


@bp.route("/<int:conversation_id>")
@login_required
def conversation(conversation_id: int):
    conv = _conversation(conversation_id)
    conversations = db.session.scalars(select(TutorConversation).where(TutorConversation.user_id == current_user.id)
                                       .order_by(TutorConversation.updated_at.desc()).limit(50)).all()
    return render_template("tutor/chat.html", conv=conv, conversations=conversations,
                           courses=queries.visible_courses(current_user.id), remaining=ai.remaining(current_user))


@bp.route("/<int:conversation_id>/delete", methods=["POST"])
@login_required
def delete(conversation_id: int):
    db.session.delete(_conversation(conversation_id))
    db.session.commit()
    flash("Conversation deleted.", "info")
    return redirect(url_for("tutor.index"))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@bp.route("/<int:conversation_id>/message", methods=["POST"])
@login_required
def message(conversation_id: int):
    conv = _conversation(conversation_id)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:6000]
    if not text:
        return jsonify({"error": "Type a question first."}), 400
    try:
        provider = ai.provider()
        usage = ai.reserve(current_user, "tutor")
    except ai.AIError as exc:
        return jsonify({"error": str(exc)}), 402 if isinstance(exc, ai.QuotaExceeded) else 503

    course_ids = [conv.course_id] if conv.course_id else [c.id for c in queries.visible_courses(current_user.id)]
    history = conv.messages[-HISTORY_MESSAGES:]
    while history and history[0].role != "user":  # the API expects the conversation to open with the user
        history = history[1:]
    last_user = next((m.content for m in reversed(history) if m.role == "user"), "")
    chunks = retrieval.search(current_user.id, f"{text}\n{last_user}", course_ids, k=8)
    sources = [{"n": i, "title": c.title, "type": c.source_type, "url": _source_link(c)} for i, c in enumerate(chunks, 1)]
    materials = "\n\n".join(f"[S{i}] {c.title} ({c.source_type})\n{c.text}" for i, c in enumerate(chunks, 1))
    scope = conv.course.name if conv.course else "all of the student's classes"
    prompt = (f"<materials scope=\"{scope}\">\n{materials or 'No matching course material was found.'}\n</materials>\n\n"
              f"{text}")
    messages = [{"role": m.role, "content": m.content} for m in history] + [{"role": "user", "content": prompt}]

    db.session.add(TutorMessage(conversation_id=conv.id, role="user", content=text))
    if conv.title == "New conversation":
        conv.title = text[:80] + ("…" if len(text) > 80 else "")
    conv.updated_at = utcnow()
    db.session.commit()
    handle = provider.stream(system=SYSTEM, messages=messages, max_tokens=12000, effort="medium")
    user = current_user._get_current_object()

    def generate():
        parts: list[str] = []
        try:
            for delta in handle:
                parts.append(delta)
                yield _sse("delta", {"text": delta})
        except ai.AIError as exc:
            if parts:
                ai.finish(usage, None)  # a partial answer still used the model
            else:
                ai.release(usage)
            yield _sse("error", {"message": str(exc)})
            return
        answer = "".join(parts).strip() or "(No answer.)"
        cited = {int(n) for n in re.findall(r"\[S(\d+)\]", answer)}
        used = [s for s in sources if s["n"] in cited]
        db.session.add(TutorMessage(conversation_id=conv.id, role="assistant", content=answer, sources=used))
        ai.finish(usage, handle.result)
        yield _sse("done", {"html": render_answer(answer, used), "sources": used,
                            "remaining": ai.remaining(user)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
