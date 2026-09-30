"""Plain-text extraction from course files, so the tutor and generators can read them."""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)
# pypdf logs a warning for every malformed object in real-world PDFs, which floods the logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")
TEXT_EXTS = (".txt", ".md", ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".html", ".css", ".csv", ".json", ".r",
             ".m", ".sql", ".tex", ".rtf")
MAX_CHARS = 400_000


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # a single broken page shouldn't lose the rest
            continue
    return "\n\n".join(parts)


def _docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def _pptx(data: bytes) -> str:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    slides = []
    for n, slide in enumerate(deck.slides, 1):
        texts = [shape.text_frame.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)]
        slides.append(f"Slide {n}\n" + "\n".join(t for t in texts if t.strip()))
    return "\n\n".join(slides)


# Office files are zip archives: a few KB can inflate to gigabytes of XML ("zip bomb").
MAX_UNZIPPED_BYTES = 150 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


def _zip_too_big(data: bytes) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile:
        return False  # the parser will reject it on its own
    return total > MAX_UNZIPPED_BYTES or (len(data) and total / len(data) > MAX_COMPRESSION_RATIO)


def extract_text(data: bytes, name: str, content_type: str | None) -> tuple[str | None, str]:
    """Returns (text, status). Status is "ok", "empty", "unsupported" or "error"."""
    lower = (name or "").lower()
    ctype = (content_type or "").lower()
    office = lower.endswith((".docx", ".pptx")) or "officedocument" in ctype
    if office and _zip_too_big(data):
        return None, "too_large"
    try:
        if lower.endswith(".pdf") or ctype == "application/pdf":
            text = _pdf(data)
        elif lower.endswith(".docx") or "wordprocessingml" in ctype:
            text = _docx(data)
        elif lower.endswith(".pptx") or "presentationml" in ctype:
            text = _pptx(data)
        elif lower.endswith(TEXT_EXTS) or ctype.startswith(TEXT_TYPES):
            text = data.decode("utf-8", errors="replace")
        else:
            return None, "unsupported"
    except Exception as exc:
        log.warning("text extraction failed for %s: %s", name, exc)
        return None, "error"
    text = (text or "").replace("\x00", "").strip()
    if not text:
        return None, "empty"  # e.g. a scanned PDF with no text layer
    return text[:MAX_CHARS], "ok"
