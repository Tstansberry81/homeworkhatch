"""Application-level encryption: database fields and stored files (see SECURITY.md).

Keys come from ENCRYPTION_KEYS ("kid:base64key,..."; 32-byte keys, the first one encrypts, the
rest only decrypt, so keys can be rotated). Each master key is stretched with HKDF into separate
subkeys for fields, for wrapping file keys, and for blind indexes, so no key is used for two jobs.

- Fields: AES-256-GCM with a random 96-bit nonce; the format version, key id and column name are
  authenticated as associated data (a value copied into another column won't decrypt), and values
  are padded to a multiple of 32 bytes so their length doesn't give away short values like grades.
  Stored as text: "enc1:<kid>:<base64url(nonce|ciphertext|tag)>".
- Files: a random 256-bit data key per object, wrapped with the file subkey; the body is split into
  64 KiB segments, each sealed with AES-256-GCM (the STREAM construction: a counter and a "last
  segment" flag in every nonce, the header and the object's storage path as associated data), so
  reordering, truncating, appending to or moving a file is detected, and big files stream without
  sitting in memory.
- Key check values identify a key without revealing it, so a mistyped or swapped key is caught at
  startup instead of silently making data unreadable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import struct
from dataclasses import dataclass
from typing import IO, Callable, Iterable, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FIELD_PREFIX = "enc1:"
MAGIC = b"\x89HHENC1\n"  # starts every sealed file; a plain file starting with these 8 bytes is not a concern
SEGMENT = 64 * 1024
TAG = 16
NONCE_PREFIX = 7  # + 4-byte counter + 1-byte last flag = the 12-byte GCM nonce
MAX_SEGMENTS = 2**32


class CryptoError(Exception):
    """Ciphertext that doesn't decrypt: wrong key, unknown key id, or tampered/truncated data."""


PAD = 32


@dataclass(frozen=True)
class _Key:
    kid: str
    field: AESGCM
    kek: AESGCM
    check: str


def _subkey(master: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"homework-hatch", info=label).derive(master)


def _b64decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))


def new_key_spec(kid: str) -> str:
    """A fresh key entry for ENCRYPTION_KEYS (generate it, store it, never print it)."""
    return f"{kid}:{base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')}"


