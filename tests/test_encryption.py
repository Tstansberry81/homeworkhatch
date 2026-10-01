"""Application-level encryption: the crypto core, encrypted columns and files, and migration."""

import io
import os

import pytest
from sqlalchemy import Text, cast, select, text

from app.encrypted_types import LABELS, _Encrypted
from app.extensions import db
from app.models import Assignment, CanvasFile, ChatMessage, Course, EncryptionKey, User
from app.services import crypto, encryption
from app.services.storage import EncryptedStorage, get_storage

from .conftest import api_token, login, make_user, sync


def test_crypto_core_detects_tampering_wrong_keys_and_moved_files():
    a, b = crypto.new_key_spec("k1"), crypto.new_key_spec("k2")
    one, rotated, other = crypto.Keyring(a), crypto.Keyring(f"{b},{a}"), crypto.Keyring(b)
    token = one.encrypt_field("B+", "assignment.grade")
    assert token.startswith("enc1:k1:") and rotated.decrypt_field(token, "assignment.grade") == "B+"
    assert len(one.encrypt_field("A", "x")) == len(one.encrypt_field("x" * 20, "x")), "short values all look alike"
    for bad in (lambda: one.decrypt_field(token, "assignment.score"),          # moved to another column
                lambda: other.decrypt_field(token, "assignment.grade"),         # wrong key
                lambda: one.decrypt_field(token.replace("enc1:k1:", "enc1:k2:"), "assignment.grade")):
        with pytest.raises(crypto.CryptoError):
            bad()
    data = os.urandom(3 * crypto.SEGMENT + 5)
    sealed = one.seal_bytes(data, "u/1/files/a.pdf")
    assert rotated.open_bytes(sealed, "u/1/files/a.pdf") == data
    for bad in (sealed[:-1], sealed + b"x", sealed[:200] + bytes([sealed[200] ^ 1]) + sealed[201:]):
        with pytest.raises(crypto.CryptoError):
            one.open_bytes(bad, "u/1/files/a.pdf")
    with pytest.raises(crypto.CryptoError):
        one.open_bytes(sealed, "u/2/files/a.pdf")  # copied into another student's folder
    with pytest.raises(ValueError):
        crypto.Keyring("K1:short")


def test_every_encrypted_column_has_its_own_label_and_no_length_limit():
    cols = encryption.encrypted_columns()
    labels = [c.type.aad for _, c in cols]
    assert len(labels) == len(set(labels)) == len(LABELS) >= 30
    for table, col in cols:
        assert isinstance(col.type, _Encrypted) and col.type.impl.length is None, f"{table.name}.{col.name}"


def test_sensitive_values_are_ciphertext_in_the_database(app, client, snapshot, manifest):
    user = make_user()
    sync(client, api_token(user), snapshot, manifest, {"9002": b"The chain rule multiplies derivatives."})
    a = db.session.scalar(select(Assignment).where(Assignment.score.is_not(None)))
    assert isinstance(a.score, float), "the app sees normal values"
    raw = lambda col, row_id: db.session.execute(  # noqa: E731
        select(cast(col, Text)).where(col.class_.id == row_id)).scalar()
    assert raw(Assignment.score, a.id).startswith("enc1:t1:") and str(a.score) not in raw(Assignment.score, a.id)
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    assert "chain rule" in f.text and raw(CanvasFile.text, f.id).startswith("enc1:")
    u = db.session.get(User, user.id)
    assert raw(User.calendar_token, u.id).startswith("enc1:") and u.calendar_token_hash
    assert client.get(f"/calendar/{u.calendar_token}.ics").status_code == 200, "the feed is found by its hash"
    # Search still works over encrypted chunk text.
    from app.services import retrieval

    assert retrieval.search(user.id, "chain rule"), "matching happens after decrypting"
    # Files are sealed at rest and bound to their path.
    st = get_storage()
    assert isinstance(st, EncryptedStorage) and st.backend.read(f.storage_key).startswith(crypto.MAGIC)
    login(client, user)
    assert client.get(f"/courses/files/{f.id}/download").data == b"The chain rule multiplies derivatives."


