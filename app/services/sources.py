"""Study sources a student can pick for flashcards, quizzes and tutor chats: a synced Canvas
file, a Canvas page, or one of their own uploads (from their computer or Google Drive).

A source is named by a ref string: "file:12", "page:3", "upload:7". Lists never load file
text (it's deferred and can be large); `texts()` loads it only for the refs asked for.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from flask import url_for
from sqlalchemy import select

from ..extensions import db
from ..models import CanvasFile, Course, Page, Upload, User
from ..utils import html_to_text, parse_id

READY = {"ok", "ai"}
READING = {"pending", "extracting"}
KINDS = ("file", "page", "upload")


@dataclass
class Source:
    ref: str
    kind: str
    title: str
    course_id: int | None
    status: str  # ready | reading | unreadable
    note: str  # why it isn't ready, for the picker
    url: str | None
    size: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _file_status(text_status: str | None, stored: bool) -> tuple[str, str]:
    if text_status in READY:
        return "ready", ""
    if text_status in READING or (stored and text_status is None):
        return "reading", "Reading the text… ready in a minute."
    if not stored:
        return "unreadable", "Not uploaded yet."
    return "unreadable", {
        "empty": "A scan with no text layer. Use “Read this scan with AI” on its page first.",
        "unsupported": "This file type can't be read (PDF, Word, PowerPoint and text files work).",
        "too_large": "Too large to read.",
        "error": "Couldn't be read.",
    }.get(text_status or "", "No readable text.")


def for_course(user: User, course_id: int | None) -> list[Source]:
    """Everything pickable in one class, or the student's unfiled uploads when course_id is None."""
    out: list[Source] = []
    if course_id is not None:
        course = db.session.get(Course, course_id)
        if course is None or course.user_id != user.id:
            return []
        for f in course.listed_files:
            status, note = _file_status(f.text_status, bool(f.storage_key))
            out.append(Source(f"file:{f.id}", "file", f.name, course.id, status, note,
                              url_for("courses.file_detail", file_id=f.id), f.size))
        pages = db.session.scalars(select(Page).where(Page.course_id == course.id, Page.body_html.is_not(None))
                                   .order_by(Page.title)).all()
        for p in pages:
            out.append(Source(f"page:{p.id}", "page", p.title, course.id, "ready", "",
                              url_for("courses.page", page_id=p.id)))
    uploads = db.session.scalars(select(Upload).where(Upload.user_id == user.id, Upload.course_id.is_(course_id)
                                                      if course_id is None else Upload.course_id == course_id)
                                 .order_by(Upload.created_at.desc())).all()
    for u in uploads:
        status, note = _file_status(u.text_status, bool(u.storage_key))
        out.append(Source(f"upload:{u.id}", "upload", u.name, u.course_id, status, note,
                          url_for("uploads.download", upload_id=u.id), u.size))
    return out


def parse(refs) -> list[tuple[str, int]]:
    out, seen = [], set()
    for ref in refs or []:
        kind, _, ident = str(ref).partition(":")
        number = parse_id(ident)  # not str.isdigit(): "²" passes that and crashes int()
        if kind in KINDS and number is not None and (kind, number) not in seen:
            seen.add((kind, number))
            out.append((kind, number))
    return out[:40]


def texts(user: User, refs) -> list[tuple[Source, str]]:
    """(source, text) for each readable ref the student owns, in the order given."""
    out = []
    for kind, ident in parse(refs):
        if kind == "file":
            f = db.session.get(CanvasFile, ident)
            if f is None or f.user_id != user.id or not f.text:
                continue
            src = Source(f"file:{f.id}", kind, f.name, f.course_id, "ready", "",
                         url_for("courses.file_detail", file_id=f.id), f.size)
            out.append((src, f.text))
        elif kind == "page":
            p = db.session.get(Page, ident)
            if p is None or db.session.get(Course, p.course_id).user_id != user.id:
                continue
            text = html_to_text(p.body_html)
            if text:
                out.append((Source(f"page:{p.id}", kind, p.title, p.course_id, "ready", "",
                                   url_for("courses.page", page_id=p.id)), text))
        else:
            u = db.session.get(Upload, ident)
            if u is None or u.user_id != user.id or not u.text:
                continue
            out.append((Source(f"upload:{u.id}", kind, u.name, u.course_id, "ready", "",
                               url_for("uploads.download", upload_id=u.id), u.size), u.text))
    return out


def describe(user: User, refs) -> list[Source]:
    """Titles and status for refs (attached to a chat, say) without loading their text."""
    out = []
    for kind, ident in parse(refs):
        model = {"file": CanvasFile, "page": Page, "upload": Upload}[kind]
        row = db.session.get(model, ident)
        if row is None:
            continue
        owner = db.session.get(Course, row.course_id).user_id if kind == "page" else row.user_id
        if owner != user.id:
            continue
        if kind == "page":
            out.append(Source(f"page:{row.id}", kind, row.title, row.course_id, "ready", "",
                              url_for("courses.page", page_id=row.id)))
        else:
            status, note = _file_status(row.text_status, bool(row.storage_key))
            url = (url_for("courses.file_detail", file_id=row.id) if kind == "file"
                   else url_for("uploads.download", upload_id=row.id))
            out.append(Source(f"{kind}:{row.id}", kind, row.name, row.course_id, status, note, url, row.size))
    return out


def fair_share(lengths: list[int], budget: int) -> list[int]:
    """How many characters each source gets: short sources keep everything, and what they
    don't use is split evenly among the longer ones (so every picked file contributes)."""
    caps = [0] * len(lengths)
    remaining = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for n, i in enumerate(order):
        caps[i] = min(lengths[i], remaining // (len(order) - n))
        remaining -= caps[i]
    return caps
