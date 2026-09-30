"""Storing the student's own files (from their computer or Google Drive)."""

from __future__ import annotations

import mimetypes

from flask import current_app

from ..extensions import db
from ..models import Upload, User
from . import textjobs
from .ingest import _spool
from .storage import get_storage, safe_key_part


def guess_type(name: str, content_type: str | None) -> str:
    if content_type and content_type not in ("application/octet-stream", "binary/octet-stream"):
        return content_type[:200]
    return mimetypes.guess_type(name or "")[0] or "application/octet-stream"


def store(user: User, stream, name: str, content_type: str | None, course_id: int | None,
          source: str = "upload", external_id: str | None = None) -> Upload:
    """Save one file and queue its text to be read. Raises storage.TooLarge over the limit."""
    name = (name or "file").strip().replace("/", "_")[:500] or "file"
    spool, size, digest = _spool(stream, current_app.config["MAX_FILE_MB"] * 1024 * 1024)
    row = Upload(user_id=user.id, course_id=course_id, name=name, content_type=guess_type(name, content_type),
                 size=size, sha256=digest, source=source, external_id=external_id, text_status="pending")
    db.session.add(row)
    db.session.flush()
    row.storage_key = f"u/{user.id}/uploads/{row.id}/{safe_key_part(name)}"
    try:
        get_storage().put_file(row.storage_key, spool, row.content_type)
    except Exception:
        db.session.rollback()
        raise
    finally:
        spool.close()
    db.session.commit()
    textjobs.kick()
    return row


def delete(row: Upload) -> None:
    key = row.storage_key
    from sqlalchemy import delete as sql_delete

    from ..models import ContentChunk
    db.session.execute(sql_delete(ContentChunk).where(ContentChunk.source_type == "upload", ContentChunk.source_id == row.id))
    db.session.delete(row)
    db.session.commit()
    if key:
        try:
            get_storage().delete_prefix(key)
        except Exception as exc:
            current_app.logger.warning("could not delete upload %s: %s", key, exc)
