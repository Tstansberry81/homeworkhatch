"""AI study-material generation: flashcards, practice quizzes and summaries.

Material comes from the student's own content: any mix of synced Canvas files, pages and
their own uploads picked from one class (services/sources.py), an assignment, a topic
searched across a course, or pasted notes. Generated sets
are saved as ordinary decks/quizzes the student can edit.
"""

from __future__ import annotations

from dataclasses import dataclass

import markdown as md
import nh3
from sqlalchemy import select

from ..extensions import db
from ..models import Assignment, CanvasFile, Course, Page, User
from ..utils import html_to_text
from . import ai, retrieval, sources

# Roughly 40k tokens of source material per request. Longer sources are trimmed and the
# student is told so (never silently).
MAX_MATERIAL_CHARS = 160_000

SYSTEM = (
    "You create study materials for students from their own course materials. Stay faithful to the "
    "provided material: don't introduce facts that contradict it, and prefer its terminology and notation. "
    "Write clearly for a student audience. Use LaTeX between $...$ for math."
)


@dataclass
class Material:
    title: str
    text: str
    truncated: bool
    course_id: int | None
    sources: int = 1


class MaterialError(ValueError):
    pass


def _cap(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_MATERIAL_CHARS:
        return text, False
    return text[:MAX_MATERIAL_CHARS], True


def gather_material(user: User, kind: str, ref: str | int | None = None, topic: str | None = None,
                    pasted: str | None = None) -> Material:
    """kind: file | page | assignment | course (topic search) | paste."""
    if kind == "paste":
        text = (pasted or "").strip()
        if len(text) < 40:
            raise MaterialError("Paste at least a few sentences of notes.")
        text, cut = _cap(text)
        return Material("Pasted notes", text, cut, None)
    try:
        ident = int(ref or 0)
    except (TypeError, ValueError):
        raise MaterialError("Pick something to study from.")
    if kind == "file":
        f = db.session.get(CanvasFile, ident)
        if not f or f.user_id != user.id:
            raise MaterialError("File not found.")
        if not f.text:
            raise MaterialError(
                "No readable text in that file yet. It may still be uploading, be a scanned image, or be a format "
                "we can't read (PDF, Word, PowerPoint and text files work).")
        text, cut = _cap(f.text)
        return Material(f.name, text, cut, f.course_id)
    if kind == "page":
        p = db.session.get(Page, ident)
        if not p or db.session.get(Course, p.course_id).user_id != user.id:
            raise MaterialError("Page not found.")
        text, cut = _cap(html_to_text(p.body_html))
        if not text:
            raise MaterialError("That page is empty.")
        return Material(p.title, text, cut, p.course_id)
    if kind == "assignment":
        a = db.session.get(Assignment, ident)
        if not a or a.course.user_id != user.id:
            raise MaterialError("Assignment not found.")
        text, cut = _cap(html_to_text(a.description_html))
        if not text:
            raise MaterialError("That assignment has no description to study from.")
        return Material(a.name, text, cut, a.course_id)
    if kind == "course":
        course = db.session.get(Course, ident)
        if not course or course.user_id != user.id:
            raise MaterialError("Course not found.")
        if not (topic or "").strip():
            raise MaterialError("Enter a topic to pull material on.")
        chunks = retrieval.search(user.id, topic, [course.id], k=14)
        if not chunks:
            raise MaterialError(f"Couldn't find anything about “{topic}” in {course.name}'s synced materials.")
        text = "\n\n---\n\n".join(f"[{c.title}]\n{c.text}" for c in chunks)
        text, cut = _cap(text)
        return Material(f"{course.name}: {topic.strip()}", text, cut, course.id)
    raise MaterialError("Unknown material type.")


def gather_sources(user: User, refs) -> Material:
    """Several picked sources as one material; each gets a fair share of the length budget."""
    picked = sources.texts(user, refs)
    if not picked:
        raise MaterialError("Pick at least one file or page with readable text.")
    heads = [f"=== {src.title} ===\n" for src, _ in picked]
    budget = MAX_MATERIAL_CHARS - sum(len(h) + 2 for h in heads)
    caps = sources.fair_share([len(text) for _, text in picked], budget)
    text = "\n\n".join(head + body[:cap] for head, (_, body), cap in zip(heads, picked, caps))
    truncated = any(cap < len(body) for (_, body), cap in zip(picked, caps))
    course_ids = {src.course_id for src, _ in picked if src.course_id}
    title = picked[0][0].title if len(picked) == 1 else f"{len(picked)} sources"
    return Material(title, text, truncated, course_ids.pop() if len(course_ids) == 1 else None, len(picked))


def _prompt(material: Material, instruction: str) -> str:
    if material.sources > 1:
        instruction += (f" The material combines {material.sources} sources, each starting with a === title === line; "
                        "cover all of them, roughly in proportion to how much each one contains.")
    return (f"<material title=\"{material.title}\">\n{material.text}\n</material>\n\n{instruction}")


# ---------------------------------------------------------------- flashcards

CARDS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"front": {"type": "string"}, "back": {"type": "string"}},
                "required": ["front", "back"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "cards"],
    "additionalProperties": False,
}


