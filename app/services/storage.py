"""Blob storage for synced Canvas files and raw snapshots.

Backends behind one small interface:
- "local": files on disk (development only; Render's disk is wiped on every deploy).
- "supabase": Supabase Storage through its S3-compatible API.
- "s3": any S3-compatible service (AWS S3, Cloudflare R2, MinIO).

With ENCRYPTION_KEYS set (always, outside bare unit setups), the backend is wrapped in
EncryptedStorage: every object is sealed before it reaches the provider and opened on the way
back (see crypto.py), so downloads stream through the app and uploads come through it too; no
presigned URLs hand out or accept plaintext. Keys look like
"u/<user id>/files/<account id>/<canvas file id>/<version>/<filename>".
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import IO, Iterator, Protocol

from flask import current_app

from . import crypto


class TooLarge(Exception):
    pass


class StorageError(Exception):
    pass


class Storage(Protocol):
    def put_file(self, key: str, fileobj: IO[bytes], content_type: str | None = None) -> None: ...
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def open(self, key: str) -> IO[bytes]: ...
    def read(self, key: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def signed_url(self, key: str, filename: str, content_type: str | None, inline: bool, ttl: int) -> str | None: ...
    def presign_put(self, key: str, content_type: str, ttl: int) -> str | None: ...
    def size(self, key: str) -> int | None: ...


_UNSAFE = re.compile(r"[^A-Za-z0-9._() -]+")


def safe_key_part(name: str, fallback: str = "file") -> str:
    """Object-key-safe filename (Supabase allows a restricted character set in keys)."""
    cleaned = _UNSAFE.sub("_", name or "").strip(" .")
    return (cleaned or fallback)[:150]


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("storage key escapes root")
        return path

    def put_file(self, key, fileobj, content_type=None):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(fileobj, out, 1024 * 256)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def put_bytes(self, key, data, content_type=None):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def open(self, key):
        return open(self._path(key), "rb")

    def read(self, key):
        return self._path(key).read_bytes()

    def delete_prefix(self, prefix):
        path = self._path(prefix.rstrip("/"))
        if path == self.root:
            raise ValueError("refusing to delete the storage root")
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()

    def signed_url(self, key, filename, content_type, inline, ttl):
        return None  # served by the app with send_file

    def presign_put(self, key, content_type, ttl):
        return None  # no direct uploads; the extension sends files through the app

    def size(self, key):
        path = self._path(key)
        return path.stat().st_size if path.is_file() else None

    def head_bytes(self, key, n):
        with open(self._path(key), "rb") as fh:
            return fh.read(n)

    def list_keys(self, prefix=""):
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key


def _xml_content_type(request, **kwargs):
    request.headers["Content-Type"] = "application/xml"


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None, region: str | None, access_key: str | None,
                 secret_key: str | None, path_style: bool):
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        if not bucket:
            raise StorageError("No storage bucket configured (S3_BUCKET / SUPABASE_BUCKET).")
        self.bucket = bucket
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "auto"},
            retries={"max_attempts": 5, "mode": "standard"},
            # boto3 >= 1.36 adds CRC checksums by default, which many S3-compatible
            # services (including Supabase) don't need; only send them when required.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url, region_name=region, aws_access_key_id=access_key,
                               aws_secret_access_key=secret_key, config=cfg)
        # boto3 sends DeleteObjects without a Content-Type; Supabase then ignores the XML body
        # and rejects the request ("must have required property 'Body'").
        self.s3.meta.events.register("before-sign.s3.DeleteObjects", _xml_content_type)
        # One streamed PutObject per file (objects are capped at 50 MB anyway): multipart uploads
        # buffer every part in memory, which a 512 MB instance can't afford with uploads in parallel.
        self.transfer = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024,
                                       max_concurrency=1)

    @staticmethod
    def _too_large(exc) -> bool:
        err = getattr(exc, "response", {}).get("Error", {})
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        return err.get("Code") in {"EntityTooLarge", "PayloadTooLarge"} or status == 413

    def put_file(self, key, fileobj, content_type=None):
        from boto3.exceptions import S3UploadFailedError
        from botocore.exceptions import ClientError

        extra = {"ContentType": content_type} if content_type else {}
        try:
            self.s3.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra, Config=self.transfer)
        except ClientError as exc:
            if self._too_large(exc):
                raise TooLarge("the storage provider rejected the file as too large") from exc
            raise StorageError(str(exc)) from exc
        except S3UploadFailedError as exc:
            if "EntityTooLarge" in str(exc) or "413" in str(exc):
                raise TooLarge("the storage provider rejected the file as too large") from exc
            raise StorageError(str(exc)) from exc

    def put_bytes(self, key, data, content_type=None):
        extra = {"ContentType": content_type} if content_type else {}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def open(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]

    def read(self, key):
        return self.open(key).read()

    def delete_prefix(self, prefix):
        if not prefix.strip("/"):
            raise ValueError("refusing to delete the whole bucket")
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})

    def signed_url(self, key, filename, content_type, inline, ttl):
        disposition = "inline" if inline else "attachment"
        ascii_name = filename.encode("ascii", "ignore").decode() or "file"
        from urllib.parse import quote

        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ResponseContentDisposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        }
        if content_type:
            params["ResponseContentType"] = content_type
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)

    def presign_put(self, key, content_type, ttl):
        """A URL the browser extension can PUT the file to directly. The signature covers the
        Content-Type, so the upload must send exactly that header."""
        return self.s3.generate_presigned_url("put_object", ExpiresIn=ttl,
                                              Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type})

    def head_bytes(self, key, n):
        from botocore.exceptions import ClientError

        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{n - 1}")["Body"].read()
        except ClientError as exc:
            err = exc.response.get("Error", {}).get("Code")
            if err == "InvalidRange" or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 416:
                return b""  # an empty object: nothing to read, and certainly not sealed
            raise StorageError(str(exc)) from exc

    def list_keys(self, prefix=""):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                yield o["Key"]

    def size(self, key):
        from botocore.exceptions import ClientError

        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise StorageError(str(exc)) from exc


SPOOL = 2 * 1024 * 1024  # bigger temporary copies go to disk, not memory (512 MB instances)


def _prepend(head: bytes, raw: IO[bytes]):
    """A read(n) that returns `head` first, then the rest of `raw`."""
    buf = bytearray(head)

    def read(n: int) -> bytes:
        if buf:
            out = bytes(buf[:n])
            del buf[:n]
            return out
        return raw.read(n)
    return read


def _pieces(read, size: int = 1024 * 1024) -> Iterator[bytes]:
    while piece := read(size):
        yield piece


class EncryptedStorage:
    """Seals every object on the way in and opens it on the way out. Objects written before
    encryption was switched on are read as they are until migrated (and refused in strict mode)."""

    def __init__(self, backend):
        self.backend = backend

    @staticmethod
    def _ring() -> crypto.Keyring:
        return crypto.require_keyring()

    def _sealed_spool(self, chunks, key: str):
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL)
        for piece in self._ring().seal(chunks, key):
            spool.write(piece)
        spool.seek(0)
        return spool

    def put_file(self, key, fileobj, content_type=None):
        with self._sealed_spool(crypto.stream_file(fileobj), key) as spool:
            self.backend.put_file(key, spool, "application/octet-stream")

    def put_bytes(self, key, data, content_type=None):
        self.backend.put_bytes(key, self._ring().seal_bytes(data, key), "application/octet-stream")

    def open(self, key):
        raw = self.backend.open(key)
        head = crypto._read_upto(raw.read, len(crypto.MAGIC))
        if head == crypto.MAGIC:
            plain = self._ring().open(_prepend(head, raw), key)
        elif crypto.strict():
            raw.close()
            raise crypto.CryptoError(f"{key} isn't encrypted (ENCRYPTION_STRICT is on)")
        else:
            plain = _pieces(_prepend(head, raw))
        return io.BufferedReader(crypto.StreamReader(plain, on_close=raw.close), buffer_size=256 * 1024)

    def read(self, key):
        with closing(self.open(key)) as fh:
            return fh.read()

    def delete_prefix(self, prefix):
        self.backend.delete_prefix(prefix)

    def signed_url(self, key, filename, content_type, inline, ttl):
        return None  # the provider only holds ciphertext: the app decrypts and streams

    def presign_put(self, key, content_type, ttl):
        return None  # uploads come through the app, which seals them before storage

    def size(self, key):
        return self.backend.size(key)

    def list_keys(self, prefix=""):
        return self.backend.list_keys(prefix)

    def sealed_kid(self, key) -> str | None:
        """The key id an object is sealed with, or None if it's stored unencrypted."""
        return self._ring().file_kid(self.backend.head_bytes(key, len(crypto.MAGIC) + 17))

    def rewrite(self, key: str, seal: bool) -> bool:
        """Re-store one object sealed with the active key (seal=True) or unencrypted (seal=False),
        if it isn't already. The new bytes are checked before they replace the old ones, and read
        back afterwards; on any mismatch the original is put back. Returns whether it changed."""
        ring = self._ring()
        kid = self.sealed_kid(key)
        if (seal and kid == ring.active.kid) or (not seal and kid is None):
            return False
        before = hashlib.sha256()
        original = tempfile.SpooledTemporaryFile(max_size=SPOOL)
        raw = self.backend.open(key)
        try:
            for piece in _pieces(raw.read):
                original.write(piece)
        finally:
            raw.close()
        original.seek(0)
        plain = ring.open(original.read, key) if kid else _pieces(original.read)

        def tee():
            for piece in plain:
                before.update(piece)
                yield piece
        new = self._sealed_spool(tee(), key) if seal else _spool(tee())
        try:
            check = hashlib.sha256()
            for piece in (ring.open(new.read, key) if seal else _pieces(new.read)):
                check.update(piece)
            if check.hexdigest() != before.hexdigest():
                raise crypto.CryptoError(f"re-encrypting {key} didn't round-trip; left it unchanged")
            new.seek(0)
            self.backend.put_file(key, new, "application/octet-stream")
            try:
                after = hashlib.sha256()
                with closing(self.open(key)) as fh:
                    for piece in _pieces(fh.read):
                        after.update(piece)
                if after.hexdigest() != before.hexdigest():
                    raise crypto.CryptoError(f"{key} didn't read back correctly")
            except Exception:
                self._put_back(key, original)
                raise
            return True
        finally:
            new.close()
            original.close()

    def _put_back(self, key: str, original) -> None:
        """Restore an object's original bytes after a failed rewrite; if that fails too, keep them
        under a quarantine key rather than lose them."""
        try:
            original.seek(0)
            self.backend.put_file(key, original, "application/octet-stream")
        except Exception:
            original.seek(0)
            self.backend.put_file(f"_quarantine/{key}", original, "application/octet-stream")
            current_app.logger.error("encryption: couldn't restore %s; original kept at _quarantine/%s", key, key)
            raise


def _spool(chunks) -> IO[bytes]:
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL)
    for piece in chunks:
        spool.write(piece)
    spool.seek(0)
    return spool


def get_storage() -> Storage:
    app = current_app
    cached = app.extensions.get("hh_storage")
    if cached is not None:
        return cached
    cfg = app.config
    backend = cfg["STORAGE_BACKEND"]
    if backend in {"s3", "supabase"}:
        storage = S3Storage(cfg["S3_BUCKET"], cfg["S3_ENDPOINT_URL"], cfg["S3_REGION"], cfg["S3_ACCESS_KEY_ID"],
                            cfg["S3_SECRET_ACCESS_KEY"],
                            # Supabase requires path-style addressing; so do most custom endpoints.
                            path_style=backend == "supabase" or bool(cfg["S3_ENDPOINT_URL"]))
    else:
        storage = LocalStorage(cfg["STORAGE_DIR"])
    if crypto.keyring() is not None:
        storage = EncryptedStorage(storage)
    app.extensions["hh_storage"] = storage
    return storage
