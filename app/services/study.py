"""AI study-material generation: flashcards, practice quizzes and summaries.

Material comes from the student's own synced Canvas content (a file, a page, an
assignment, or a topic searched across a course) or from pasted notes. Generated sets
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
from . import ai, retrieval

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


def _prompt(material: Material, instruction: str) -> str:
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


def generate_flashcards(user: User, material: Material, count: int = 15) -> dict:
    count = max(5, min(int(count), 50))
    instruction = (
        f"Make {count} flashcards covering the most important ideas in the material: key terms, definitions, "
        "formulas, cause-and-effect, and common points of confusion. Fronts are short prompts or questions; backs "
        "are concise, complete answers (one to three sentences). No duplicates. Also give the set a short title."
    )
    return ai.complete_json(user, "flashcards", system=SYSTEM, prompt=_prompt(material, instruction),
                            schema=CARDS_SCHEMA, effort="low", validate=_valid_cards)


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
        choices = [str(c).strip()[:500] for c in q.get("choices") or [] if str(c).strip()]
        try:
            answer = int(q.get("answer"))
        except (TypeError, ValueError):
            continue
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


def generate_quiz(user: User, material: Material, count: int = 10) -> dict:
    count = max(3, min(int(count), 30))
    instruction = (
        f"Write {count} multiple-choice questions that test understanding of the material, not trivia. Each has "
        "exactly 4 choices with one correct answer; `answer` is the 0-based index of the correct choice. Wrong "
        "choices should be plausible misconceptions. Vary which position holds the correct answer. The "
        "explanation says why the answer is right. Also give the quiz a short title."
    )
    return ai.complete_json(user, "quiz", system=SYSTEM, prompt=_prompt(material, instruction),
                            schema=QUIZ_SCHEMA, effort="medium", validate=_valid_quiz)


# ---------------------------------------------------------------- summaries


def summarize(user: User, material: Material) -> str:
    instruction = (
        "Write a study summary of the material in Markdown: a two-sentence overview, then the key ideas as "
        "headed sections with bullet points, important terms in bold with brief definitions, and a final "
        "'Check yourself' list of 3-5 questions. Be concise."
    )
    return ai.complete(user, "summary", system=SYSTEM, prompt=_prompt(material, instruction), effort="low").text


# ---------------------------------------------------------------- rendering

_MD_TAGS = {"p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "h1", "h2", "h3", "h4", "blockquote",
            "a", "table", "thead", "tbody", "tr", "th", "td", "hr", "sup", "sub", "del"}


def render_markdown(text: str) -> str:
    """Markdown (from the AI) -> sanitized HTML. Math stays as $...$ for KaTeX to render."""
    html = md.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists"])
    return nh3.clean(html, tags=_MD_TAGS, attributes={"a": {"href"}}, url_schemes={"http", "https"},
                     link_rel="noopener noreferrer")


def material_options(user: User) -> dict:
    """Everything the generator form can draw from."""
    courses = db.session.scalars(select(Course).where(Course.user_id == user.id, Course.active.is_(True),
                                                      Course.hidden.is_(False)).order_by(Course.name)).all()
    course_ids = [c.id for c in courses]
    files = db.session.scalars(select(CanvasFile).where(CanvasFile.user_id == user.id,
                                                        CanvasFile.course_id.in_(course_ids),
                                                        CanvasFile.text.is_not(None)).order_by(CanvasFile.name)).all()
    pages = db.session.scalars(select(Page).where(Page.course_id.in_(course_ids), Page.body_html.is_not(None))
                               .order_by(Page.title)).all()
    return {"courses": courses, "files": files, "pages": pages}