def _valid_cards(data: dict) -> dict:
    cards = [
        {"front": c["front"].strip()[:2000], "back": c["back"].strip()[:4000]}
        for c in data.get("cards", []) if isinstance(c, dict) and c.get("front", "").strip() and c.get("back", "").strip()
    ]
    if not cards:
        raise ai.AIError("The AI didn't produce any usable cards. Try different material.")
    return {"title": (data.get("title") or "").strip()[:200], "cards": cards}


def generate_flashcards(user: User, material: Material, count: int = 15, fresh: bool = False) -> dict:
    count = max(5, min(int(count), 50))
    instruction = (
        f"Make {count} flashcards covering the most important ideas in the material: key terms, definitions, "
        "formulas, cause-and-effect, and common points of confusion. Fronts are short prompts or questions; backs "
        "are concise, complete answers (one to three sentences). No duplicates. Also give the set a short title."
    )
    return ai.complete_json(user, "flashcards", system=SYSTEM, prompt=_prompt(material, instruction),
                            schema=CARDS_SCHEMA, effort="low", validate=_valid_cards, share=True, fresh=fresh)


# ---------------------------------------------------------------- practice quizzes

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "integer"},
                    "explanation": {"type": "string"},
                },
                "required": ["question", "choices", "answer", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "questions"],
    "additionalProperties": False,
}


def valid_questions(raw) -> list[dict]:
    """Keep only well-formed 2-6 choice questions with an in-range answer index."""
    out = []
    for q in raw or []:
        if not isinstance(q, dict):
            continue
        raw_choices = [str(c).strip()[:500] for c in q.get("choices") or []]
        try:
            answer = int(q.get("answer"))
        except (TypeError, ValueError):
            continue
        if not 0 <= answer < len(raw_choices) or not raw_choices[answer]:
            continue  # the marked answer is missing or blank
        # Drop blank choices and move the answer index along with them.
        answer -= sum(1 for c in raw_choices[:answer] if not c)
        choices = [c for c in raw_choices if c]
        question = str(q.get("question") or "").strip()[:2000]
        if not question or not 2 <= len(choices) <= 6 or not 0 <= answer < len(choices):
            continue
        out.append({"question": question, "choices": choices, "answer": answer,
                    "explanation": str(q.get("explanation") or "").strip()[:2000]})
    return out


def _valid_quiz(data: dict) -> dict:
    questions = valid_questions(data.get("questions"))
    if not questions:
        raise ai.AIError("The AI didn't produce usable questions. Try different material.")
    return {"title": (data.get("title") or "").strip()[:200], "questions": questions}


def generate_quiz(user: User, material: Material, count: int = 10, fresh: bool = False) -> dict:
    count = max(3, min(int(count), 30))
    instruction = (
        f"Write {count} multiple-choice questions that test understanding of the material, not trivia. Each has "
        "exactly 4 choices with one correct answer; `answer` is the 0-based index of the correct choice. Wrong "
        "choices should be plausible misconceptions. Vary which position holds the correct answer. The "
        "explanation says why the answer is right. Also give the quiz a short title."
    )
    return ai.complete_json(user, "quiz", system=SYSTEM, prompt=_prompt(material, instruction),
                            schema=QUIZ_SCHEMA, effort="medium", validate=_valid_quiz, share=True, fresh=fresh)


