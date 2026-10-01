"""Course-material search for the AI tutor and generators.

Text from syllabi, pages, assignment descriptions, announcements and extracted files is
split into chunks and ranked with BM25. Per-student corpora are small (hundreds to a few
thousand chunks), so an in-process ranker keeps the stack free of a vector database.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from flask import current_app
from sqlalchemy import delete, select
from sqlalchemy.orm import undefer

from ..extensions import db
from ..models import Assignment, CanvasFile, ContentChunk, Course
from ..utils import html_to_text

_WORD = re.compile(r"[a-z0-9][a-z0-9'+#.-]*[a-z0-9+#]|[a-z0-9]")
STOPWORDS = set("""a an and are as at be but by can do does for from has have how i if in into is it its me my
not of on or our so that the their them then there these they this to was we what when where which who why will
with you your about also any all been more most other some such than too very just should would could""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > size:  # hard-wrap giant paragraphs (e.g. PDFs with no blank lines)
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:cut].strip())
            para = para[max(cut - overlap, 0):].strip()
        if len(current) + len(para) + 2 > size and current:
            chunks.append(current)
            tail = current[-overlap:]
            current = (tail[tail.find(" ") + 1:] if " " in tail else "") + "\n\n" + para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _course_sources(course: Course):
    if course.syllabus_html:
        yield "syllabus", course.id, f"{course.name} — Syllabus", course.html_url, html_to_text(course.syllabus_html)
    for page in course.pages:
        if page.body_html:
            yield "page", page.id, page.title, page.html_url, html_to_text(page.body_html)
    for a in db.session.scalars(select(Assignment).options(undefer(Assignment.description_html))
                                .where(Assignment.course_id == course.id).order_by(Assignment.due_at)):
        if a.description_html:
            yield "assignment", a.id, a.name, a.html_url, html_to_text(a.description_html)
    for ann in course.announcements:
        if ann.message_html:
            yield "announcement", ann.id, ann.title, ann.html_url, html_to_text(ann.message_html)


def rebuild_course_chunks(course: Course, force: bool = False) -> bool:
    """Re-index a course's HTML content (not files). Skips work when nothing changed."""
    sources = list(_course_sources(course))
    h = hashlib.sha256()
    for kind, sid, title, _url, text in sources:
        h.update(f"{kind}\x00{sid}\x00{title}\x00{text}\x00".encode())
    signature = h.hexdigest()
    if not force and course.content_signature == signature:
        return False
    db.session.execute(delete(ContentChunk).where(ContentChunk.course_id == course.id,
                                                  ContentChunk.source_type != "file"))
    for kind, sid, title, url, text in sources:
        for n, piece in enumerate(chunk_text(text)):
            db.session.add(ContentChunk(user_id=course.user_id, course_id=course.id, source_type=kind,
                                        source_id=sid, title=title[:500], url=url, ordinal=n, text=piece))
    course.content_signature = signature
    return True


def rebuild_file_chunks(f: CanvasFile, source_type: str = "file") -> None:
    """Search chunks for a file's text. Files outside any class (an upload not filed under a
    class) aren't searched; they reach the tutor only when attached to a chat."""
    db.session.execute(delete(ContentChunk).where(ContentChunk.source_type == source_type, ContentChunk.source_id == f.id))
    if not f.text or not f.course_id:
        return
    for n, piece in enumerate(chunk_text(f.text)):
        db.session.add(ContentChunk(user_id=f.user_id, course_id=f.course_id, source_type=source_type, source_id=f.id,
                                    title=f.name[:500], url=None, ordinal=n, text=piece))


def rebuild_chunks_for(row) -> None:
    """A Canvas file or one of the student's own uploads."""
    rebuild_file_chunks(row, "upload" if row.__class__.__name__ == "Upload" else "file")


def search(user_id: int, query: str, course_ids: list[int] | None = None, k: int = 8,
           max_candidates: int = 20000, per_source: int = 3) -> list[ContentChunk]:
    """BM25 over the student's chunks (optionally limited to some courses).

    Chunk text is encrypted, so matching happens here after decrypting rather than in SQL (a
    student has hundreds to a few thousand chunks). At most `per_source` chunks per file/page are
    returned so one long document can't crowd out everything else.
    """
    q_terms = tokenize(query)
    if not q_terms:
        return []
    stmt = select(ContentChunk).where(ContentChunk.user_id == user_id)
    if course_ids is not None:
        if not course_ids:
            return []
        stmt = stmt.where(ContentChunk.course_id.in_(course_ids))
    chunks = db.session.scalars(stmt.order_by(ContentChunk.id.desc()).limit(max_candidates)).all()
    if len(chunks) == max_candidates:
        current_app.logger.warning("search for user %s hit the %s-chunk cap; oldest chunks skipped", user_id, max_candidates)
    wanted = set(q_terms)
    # Cheap substring test first: a chunk can only contain a query token if it contains it as a
    # substring, so most chunks are skipped without running the tokenizer (same results).
    pairs = [(c, Counter(tokenize(c.text) + tokenize(c.title) * 2)) for c in chunks
             if any(w in c.text.lower() or w in c.title.lower() for w in wanted)]
    pairs = [(c, d) for c, d in pairs if wanted & d.keys()]
    if not pairs:
        return []
    chunks = [c for c, _ in pairs]
    docs = [d for _, d in pairs]
    n = len(docs)
    avg_len = sum(sum(d.values()) for d in docs) / n or 1.0
    df = Counter()
    for d in docs:
        df.update(set(d))
    k1, b = 1.4, 0.75
    q_counts = Counter(q_terms)
    scored = []
    for chunk, d in zip(chunks, docs):
        length = sum(d.values()) or 1
        score = 0.0
        for term, qf in q_counts.items():
            tf = d.get(term)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += qf * idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / avg_len))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    picked, per = [], Counter()
    for _score, chunk in scored:
        key = (chunk.source_type, chunk.source_id)
        if per[key] >= per_source:
            continue
        per[key] += 1
        picked.append(chunk)
        if len(picked) == k:
            break
    return picked
