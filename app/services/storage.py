"""Blob storage for synced Canvas files and raw snapshots.

Backends behind one small interface:
- "local": files on disk (development only; Render's disk is wiped on every deploy).
- "supabase": Supabase Storage through its S3-compatible API.
- "s3": any S3-compatible service (AWS S3, Cloudflare R2, MinIO).

Object storage serves downloads via short-lived presigned URLs, so large files never
stream through the web worker. Keys look like
"u/<user id>/files/<account id>/<canvas file id>/<version>/<filename>".
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import IO, Protocol

from flask import current_app


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
        self.transfer = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024,
                                       max_concurrency=4)

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
    app.extensions["hh_storage"] = storage
    return storage
