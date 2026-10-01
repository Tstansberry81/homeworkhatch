"""Column types that encrypt on the way into the database and decrypt on the way out.

Each column names its own associated-data label (keep it if the column is ever renamed: values
are bound to it). NULL stays NULL, so `IS NULL` filters keep working; nothing else about an
encrypted value can be queried in SQL. Values written before encryption was switched on are read
as plain text until `flask encryption migrate` (or the background migration) encrypts them, and
refused once ENCRYPTION_STRICT is on.
"""

from __future__ import annotations

import json
import math

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from .services import crypto

LABELS: set[str] = set()


class _Encrypted(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, aad: str):
        super().__init__()
        self.aad = aad
        LABELS.add(aad)

    def _dump(self, value) -> str:  # Python value -> text
        return value

    def _load(self, text: str):  # text -> Python value
        return text

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return crypto.require_keyring().encrypt_field(self._dump(value), self.aad)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if crypto.Keyring.is_field_ciphertext(value):
            return self._load(crypto.require_keyring().decrypt_field(value, self.aad))
        if crypto.strict():
            raise crypto.CryptoError(f"an unencrypted value in {self.aad} (ENCRYPTION_STRICT is on)")
        return self._load(value) if isinstance(value, str) else value

    def legacy(self, text: str):
        """A not-yet-encrypted stored value as the Python value it stands for."""
        return self._load(text)

    def plain_text(self, value) -> str:
        """The unencrypted text form of a Python value (for decrypt-all)."""
        return self._dump(value)


class EncryptedText(_Encrypted):
    cache_ok = True


class EncryptedJSON(_Encrypted):
    cache_ok = True

    def _dump(self, value) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    def _load(self, text: str):
        return json.loads(text)


class EncryptedFloat(_Encrypted):
    cache_ok = True

    def _dump(self, value) -> str:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("can't store NaN or infinity")
        return repr(value)

    def _load(self, text: str):
        return float(text)
