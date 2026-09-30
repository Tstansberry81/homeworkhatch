"""Picking files from the student's Google Drive (through Composio) to use as study sources."""

from __future__ import annotations

import requests
from sqlalchemy import select

from ..extensions import db
from ..models import Upload, User
from . import integrations, uploads

FOLDER = "application/vnd.google-apps.folder"
# Google Docs/Slides/Sheets have no file to download; they're exported. Text keeps them small.
EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def search(user: User, query: str = "", page_token: str | None = None) -> dict:
    """Recent files, or files whose name contains `query`. Folders and trash are left out."""
    q = f"trashed = false and mimeType != '{FOLDER}'"
    if query.strip():
        q += f" and name contains '{_quote(query.strip()[:100])}'"
    args = {"q": q, "pageSize": 30, "orderBy": "modifiedTime desc",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,iconLink,webViewLink)"}
    if page_token:
        args["pageToken"] = page_token
    data = integrations.execute(user, "drive", "GOOGLEDRIVE_FIND_FILE", args)
    body = data.get("response_data") if isinstance(data.get("response_data"), dict) else data
    files = []
    for f in body.get("files") or []:
        mime = f.get("mimeType") or ""
        if mime.startswith("application/vnd.google-apps.") and mime not in EXPORTS:
            continue  # forms, drawings, shortcuts... nothing to study from
        files.append({"id": f.get("id"), "name": f.get("name"), "mimeType": mime,
                      "size": int(f["size"]) if str(f.get("size") or "").isdigit() else None,
                      "modified": f.get("modifiedTime"), "icon": f.get("iconLink"), "url": f.get("webViewLink")})
    return {"files": files, "next": body.get("nextPageToken")}


def import_file(user: User, file_id: str, mime_type: str, course_id: int | None) -> Upload:
    """Copy a Drive file into the student's files (once: picking it again reuses the copy)."""
    if not file_id or not file_id.replace("-", "").replace("_", "").isalnum():
        raise integrations.IntegrationError("That isn't a Drive file.")
    existing = db.session.scalar(select(Upload).where(Upload.user_id == user.id, Upload.source == "drive",
                                                      Upload.external_id == file_id))
    if existing is not None:
        if course_id and existing.course_id != course_id:
            existing.course_id = course_id
            db.session.commit()
        return existing
    export = EXPORTS.get(mime_type or "")
    args = {"fileId": file_id}
    if export:
        args["mime_type"] = export[0]
    data = integrations.execute(user, "drive", "GOOGLEDRIVE_DOWNLOAD_FILE", args)
    content = data.get("downloaded_file_content") or {}
    url = content.get("s3url") if isinstance(content, dict) else None
    if not url:
        raise integrations.IntegrationError("Google Drive didn't return the file's contents.")
    name = data.get("name") or content.get("name") or "Drive file"
    if export and not name.lower().endswith(export[1]):
        name += export[1]
    try:
        response = requests.get(url, stream=True, timeout=(10, 120))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise integrations.IntegrationError(f"Couldn't download the file from Drive: {exc}") from exc
    response.raw.decode_content = True
    try:
        return uploads.store(user, response.raw, name, export[0] if export else content.get("mimetype") or mime_type,
                             course_id, source="drive", external_id=file_id)
    finally:
        response.close()

