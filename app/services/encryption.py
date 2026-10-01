"""Keeping stored data encrypted: key checks, migrating existing data, rotation and status.

New writes are encrypted by the column types (encrypted_types.py) and EncryptedStorage. This
module handles what was stored before, key changes, and the checks that stop a wrong key from
silently making data unreadable. In production the migration runs by itself in the background
after a deploy (one process at a time), so the key never has to leave the server.
"""

from __future__ import annotations

import logging
import threading

import click
from flask import Flask, current_app
from sqlalchemy import Text, and_, cast, inspect, literal, select, text, update

from ..encrypted_types import _Encrypted
from ..extensions import db
from ..models import AppState, EncryptionKey, utcnow
from . import crypto
from .storage import EncryptedStorage, get_storage

log = logging.getLogger(__name__)
LOCK_ID = 727274  # Postgres advisory lock: one migration at a time across workers
BATCH = 200


class KeyMismatch(RuntimeError):
    pass


# ---------------------------------------------------------------- keys


def check_registry(ring: crypto.Keyring) -> None:
    """Configured keys must match every key the data was written with (by check value)."""
    if not inspect(db.engine).has_table("encryption_key"):
        return  # before the first migration
    known = {r.kid: r.check for r in db.session.scalars(select(EncryptionKey))}
    configured = ring.checks()
    wrong = [kid for kid, check in configured.items() if kid in known and known[kid] != check]
    if wrong:
        raise KeyMismatch(f"encryption key {', '.join(wrong)} isn't the key the data was written with. "
                          "Check ENCRYPTION_KEYS against your saved copy (`flask encryption status` shows check values).")
    missing = sorted(set(known) - set(configured))
    if missing:
        raise KeyMismatch(f"encryption key {', '.join(missing)} was used for stored data but is missing from "
                          "ENCRYPTION_KEYS. Put it back (decrypt-only, after the active key).")
    for kid, check in configured.items():
        if kid not in known:
            db.session.add(EncryptionKey(kid=kid, check=check))
    db.session.commit()


def startup(app: Flask) -> None:
    """Configure the keyring and refuse to start with a key that doesn't match the data."""
    ring = crypto.configure(app.config.get("ENCRYPTION_KEYS"), app.config.get("ENCRYPTION_STRICT"))
    if ring is None:
        return
    ring.selftest()
    with app.app_context():
        try:
            check_registry(ring)
        finally:
            db.session.remove()
            db.engine.dispose()  # gunicorn forks after this (preload): don't share connections


# ---------------------------------------------------------------- fields


def encrypted_columns():
    return [(table, col) for table in db.metadata.sorted_tables for col in table.columns if isinstance(col.type, _Encrypted)]


def _pending(col, mode: str, active: str):
    raw = cast(col, Text)
    if mode == "encrypt":
        return and_(col.is_not(None), ~raw.like("enc1:%"))
    if mode == "rotate":
        return and_(raw.like("enc1:%"), ~raw.like(f"enc1:{active}:%"))
    return raw.like("enc1:%")  # decrypt


def convert_column(table, col, mode: str) -> int:
    """Encrypt (or rotate, or decrypt) one column's stored values in batches. Each row is updated
    only if it still holds the value that was read (compare-and-swap), so a concurrent write wins
    and is never overwritten; re-running is always safe."""
    ring = crypto.require_keyring()
    pk = table.c.id
    keep = {"updated_at": table.c.updated_at} if "updated_at" in table.c else {}
    dialect = db.engine.dialect
    changed = 0
    while True:
        rows = db.session.execute(select(pk, cast(col, Text)).where(_pending(col, mode, ring.active.kid))
                                  .order_by(pk).limit(BATCH)).all()
        if not rows:
            return changed
        progress = 0
        for row_id, raw in rows:
            if mode == "encrypt":
                value = col.type.legacy(raw)
            else:
                plain = col.type.process_result_value(raw, dialect)
                value = plain if mode == "rotate" else literal(col.type.plain_text(plain), Text)
            result = db.session.execute(update(table).where(pk == row_id, cast(col, Text) == raw)
                                        .values({col.name: value, **keep}))
            progress += result.rowcount
        db.session.commit()
        changed += progress
        if progress == 0:
            return changed  # every row changed under us; the next run picks them up


def field_status() -> dict:
    ring = crypto.require_keyring()
    out = {}
    for table, col in encrypted_columns():
        raw = cast(col, Text)
        plain = db.session.scalar(select(db.func.count()).where(and_(col.is_not(None), ~raw.like("enc1:%")))) or 0
        old = db.session.scalar(select(db.func.count()).where(_pending(col, "rotate", ring.active.kid))) or 0
        if plain or old:
            out[f"{table.name}.{col.name}"] = {"unencrypted": plain, "old_key": old}
    return out


# ---------------------------------------------------------------- files


def convert_objects(seal: bool) -> dict:
    st = get_storage()
    if not isinstance(st, EncryptedStorage):
        raise crypto.CryptoError("storage isn't encrypted (no keys configured)")
    changed, failed, total = 0, [], 0
    for key in list(st.list_keys("")):
        total += 1
        try:
            changed += st.rewrite(key, seal=seal)
        except Exception as exc:  # one bad object mustn't stop the rest; it's reported and retried next run
            log.error("encryption: couldn't %s %s: %s", "seal" if seal else "unseal", key, exc)
            failed.append(key)
    return {"objects": total, "changed": changed, "failed": failed}


