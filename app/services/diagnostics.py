"""LMS diagnostics: shape-only reports the extension sends from an LMS it can't sync yet.

The extension's Brightspace check (extension/d2l.js) records which API routes answered, their
status, timing, item counts, JSON field names with value TYPES, and a few whitelisted enum values.
It never records names, grades, titles or text. This module re-checks every report before it is
stored, so even a modified client can't get anything else into the database: unknown keys are
dropped, shape trees may only contain the shape vocabulary, and every string must match a strict
pattern or a fixed list.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select

from ..extensions import db
from ..models import LmsDiagnostic, User, utcnow

MAX_BYTES = 256 * 1024
DAILY_LIMIT = 10

LMS_NAMES = {"brightspace"}
SCHEMA = 1

# Mirrors extension/d2l.js.
SHAPE_TOKENS = {"str", "date", "url", "html", "num", "bool", "null", "obj", "arr"}
KEY_RE = re.compile(r"^[A-Za-z0-9_.@:{}\[\] -]{1,80}$")
ENUM_FIELDS = {
    "ActivityType", "AssociatedEntityType", "CalcTypeId", "CompletionType", "DropboxType", "EndDateAvailabilityType",
    "EntityType", "EventType", "GradeObjectType", "GradeObjectTypeName", "GradeType", "GradingSystem", "ItemType",
    "LateSubmissionOption", "ObjectType", "PagingTypeId", "StartDateAvailabilityType", "Status", "SubmissionRule",
    "SubmissionType", "TopicType", "Type", "TypeIdentifier", "WeightDistributionType",
}
ENUM_VALUE_RE = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}$")
D2L_TYPE_RE = re.compile(r"^D2L(?:\.[A-Za-z]{1,40}){1,6}$")
MAX_ENUM_VALUES = 20
NOTES = {
    "versions_unavailable", "signed_out", "whoami_unexpected", "worker_signed_out", "no_courses", "paging_capped",
    "paging_error", "rate_limited", "request_cap", "keys_capped",
}
ERRORS = {"timeout", "network", "not_json"}
TRANSPORTS = {"worker", "tab"}
ROUTES = {
    "versions": "/d2l/api/versions/",
    "whoami": "/d2l/api/lp/{lp}/users/whoami",
    "enrollments": "/d2l/api/lp/{lp}/enrollments/myenrollments/?orgUnitTypeId=3",
    "calendar_events": "/d2l/api/le/{le}/calendar/events/myEvents/?orgUnitIdsCSV={ou}&startDateTime={date}&endDateTime={date}",
    "my_items_due": "/d2l/api/le/{le}/content/myItems/due/?orgUnitIdsCSV={ou}",
    "dropbox_folders": "/d2l/api/le/{le}/{ou}/dropbox/folders/",
    "dropbox_mysubmissions": "/d2l/api/le/{le}/{ou}/dropbox/folders/{id}/submissions/mysubmissions/",
    "quizzes": "/d2l/api/le/{le}/{ou}/quizzes/",
    "grade_values": "/d2l/api/le/{le}/{ou}/grades/values/myGradeValues/",
    "grade_objects": "/d2l/api/le/{le}/{ou}/grades/",
    "grade_categories": "/d2l/api/le/{le}/{ou}/grades/categories/",
    "grade_setup": "/d2l/api/le/{le}/{ou}/grades/setup/",
    "news": "/d2l/api/le/{le}/{ou}/news/",
    "content_toc": "/d2l/api/le/{le}/{ou}/content/toc",
}
# A recorded path is its route's template with only the API version filled in.
ROUTE_RES = {
    name: re.compile("^" + re.escape(t).replace(r"\{lp\}", r"\d{1,2}\.\d{1,3}").replace(r"\{le\}", r"\d{1,2}\.\d{1,3}") + "$")
    for name, t in ROUTES.items()
}
VERSION_RE = re.compile(r"^\d{1,2}\.\d{1,3}$")
BUILD_RE = re.compile(r"^\d{1,3}(?:\.\d{1,5}){1,3}$")
EXT_VERSION_RE = re.compile(r"^\d{1,3}(?:\.\d{1,5}){0,3}$")
RAN_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(?=.{{1,253}}$){HOST_LABEL}(?:\.{HOST_LABEL})*$")
MAX_ENDPOINTS = 80
MAX_SHAPE_DEPTH = 8
MAX_SHAPE_NODES = 4000


class DiagnosticError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _int(value, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None
    return value


def _shape_token(value) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    parts = value.split("|")
    return value if all(p in SHAPE_TOKENS for p in parts) and len(set(parts)) == len(parts) else None


def clean_shape(value, depth: int = 0, budget: list | None = None):
    """A shape tree with everything outside the vocabulary removed; None if nothing valid is left."""
    budget = budget if budget is not None else [MAX_SHAPE_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_SHAPE_DEPTH:
        return None
    if isinstance(value, str):
        return _shape_token(value)
    if not isinstance(value, dict):
        return None
    length = value.get("len")
    if _int(length, 0, 10_000_000) is not None:  # an array: {"[]": element shape, "len": n}
        out: dict = {"len": length}
        if "[]" in value:
            items = clean_shape(value["[]"], depth + 1, budget)
            if items is not None:
                out["[]"] = items
        alt = _shape_token(value.get("{or}"))
        if alt:
            out["{or}"] = alt
        return out
    out = {}
    for key, sub in list(value.items())[:200]:
        if not isinstance(key, str) or not KEY_RE.match(key):
            continue
        if key == "{or}":
            alt = _shape_token(sub)
            if alt:
                out[key] = alt
            continue
        cleaned = clean_shape(sub, depth + 1, budget)
        if cleaned is not None:
            out[key] = cleaned
    return out


def _enum_value(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and abs(value) <= 10_000:
        return value
    if isinstance(value, str) and (ENUM_VALUE_RE.match(value) or D2L_TYPE_RE.match(value)):
        return value
    return None


def clean_enums(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for field, values in value.items():
        if field not in ENUM_FIELDS or not isinstance(values, list):
            continue
        kept = []
        for v in values:
            v = _enum_value(v)
            if v is not None and v not in kept and len(kept) < MAX_ENUM_VALUES:
                kept.append(v)
        if kept:
            out[field] = kept
    return out


def clean_endpoint(value) -> dict | None:
    if not isinstance(value, dict) or value.get("name") not in ROUTES:
        return None
    name = value["name"]
    status = _int(value.get("status"), 0, 599)
    if status is None:
        return None
    path = value.get("path")
    out = {"name": name, "path": path if isinstance(path, str) and ROUTE_RES[name].match(path) else None, "status": status,
           "ms": _int(value.get("ms"), 0, 600_000), "count": _int(value.get("count"), 0, 10_000_000),
           "shape": clean_shape(value.get("shape"))}
    for key, lo, hi in (("course", 1, 10), ("pages", 1, 100), ("retry_after", 0, 86_400)):
        if key in value:
            out[key] = _int(value.get(key), lo, hi)
    if value.get("error") in ERRORS:
        out["error"] = value["error"]
    enums = clean_enums(value.get("enums"))
    if enums:
        out["enums"] = enums
    return out


def clean_report(body) -> dict:
    """The report with only documented, safe fields. Raises DiagnosticError when it isn't one."""
    if not isinstance(body, dict):
        raise DiagnosticError("report must be a JSON object")
    if body.get("lms") not in LMS_NAMES:
        raise DiagnosticError("unsupported lms")
    if body.get("schema") != SCHEMA or isinstance(body.get("schema"), bool):
        raise DiagnosticError("unsupported schema")
    host = body.get("host")
    if not isinstance(host, str) or not HOST_RE.match(host.lower()):
        raise DiagnosticError("host must be a hostname")
    out: dict = {"lms": body["lms"], "schema": SCHEMA, "host": host.lower()}
    ext = body.get("extension_version")
    out["extension_version"] = ext if isinstance(ext, str) and EXT_VERSION_RE.match(ext) else None
    ran_at = body.get("ran_at")
    out["ran_at"] = ran_at if isinstance(ran_at, str) and RAN_AT_RE.match(ran_at) else None
    out["transport"] = body.get("transport") if body.get("transport") in TRANSPORTS else None
    versions = body.get("versions") if isinstance(body.get("versions"), dict) else {}
    out["versions"] = {k: versions[k] for k in ("lp", "le") if isinstance(versions.get(k), str) and VERSION_RE.match(versions[k])}
    if isinstance(versions.get("product_build"), str) and BUILD_RE.match(versions["product_build"]):
        out["versions"]["product_build"] = versions["product_build"]
    out["signed_in"] = body.get("signed_in") is True
    endpoints = body.get("endpoints") if isinstance(body.get("endpoints"), list) else []
    out["endpoints"] = [e for e in (clean_endpoint(x) for x in endpoints[:MAX_ENDPOINTS]) if e]
    out["enums"] = clean_enums(body.get("enums"))
    notes = body.get("notes") if isinstance(body.get("notes"), list) else []
    out["notes"] = [n for i, n in enumerate(notes[:20]) if isinstance(n, str) and n in NOTES and n not in notes[:i]]
    return out


def sent_today(user: User) -> int:
    since = utcnow() - timedelta(days=1)
    return db.session.scalar(select(func.count(LmsDiagnostic.id))
                             .where(LmsDiagnostic.user_id == user.id, LmsDiagnostic.created_at >= since)) or 0


def store(user: User, body) -> LmsDiagnostic:
    report = clean_report(body)
    if sent_today(user) >= DAILY_LIMIT:
        raise DiagnosticError(f"at most {DAILY_LIMIT} reports a day", status=429)
    row = LmsDiagnostic(user_id=user.id, lms=report["lms"], host=report["host"],
                        extension_version=report["extension_version"], payload=report)
    db.session.add(row)
    db.session.commit()
    return row


def answered(payload: dict) -> tuple[int, int]:
    endpoints = (payload or {}).get("endpoints") or []
    ok = sum(1 for e in endpoints if isinstance(e, dict) and 200 <= (e.get("status") or 0) < 300)
    return ok, len(endpoints)
