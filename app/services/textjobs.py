"""Reads text out of stored files in the background, so uploads return immediately.

Storing a file (a synced Canvas file or the student's own upload) marks it text_status="pending". `kick()` starts one reader thread per web
process; the thread claims pending files one at a time through the database (so several
processes never read the same file), downloads each from storage, hashes it, extracts its
text and rebuilds its search chunks. A claim older than STALE is taken over, so a file whose
reader died with a restart or deploy is picked up again by the next kick.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import closing
from datetime import timedelta

from flask import current_app
from sqlalchemy import and_, exists, or_, select, update

from ..extensions import db
from ..models import CanvasFile, Upload, utcnow
from . import retrieval
from .extract import extract_text
from .storage import get_storage

STALE = timedelta(minutes=10)
MODELS = (CanvasFile, Upload)
_lock = threading.Lock()


def _claimable(model, now):
    return and_(model.storage_key.is_not(None),
                or_(model.text_status == "pending",
                    and_(model.text_status == "extracting", model.text_started_at < now - STALE)))


def kick() -> None:
    """Make sure pending files get read. Cheap to call after every upload."""
    app = current_app._get_current_object()
    if app.config.get("EXTRACT_INLINE"):
        run_pending()
        return
    with _lock:
        if app.extensions.get("hh_text_reader"):
            return
        app.extensions["hh_text_reader"] = True
    threading.Thread(target=_reader, args=(app,), name="text-reader", daemon=True).start()


def _reader(app) -> None:
    with app.app_context():
        while True:
            try:
                run_pending()
            except Exception:
                app.logger.exception("background text reader failed")
            finally:
                db.session.remove()
            with _lock:
                app.extensions["hh_text_reader"] = False
            # A file marked pending after our last claim, whose kick saw us still running,
            # would otherwise wait for the next sync.
            try:
                now = utcnow()
                more = any(db.session.scalar(select(exists().where(_claimable(m, now)))) for m in MODELS)
            finally:
                db.session.remove()
            with _lock:
                if not more or app.extensions.get("hh_text_reader"):
                    return
                app.extensions["hh_text_reader"] = True


def run_pending(limit: int | None = None) -> int:
    done = 0
    while limit is None or done < limit:
        claim = _claim()
        if claim is None:
            break
        _read(*claim)
        done += 1
    return done


def _claim() -> tuple[type, int] | None:
    for model in MODELS:
        for _ in range(20):  # another process may win the race for the same row
            now = utcnow()
            row_id = db.session.scalar(select(model.id).where(_claimable(model, now)).order_by(model.id).limit(1))
            if row_id is None:
                db.session.rollback()
                break
            claimed = db.session.execute(
                update(model).where(model.id == row_id, _claimable(model, now))
                .values(text_status="extracting", text_started_at=now)
                .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if claimed:
                return model, row_id
    return None


def _read(model, row_id: int) -> None:
    row = db.session.get(model, row_id)
    key = row.storage_key
    limit = current_app.config["MAX_EXTRACT_MB"] * 1024 * 1024
    digest = hashlib.sha256()
    parts: list[bytes] | None = [] if (row.size or 0) <= limit else None
    total = 0
    try:
        with closing(get_storage().open(key)) as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
                if parts is not None:
                    parts.append(chunk)
                    if total > limit:
                        parts = None
    except Exception as exc:
        # Left "extracting": the claim goes stale and a later kick retries it.
        current_app.logger.warning("could not read %s for text: %s", key, exc)
        db.session.rollback()
        return
    if parts is None:
        text, status = None, "too_large"
    else:
        data = b"".join(parts)
        parts = None
        text, status = extract_text(data, row.name, row.content_type)
        del data
    db.session.refresh(row)
    if row.storage_key != key:  # a newer version arrived while we were reading; it has its own claim
        db.session.rollback()
        return
    row.sha256 = digest.hexdigest()
    row.size = total
    row.text, row.text_status, row.text_started_at = text, status, None
    retrieval.rebuild_chunks_for(row)
    db.session.commit()