class Keyring:
    def __init__(self, spec: str):
        keys: list[_Key] = []
        for part in (spec or "").split(","):
            part = part.strip()
            if not part:
                continue
            kid, sep, encoded = part.partition(":")
            if not sep or not (kid.isascii() and kid.isalnum() and kid.islower() or kid.isdigit()) or len(kid) > 16:
                raise ValueError("ENCRYPTION_KEYS entries look like kid:base64key, with a short lowercase kid like k1")
            try:
                master = _b64decode(encoded)
            except ValueError as exc:
                raise ValueError(f"encryption key {kid} isn't valid base64") from exc
            if len(master) != 32:
                raise ValueError(f"encryption key {kid} must be 32 bytes")
            check = hmac.new(_subkey(master, b"hh/check/v1"), b"hh-key-check", hashlib.sha256).hexdigest()[:16]
            keys.append(_Key(kid, AESGCM(_subkey(master, b"hh/field/v1")), AESGCM(_subkey(master, b"hh/file/v1")), check))
        if not keys:
            raise ValueError("ENCRYPTION_KEYS is empty")
        if len({k.kid for k in keys}) != len(keys):
            raise ValueError("ENCRYPTION_KEYS has a repeated key id")
        self.keys = keys
        self.active = keys[0]
        self._by_kid = {k.kid: k for k in keys}

    def _key(self, kid: str) -> _Key:
        key = self._by_kid.get(kid)
        if key is None:
            raise CryptoError(f"no encryption key with id {kid!r} (was it removed from ENCRYPTION_KEYS?)")
        return key

    def checks(self) -> dict[str, str]:
        """kid -> key check value (safe to show and store; it doesn't reveal the key)."""
        return {k.kid: k.check for k in self.keys}

    # ---------------------------------------------------------------- fields

    @staticmethod
    def _field_aad(kid: str, aad: str) -> bytes:
        return b"hh|enc1|" + kid.encode() + b"|" + aad.encode()

    def encrypt_field(self, plaintext: str, aad: str) -> str:
        data = plaintext.encode("utf-8") + b"\x80"
        data += b"\x00" * (-len(data) % PAD)
        nonce = os.urandom(12)
        sealed = self.active.field.encrypt(nonce, data, self._field_aad(self.active.kid, aad))
        return f"{FIELD_PREFIX}{self.active.kid}:{base64.urlsafe_b64encode(nonce + sealed).decode().rstrip('=')}"

    def decrypt_field(self, token: str, aad: str) -> str:
        try:
            _, kid, body = token.split(":", 2)
            raw = _b64decode(body)
            if len(raw) < 12 + PAD + TAG:
                raise ValueError("too short")
            data = self._key(kid).field.decrypt(raw[:12], raw[12:], self._field_aad(kid, aad)).rstrip(b"\x00")
            if not data.endswith(b"\x80"):
                raise ValueError("bad padding")
            return data[:-1].decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise CryptoError(f"a value in {aad} doesn't decrypt") from exc

    @staticmethod
    def is_field_ciphertext(value) -> bool:
        return isinstance(value, str) and value.startswith(FIELD_PREFIX)

    @staticmethod
    def field_kid(token: str) -> str:
        return token.split(":", 2)[1]

    # ---------------------------------------------------------------- files

    def seal(self, chunks: Iterable[bytes], path: str) -> Iterator[bytes]:
        """Encrypt a byte stream stored at `path`; yields the sealed object piece by piece."""
        dek = os.urandom(32)
        wrap_nonce = os.urandom(12)
        kid = self.active.kid.encode()
        bound = b"|" + path.encode("utf-8")
        wrapped = self.active.kek.encrypt(wrap_nonce, dek, MAGIC + kid + bound)
        prefix = os.urandom(NONCE_PREFIX)
        header = MAGIC + bytes([len(kid)]) + kid + wrap_nonce + wrapped + prefix
        yield header
        aad = header + bound
        aead = AESGCM(dek)
        counter = 0
        pending = b""
        held: bytes | None = None  # one full segment held back until we know whether it's the last
        for chunk in chunks:
            pending += chunk
            while len(pending) >= SEGMENT:
                if held is not None:
                    yield aead.encrypt(_nonce(prefix, counter, False), held, aad)
                    counter += 1
                held, pending = pending[:SEGMENT], pending[SEGMENT:]
        if held is not None and pending:
            yield aead.encrypt(_nonce(prefix, counter, False), held, aad)
            counter += 1
            held = None
        last = held if held is not None else pending
        if counter >= MAX_SEGMENTS:
            raise CryptoError("file too large to seal")
        yield aead.encrypt(_nonce(prefix, counter, True), last, aad)

    def open(self, read: Callable[[int], bytes], path: str) -> Iterator[bytes]:
        """Decrypt a sealed stream stored at `path`, given a read(n) function; yields plaintext."""
        head = _read_exact(read, len(MAGIC) + 1)
        if head[:len(MAGIC)] != MAGIC:
            raise CryptoError("not a sealed file")
        kid_len = head[-1]
        kid = _read_exact(read, kid_len)
        rest = _read_exact(read, 12 + 32 + TAG + NONCE_PREFIX)
        wrap_nonce, wrapped, prefix = rest[:12], rest[12:12 + 32 + TAG], rest[12 + 32 + TAG:]
        header = head + kid + rest
        bound = b"|" + path.encode("utf-8")
        aad = header + bound
        try:
            dek = self._key(kid.decode()).kek.decrypt(wrap_nonce, wrapped, MAGIC + kid + bound)
        except InvalidTag as exc:
            raise CryptoError("the file's data key doesn't unwrap (wrong key, or the file was moved)") from exc
        aead = AESGCM(dek)
        counter = 0
        block = _read_upto(read, SEGMENT + TAG)
        while True:
            following = _read_upto(read, SEGMENT + TAG) if len(block) == SEGMENT + TAG else b""
            last = not following
            try:
                yield aead.decrypt(_nonce(prefix, counter, last), block, aad)
            except InvalidTag as exc:
                raise CryptoError("a sealed file was modified or cut short") from exc
            if last:
                return
            counter += 1
            block = following

    def seal_bytes(self, data: bytes, path: str) -> bytes:
        return b"".join(self.seal([data], path))

    def open_bytes(self, data: bytes, path: str) -> bytes:
        return b"".join(self.open(io.BytesIO(data).read, path))

    def selftest(self) -> None:
        """Every configured key encrypts and decrypts a field and a file."""
        for key in self.keys:
            ring = Keyring.__new__(Keyring)
            ring.keys, ring.active, ring._by_kid = [key], key, {key.kid: key}
            if ring.decrypt_field(ring.encrypt_field("selftest", "selftest"), "selftest") != "selftest":
                raise CryptoError(f"key {key.kid} failed its self-test")
            if self.open_bytes(ring.seal_bytes(b"selftest" * 9000, "selftest"), "selftest") != b"selftest" * 9000:
                raise CryptoError(f"key {key.kid} failed its self-test")

    def file_kid(self, header: bytes) -> str | None:
        if not header.startswith(MAGIC) or len(header) < len(MAGIC) + 1:
            return None
        n = header[len(MAGIC)]
        return header[len(MAGIC) + 1:len(MAGIC) + 1 + n].decode(errors="replace")


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


def _read_upto(read: Callable[[int], bytes], n: int) -> bytes:
    out = b""
    while len(out) < n:
        piece = read(n - len(out))
        if not piece:
            break
        out += piece
    return out


def _read_exact(read: Callable[[int], bytes], n: int) -> bytes:
    out = _read_upto(read, n)
    if len(out) != n:
        raise CryptoError("a sealed file was cut short")
    return out


class StreamReader(io.RawIOBase):
    """A read-only file object over an iterator of byte strings."""

    def __init__(self, pieces: Iterator[bytes], on_close: Callable[[], None] | None = None):
        self._pieces = pieces
        self._buf = b""
        self._on_close = on_close

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                self._buf = next(self._pieces)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n

    def close(self) -> None:
        if not self.closed and self._on_close:
            self._on_close()
        super().close()


# The app's keyring and strict flag, set by create_app. Strict mode (after every existing value has
# been encrypted) refuses unencrypted values instead of passing them through as legacy plaintext.
_KEYRING: Keyring | None = None
_STRICT = False


def configure(spec: str | None, strict: bool = False) -> Keyring | None:
    global _KEYRING, _STRICT
    _KEYRING = Keyring(spec) if spec else None
    _STRICT = bool(strict)
    return _KEYRING


def keyring() -> Keyring | None:
    return _KEYRING


def require_keyring() -> Keyring:
    if _KEYRING is None:
        raise CryptoError("encryption keys aren't configured (ENCRYPTION_KEYS)")
    return _KEYRING


def strict() -> bool:
    return _STRICT


def stream_file(fh: IO[bytes]) -> Iterator[bytes]:
    while chunk := fh.read(1024 * 1024):
        yield chunk