# ---------------------------------------------------------------- summaries


def summarize(user: User, material: Material, fresh: bool = False) -> str:
    """Summaries of the same source are shared like study sets; `fresh` asks for a new one."""
    instruction = (
        "Write a study summary of the material in Markdown: a two-sentence overview, then the key ideas as "
        "headed sections with bullet points, important terms in bold with brief definitions, and a final "
        "'Check yourself' list of 3-5 questions. Be concise."
    )
    prompt = _prompt(material, instruction)
    key = ai.request_key("summary", SYSTEM, prompt)
    if not fresh and (hit := ai.reuse(key)) is not None:
        return hit["text"]
    result = ai.complete(user, "summary", system=SYSTEM, prompt=prompt, effort="low")
    ai.remember(key, "summary", {"text": result.text}, result)
    return result.text


# ---------------------------------------------------------------- scanned PDFs

# Claude reads PDFs natively, including scanned pages (as images). The request limit is
# 32 MB and base64 adds a third, so cap the source file below that.
MAX_TRANSCRIBE_BYTES = 20 * 1024 * 1024

TRANSCRIBE_INSTRUCTION = (
    "Transcribe all of the readable text in this document, in reading order. Output only the text: keep "
    "paragraph breaks and headings on their own lines, write math in LaTeX between $...$, and mark "
    "illegible passages as [illegible]. Don't add commentary, summaries, or page numbers unless they're part "
    "of the text."
)


def transcribe_pdf(user: User, pdf_bytes: bytes, name: str) -> str:
    """Turn a scanned (image-only) PDF into text with Claude, streaming because output can be long."""
    import base64

    import hashlib

    if len(pdf_bytes) > MAX_TRANSCRIBE_BYTES:
        raise MaterialError("That PDF is too large for AI reading (limit 20 MB).")
    # Everyone in a class has the same scanned handout: read it once, keyed on the exact bytes.
    key = ai.request_key("transcribe", TRANSCRIBE_INSTRUCTION, hashlib.sha256(pdf_bytes).hexdigest())
    if (hit := ai.reuse(key)) is not None:
        return hit["text"]
    usage = ai.reserve(user, "transcribe")
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.standard_b64encode(pdf_bytes).decode()},
         "title": name[:200]},
        {"type": "text", "text": TRANSCRIBE_INSTRUCTION},
    ]
    try:
        handle = ai.provider().stream(system="You transcribe course documents accurately.",
                                      messages=[{"role": "user", "content": content}], max_tokens=64000, effort="low",
                                      model=ai.model_for("transcribe"))
        text = "".join(handle).strip()
    except ai.AIError:
        ai.release(usage)
        raise
    ai.finish(usage, handle.result)
    if not text:
        raise ai.AIError("The AI couldn't find readable text in that PDF.")
    ai.remember(key, "transcribe", {"text": text}, handle.result)
    return text


# ---------------------------------------------------------------- rendering

_MD_TAGS = {"p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "h1", "h2", "h3", "h4", "blockquote",
            "a", "table", "thead", "tbody", "tr", "th", "td", "hr", "sup", "sub", "del"}


def render_markdown(text: str) -> str:
    """Markdown (from the AI) -> sanitized HTML. Math stays as $...$ for KaTeX to render."""
    html = md.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists"])
    return nh3.clean(html, tags=_MD_TAGS, attributes={"a": {"href"}}, url_schemes={"http", "https"},
                     link_rel="noopener noreferrer")


def material_options(user: User, include_course_id: int | None = None) -> dict:
    """Classes the generator can draw from: the visible ones, plus one explicitly requested class
    even if it's hidden (so "Make flashcards" from a hidden class's file still works)."""
    visible = (Course.hidden.is_(False)) | (Course.id == include_course_id) if include_course_id else Course.hidden.is_(False)
    courses = db.session.scalars(select(Course).where(Course.user_id == user.id, Course.active.is_(True),
                                                      visible).order_by(Course.name)).all()
    return {"courses": courses}