def test_old_unencrypted_rows_and_files_are_migrated_and_strict_mode_refuses_leftovers(app, client, snapshot, manifest):
    user = make_user()
    sync(client, api_token(user), snapshot, manifest)
    course = db.session.scalar(select(Course).where(Course.current_score.is_not(None)))
    # Simulate data stored before encryption: plain text in the columns, a plain object in storage.
    db.session.execute(text("UPDATE course SET current_score = '87.5', syllabus_html = '<p>Week 1</p>' WHERE id = :i"),
                       {"i": course.id})
    db.session.execute(text("UPDATE chat_message SET body = 'x'"))
    msg = ChatMessage(room_key="r", user_id=user.id, body="placeholder")
    db.session.add(msg)
    db.session.commit()
    db.session.execute(text("UPDATE chat_message SET body = 'see you at office hours' WHERE id = :i"), {"i": msg.id})
    db.session.commit()
    st = get_storage()
    st.backend.put_bytes("u/1/files/old/plain.txt", b"stored before encryption")
    db.session.expire_all()
    assert db.session.get(Course, course.id).current_score == 87.5, "legacy plaintext still reads"
    assert encryption.field_status()

    result = encryption.migrate_all()
    assert result["done"] and not encryption.field_status()
    assert encryption.migrate_all()["fields_changed"] == {}, "re-running changes nothing"
    raw = db.session.execute(text("SELECT current_score, syllabus_html FROM course WHERE id = :i"), {"i": course.id}).one()
    assert all(v.startswith("enc1:") for v in raw)
    db.session.expire_all()
    c = db.session.get(Course, course.id)
    assert (c.current_score, c.syllabus_html) == (87.5, "<p>Week 1</p>")
    assert db.session.get(ChatMessage, msg.id).body == "see you at office hours"
    assert st.backend.read("u/1/files/old/plain.txt").startswith(crypto.MAGIC)
    assert st.read("u/1/files/old/plain.txt") == b"stored before encryption"

    crypto.configure(app.config["ENCRYPTION_KEYS"], strict=True)
    try:
        db.session.execute(text("UPDATE course SET final_grade = 'A' WHERE id = :i"), {"i": course.id})
        db.session.commit()
        db.session.expire_all()
        with pytest.raises(crypto.CryptoError):
            db.session.get(Course, course.id).final_grade
        st.backend.put_bytes("u/1/files/old/sneaky.txt", b"planted")
        with pytest.raises(crypto.CryptoError):
            st.read("u/1/files/old/sneaky.txt")
    finally:
        db.session.rollback()
        crypto.configure(app.config["ENCRYPTION_KEYS"], strict=False)


def test_a_wrong_or_missing_key_stops_the_app(app):
    ring = crypto.keyring()
    db.session.add(EncryptionKey(kid="t1", check=ring.checks()["t1"]))
    db.session.commit()
    encryption.check_registry(ring)  # the right key passes
    with pytest.raises(encryption.KeyMismatch, match="isn't the key"):
        encryption.check_registry(crypto.Keyring(crypto.new_key_spec("t1")))
    with pytest.raises(encryption.KeyMismatch, match="missing"):
        encryption.check_registry(crypto.Keyring(crypto.new_key_spec("t2")))


def test_rotation_moves_everything_to_the_new_key(app, client, snapshot, manifest):
    user = make_user()
    sync(client, api_token(user), snapshot, manifest, {"9002": b"rotate me"})
    old = app.config["ENCRYPTION_KEYS"]
    crypto.configure(f"{crypto.new_key_spec('t2')},{old}")
    try:
        assert encryption.field_status(), "values under the old key are reported"
        assert encryption.migrate_all()["done"] and not encryption.field_status()
        f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
        assert get_storage().sealed_kid(f.storage_key) == "t2" and get_storage().read(f.storage_key) == b"rotate me"
        db.session.expire_all()
        assert "rotate me" in db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002")).text
    finally:
        crypto.configure(old)
