"""AI tutor: chat about one class (or all of them), grounded in the student's synced materials.

Answers stream to the browser as server-sent events. Relevant course material is
retrieved per question and passed in with numbered labels [S1], [S2]..., which the answer
cites and the page turns into links back to the source. Files the student attaches to a chat
go with every question, in the system prompt so follow-ups reuse the prompt cache.
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
from ..models import AIUsage, ContentChunk, TutorConversation, TutorMessage, User, utcnow
from ..services import ai, retrieval, sources
from ..services.study import render_markdown

bp = Blueprint("tutor", __name__, url_prefix="/tutor")

HISTORY_MESSAGES = 12
ATTACHED_CHARS = 80_000  # ~20k tokens of attached files, shared fairly between them
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
- Attached files: when the student attached files to this chat they appear below in <attached>, also labeled
  [S1], [S2]... Treat them as the main material for their questions and cite them the same way.
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


def render_answer(text: str, cited: list[dict] | None) -> str:
    rendered = render_markdown(text)
    by_n = {s["n"]: s for s in cited or []}

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
                           courses=queries.visible_courses(current_user.id), remaining=ai.remaining(current_user),
                           attached=sources.describe(current_user, conv.attachments or []))


@bp.route("/<int:conversation_id>/attachments", methods=["POST"])
@login_required
def attachments(conversation_id: int):
    conv = _conversation(conversation_id)
    refs = (request.get_json(silent=True) or {}).get("refs") or []
    described = sources.describe(current_user, refs)[:20]
    conv.attachments = [s.ref for s in described]
    db.session.commit()
    return jsonify({"sources": [s.to_dict() for s in described]})


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
    attached = sources.texts(current_user, conv.attachments or [])
    attached_refs = {src.ref for src, _ in attached}
    chunks = [c for c in retrieval.search(current_user.id, f"{text}\n{last_user}", course_ids, k=8 + len(attached_refs) * 3)
              if f"{c.source_type}:{c.source_id}" not in attached_refs][:8]  # attached files are already in full
    cited_sources = [{"n": i, "title": src.title, "type": src.kind, "url": src.url} for i, (src, _) in enumerate(attached, 1)]
    cited_sources += [{"n": i, "title": c.title, "type": c.source_type, "url": _source_link(c)}
                      for i, c in enumerate(chunks, len(attached) + 1)]
    materials = "\n\n".join(f"[S{i}] {c.title} ({c.source_type})\n{c.text}" for i, c in enumerate(chunks, len(attached) + 1))
    scope = conv.course.name if conv.course else "all of the student's classes"
    prompt = (f"<materials scope=\"{scope}\">\n{materials or 'No matching course material was found.'}\n</materials>\n\n"
              f"{text}")
    system: str | list = SYSTEM
    if attached:
        caps = sources.fair_share([len(body) for _, body in attached], ATTACHED_CHARS)
        block = "\n\n".join(f"[S{i}] {src.title}\n{body[:cap]}" for i, ((src, body), cap) in enumerate(zip(attached, caps), 1))
        system = [{"type": "text", "text": SYSTEM},
                  {"type": "text", "text": f"<attached>\n{block}\n</attached>", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": m.role, "content": m.content} for m in history] + [{"role": "user", "content": prompt}]

    db.session.add(TutorMessage(conversation_id=conv.id, role="user", content=text))
    if conv.title == "New conversation":
        conv.title = text[:80] + ("…" if len(text) > 80 else "")
    conv.updated_at = utcnow()
    db.session.commit()
    handle = provider.stream(system=system, messages=messages, max_tokens=12000, effort="medium")
    # By the time the answer streams, this request's database session has been closed, so the
    # generator works from ids and loads what it needs itself.
    conv_id, usage_id, user_id = conv.id, usage.id, current_user.id

    def generate():
        parts: list[str] = []
        try:
            for delta in handle:
                parts.append(delta)
                yield _sse("delta", {"text": delta})
        except ai.AIError as exc:
            usage = db.session.get(AIUsage, usage_id)
            if usage is not None:
                if parts:
                    ai.finish(usage, None)  # a partial answer still used the model
                else:
                    ai.release(usage)
            yield _sse("error", {"message": str(exc)})
            return
        answer = "".join(parts).strip() or "(No answer.)"
        cited = {int(n) for n in re.findall(r"\[S(\d+)\]", answer)}
        used = [s for s in cited_sources if s["n"] in cited]
        db.session.add(TutorMessage(conversation_id=conv_id, role="assistant", content=answer, sources=used))
        ai.finish(db.session.get(AIUsage, usage_id), handle.result)
        yield _sse("done", {"html": render_answer(answer, used), "sources": used,
                            "remaining": ai.remaining(db.session.get(User, user_id))})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