def object_status() -> dict:
    st = get_storage()
    ring = crypto.require_keyring()
    counts = {"objects": 0, "unencrypted": 0, "old_key": 0}
    for key in st.list_keys(""):
        counts["objects"] += 1
        kid = st.sealed_kid(key)
        if kid is None:
            counts["unencrypted"] += 1
        elif kid != ring.active.kid:
            counts["old_key"] += 1
    return counts


# ---------------------------------------------------------------- the whole job


def _save_state(value: dict) -> None:
    row = db.session.get(AppState, "encryption") or AppState(key="encryption")
    row.value = value
    db.session.merge(row)
    db.session.commit()


def state() -> dict | None:
    row = db.session.get(AppState, "encryption")
    return row.value if row else None


def migrate_all(rotate: bool = True) -> dict:
    """Encrypt everything still stored unencrypted (and re-encrypt anything under an old key)."""
    fields = {}
    for table, col in encrypted_columns():
        n = convert_column(table, col, "encrypt") + (convert_column(table, col, "rotate") if rotate else 0)
        if n:
            fields[f"{table.name}.{col.name}"] = n
    objects = convert_objects(seal=True)
    remaining = field_status()
    previous = state() or {}
    result = {"at": utcnow().isoformat() + "Z", "fields_changed": fields, "objects": objects,
              "fields_remaining": remaining, "done": not remaining and not objects["failed"],
              "vacuumed": previous.get("vacuumed") if not fields else None}
    _save_state(result)
    if result["done"] and not result["vacuumed"] and db.engine.dialect.name == "postgresql":
        _vacuum()
    return result


def _vacuum() -> None:
    """Rewrite tables once so old unencrypted row versions don't linger in the database files."""
    tables = sorted({t.name for t, _ in encrypted_columns()})
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for name in tables:
            conn.execute(text(f'VACUUM (FULL, ANALYZE) "{name}"'))
    value = state() or {}
    value["vacuumed"] = utcnow().isoformat() + "Z"
    _save_state(value)


_kicked = False
_kick_lock = threading.Lock()


def kick(app: Flask) -> None:
    """Start the background migration once per process (the first request after a deploy)."""
    global _kicked
    if crypto.keyring() is None or app.testing:
        return
    with _kick_lock:
        if _kicked:
            return
        _kicked = True
    threading.Thread(target=_background, args=(app,), name="encryption-migrate", daemon=True).start()


def _background(app: Flask) -> None:
    with app.app_context():
        conn = None
        try:
            if db.engine.dialect.name == "postgresql":
                conn = db.engine.connect()
                if not conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_ID}).scalar():
                    return  # another worker is on it
            result = migrate_all()
            log.info("encryption: %s", "all stored data is encrypted" if result["done"] else f"not done: {result}")
        except Exception:
            log.exception("encryption: background migration failed; it retries after the next restart")
        finally:
            if conn is not None:
                try:
                    conn.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                finally:
                    conn.close()
            db.session.remove()


# ---------------------------------------------------------------- CLI


def register_cli(app: Flask) -> None:
    @app.cli.group("encryption")
    def group():
        """Encryption keys and stored data (see SECURITY.md)."""

    @group.command("status")
    @click.option("--objects/--no-objects", default=True, help="Also check every stored file.")
    def status_cmd(objects):
        ring = crypto.require_keyring()
        click.echo(f"active key: {ring.active.kid}   strict: {'on' if crypto.strict() else 'off'}")
        for kid, check in ring.checks().items():
            click.echo(f"  key {kid}: check value {check}")
        fields = field_status()
        click.echo("database: " + ("everything encrypted with the active key" if not fields else str(fields)))
        if objects:
            click.echo(f"files: {object_status()}")
        click.echo(f"last migration: {state()}")
        if fields:
            raise SystemExit(1)

    @group.command("migrate")
    def migrate_cmd():
        """Encrypt everything stored unencrypted, and move old-key data to the active key."""
        result = migrate_all()
        click.echo(result)
        if not result["done"]:
            raise SystemExit(1)

    @group.command("decrypt-all")
    @click.option("--yes", is_flag=True, help="Really store everything unencrypted again.")
    def decrypt_cmd(yes):
        """Undo: store everything unencrypted (only before deploying code without encryption)."""
        if not yes:
            raise SystemExit("Refusing without --yes.")
        if crypto.strict():
            raise SystemExit("Turn ENCRYPTION_STRICT off first.")
        n = sum(convert_column(t, c, "decrypt") for t, c in encrypted_columns())
        click.echo(f"fields decrypted: {n}; files: {convert_objects(seal=False)}")

    @group.command("retire")
    @click.argument("kid")
    def retire_cmd(kid):
        """Forget an old key once nothing uses it (then remove it from ENCRYPTION_KEYS, keep your copy)."""
        ring = crypto.require_keyring()
        if kid == ring.active.kid:
            raise SystemExit("That's the active key.")
        if field_status() or object_status()["old_key"]:
            raise SystemExit("Data still uses an old key; run `flask encryption migrate` first.")
        db.session.execute(EncryptionKey.__table__.delete().where(EncryptionKey.kid == kid))
        db.session.commit()
        click.echo(f"Key {kid} retired. Remove it from ENCRYPTION_KEYS after your backups older than today expire.")

    @group.command("new-key")
    @click.argument("kid")
    def new_key_cmd(kid):
        """Print a new key entry (for local development; never for production in a shared terminal)."""
        click.echo(crypto.new_key_spec(kid))
