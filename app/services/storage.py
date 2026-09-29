"""Blob storage for synced Canvas files and raw snapshots.

Two backends behind one small interface: local disk (default, fine for development or
a server with a persistent disk) and S3-compatible object storage (AWS S3, Cloudflare R2,
MinIO) for production. Keys look like "u/<user id>/files/<canvas file id>/<version>".
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import IO, Iterator, Protocol

from flask import current_app


class TooLarge(Exception):
    pass


class Storage(Protocol):
    def put_stream(self, key: str, stream: IO[bytes], limit: int) -> tuple[int, str]: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def open(self, key: str) -> IO[bytes]: ...
    def read(self, key: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> None: ...


def _chunks(stream: IO[bytes], limit: int) -> Iterator[bytes]:
    size = 0
    while True:
        chunk = stream.read(1024 * 256)
        if not chunk:
            return
        size += len(chunk)
        if size > limit:
            raise TooLarge(f"file exceeds {limit // (1024 * 1024)} MB")
        yield chunk


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("storage key escapes root")
        return path

    def put_stream(self, key, stream, limit):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as out:
                for chunk in _chunks(stream, limit):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return size, digest.hexdigest()

    def put_bytes(self, key, data):
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
        import shutil

        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None, region: str | None):
        import boto3  # optional dependency, only needed for this backend

        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def put_stream(self, key, stream, limit):
        import tempfile

        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile() as spool:
            for chunk in _chunks(stream, limit):
                digest.update(chunk)
                size += len(chunk)
                spool.write(chunk)
            spool.seek(0)
            self.s3.upload_fileobj(spool, self.bucket, key)
        return size, digest.hexdigest()

    def put_bytes(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def open(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]

    def read(self, key):
        return self.open(key).read()

    def delete_prefix(self, prefix):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})


def get_storage() -> Storage:
    app = current_app
    cached = app.extensions.get("hh_storage")
    if cached is not None:
        return cached
    cfg = app.config
    if cfg["STORAGE_BACKEND"] == "s3":
        storage = S3Storage(cfg["S3_BUCKET"], cfg["S3_ENDPOINT_URL"], cfg["S3_REGION"])
    else:
        storage = LocalStorage(cfg["STORAGE_DIR"])
    app.extensions["hh_storage"] = storage
    return storage
