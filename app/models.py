"""Database models.

Canvas data is stored per student: every Course/Assignment/File row belongs to one
user, keyed by the Canvas instance (host) and Canvas IDs. Nothing here assumes a
particular school. Rows are upserted by those natural keys on every sync, so IDs stay
stable and anything that points at them (flashcard sources, coin awards) keeps working.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timezone

from flask_login import UserMixin
from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .encrypted_types import EncryptedFloat, EncryptedJSON, EncryptedText
from .extensions import db


def utcnow() -> datetime:
    """Naive UTC timestamp; all datetimes in the database are naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- accounts


class User(UserMixin, db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_terms_at: Mapped[datetime | None] = mapped_column(DateTime)

    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    grade_level: Mapped[str | None] = mapped_column(String(40))
    birth_year: Mapped[int | None] = mapped_column(Integer)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)

    plan: Mapped[str] = mapped_column(String(20), default="free")
    plan_status: Mapped[str | None] = mapped_column(String(40))
    plan_comped: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # one-time passes; subscriptions leave it empty
    plan_payment_id: Mapped[str | None] = mapped_column(String(120))  # last pass checkout applied (webhook retries)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(120), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(120))

    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD in the user's timezone
    show_on_leaderboards: Mapped[bool] = mapped_column(Boolean, default=False)  # opt-in
    # The student's standing choice for course files: True = keep files for every class, including
    # new ones; False = only classes they tick; None = not chosen yet (no files are copied).
    keep_all_files: Mapped[bool | None] = mapped_column(Boolean)
    calendar_token: Mapped[str] = mapped_column(EncryptedText("user.calendar_token"), unique=True,
                                                default=lambda: secrets.token_urlsafe(24))
    # SHA-256 of the token (like API tokens): the feed is looked up by this, never by the token.
    calendar_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    # Bumped on password change/reset; part of the login cookie, so old sessions stop working.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # IDs of extension copies loaded from the zip (each install gets its own), so the Connect Canvas
    # page finds them on every address the site has. The Web Store copy's ID is in config.
    extension_ids: Mapped[list | None] = mapped_column(JSON)
    # Exam planner: the most study time to schedule on one day, across every plan.
    study_minutes_per_day: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
    # Shared sets taken down after a report was upheld. At sharing.STRIKES_TO_BLOCK the account can't
    # share any more (sharing_blocked), and everything it shared goes private.
    share_strikes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sharing_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)
        self.session_version = (self.session_version or 0) + 1

    def get_id(self) -> str:  # Flask-Login: "<id>:<session version>"
        return f"{self.id}:{self.session_version or 0}"

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self) -> bool:  # Flask-Login
        return bool(self.active)

    @property
    def age(self) -> int | None:
        return utcnow().year - self.birth_year if self.birth_year else None

    @property
    def is_adult(self) -> bool:
        # Only the birth year is known, so be conservative: someone born in 2008 may still be
        # 17 during 2026. Adult features unlock the year the student is certainly 18.
        return self.birth_year is not None and utcnow().year - self.birth_year >= 19


def calendar_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@event.listens_for(User, "before_insert")
@event.listens_for(User, "before_update")
def _hash_calendar_token(mapper, connection, user: User) -> None:
    if user.calendar_token is None:
        user.calendar_token = secrets.token_urlsafe(24)
    user.calendar_token_hash = calendar_token_hash(user.calendar_token)


class AppState(db.Model):
    """Small named values the app keeps for itself (e.g. the last encryption sweep's result)."""

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EncryptionKey(db.Model):
    """Every encryption key the data has been written with, by id and check value (never the key).
    At startup the configured keys must match these, so a mistyped, swapped or dropped key stops the
    app instead of silently making data unreadable."""

    kid: Mapped[str] = mapped_column(String(16), primary_key=True)
    check: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(db.Model):
    """Token the browser extension uses to upload Canvas data. Only a hash is stored."""

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class ActivityLog(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str | None] = mapped_column(EncryptedText("activity_log.detail"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- Canvas data


class CanvasAccount(db.Model):
    """One student at one Canvas instance (a student can connect more than one school)."""

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    host: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(300))
    canvas_user_id: Mapped[str] = mapped_column(String(64))
    canvas_name: Mapped[str | None] = mapped_column(EncryptedText("canvas_account.canvas_name"))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_snapshot_id: Mapped[str | None] = mapped_column(String(80))
    # Fingerprint of the last snapshot (minus its timestamp): an identical hourly sync skips
    # re-reading and re-writing every class (see ingest.snapshot_digest).
    last_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    restricted: Mapped[list | None] = mapped_column(JSON)
    # Where the data comes from: "canvas" (the extension), or "ics" (a calendar link the student
    # pasted, for Brightspace, Blackboard, Moodle, Schoology...). More LMS adapters later.
    lms: Mapped[str] = mapped_column(String(20), default="canvas", server_default="canvas")

    __table_args__ = (UniqueConstraint("user_id", "host", "canvas_user_id"),)


class Course(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("canvas_account.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    # name and course_code are what the app shows everywhere: the student's own name and short code
    # for the class (Customize tab) when set, else the LMS's. Syncs only update canvas_name/canvas_code.
    name: Mapped[str] = mapped_column(String(300))
    course_code: Mapped[str | None] = mapped_column(String(200))
    canvas_name: Mapped[str | None] = mapped_column(String(300))
    canvas_code: Mapped[str | None] = mapped_column(String(200))
    custom_name: Mapped[str | None] = mapped_column(String(300))
    custom_code: Mapped[str | None] = mapped_column(String(200))
    color: Mapped[str | None] = mapped_column(String(7))  # one of utils.CLASS_COLORS, or None for the default
    term_id: Mapped[str | None] = mapped_column(String(64))
    term_name: Mapped[str | None] = mapped_column(String(200))
    # Sections of one class (lecture/discussion shells) share a class_key.
    class_key: Mapped[str] = mapped_column(String(400))
    # Classmates at the same school share a room: "<host>:<canvas course id>".
    room_key: Mapped[str] = mapped_column(String(300), index=True)
    on_dashboard: Mapped[bool | None] = mapped_column(Boolean)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    current_score: Mapped[float | None] = mapped_column(EncryptedFloat("course.current_score"))
    current_grade: Mapped[str | None] = mapped_column(EncryptedText("course.current_grade"))
    final_score: Mapped[float | None] = mapped_column(EncryptedFloat("course.final_score"))
    final_grade: Mapped[str | None] = mapped_column(EncryptedText("course.final_grade"))
    html_url: Mapped[str | None] = mapped_column(String(500))
    syllabus_html: Mapped[str | None] = mapped_column(EncryptedText("course.syllabus_html"), deferred=True)
    files_tab_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Canvas's "weight final grade based on assignment groups" setting (None if unknown).
    group_weighting: Mapped[bool | None] = mapped_column(Boolean)
    chat_joined: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # opt-in class chat
    # Whether the student chose to keep this class's files here: None until they decide, and
    # files are only requested when True (the student directs every copy that's made).
    sync_files: Mapped[bool | None] = mapped_column(Boolean)
    # Hash of the course's text content; the search index is rebuilt only when it changes.
    content_signature: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[CanvasAccount] = relationship()
    assignments: Mapped[list[Assignment]] = relationship(back_populates="course", cascade="all, delete-orphan",
                                                         order_by="Assignment.due_at")
    groups: Mapped[list[AssignmentGroup]] = relationship(cascade="all, delete-orphan", order_by="AssignmentGroup.position")
    modules: Mapped[list[Module]] = relationship(cascade="all, delete-orphan", order_by="Module.position")
    pages: Mapped[list[Page]] = relationship(cascade="all, delete-orphan", order_by="Page.title")
    announcements: Mapped[list[Announcement]] = relationship(cascade="all, delete-orphan",
                                                             order_by="Announcement.posted_at.desc()")
    discussions: Mapped[list[Discussion]] = relationship(cascade="all, delete-orphan")
    files: Mapped[list[CanvasFile]] = relationship(cascade="all, delete-orphan", order_by="CanvasFile.name")

    def apply_custom(self) -> None:
        """Show the student's name and code for the class where they set one, else the LMS's."""
        self.name = (self.custom_name or self.canvas_name or self.name or "Course")[:300]
        self.course_code = self.custom_code or self.canvas_code

    @property
    def code(self) -> str:
        """The section code as students read it ("CS 1110-001", not "CS_1110-001")."""
        return (self.course_code or "").replace("_", " ").strip()

    @property
    def label(self) -> str:
        """The name plus its section code, so a lecture and its discussion section can be told
        apart in menus ("Intro to Moral & Pol Phil · PHIL 1730-102")."""
        code = self.code
        return f"{self.name} · {code}" if code and code.lower() not in self.name.lower() else self.name

    @property
    def listed_files(self) -> list[CanvasFile]:
        """Files to show: a file posted twice under different Canvas IDs (same name, type and
        size) is listed once."""
        seen, out = set(), []
        for f in self.files:
            key = f.stored_fingerprint or f.wanted_fingerprint or f"id:{f.id}"
            if key not in seen:
                seen.add(key)
                out.append(f)
        return out

    __table_args__ = (UniqueConstraint("account_id", "canvas_id"),)

    @property
    def visible(self) -> bool:
        return self.active and not self.hidden


class AssignmentGroup(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    weight: Mapped[float | None] = mapped_column(Float)
    position: Mapped[int | None] = mapped_column(Integer)
    drop_lowest: Mapped[int] = mapped_column(Integer, default=0)
    drop_highest: Mapped[int] = mapped_column(Integer, default=0)
    never_drop: Mapped[list | None] = mapped_column(JSON)  # Canvas assignment ids exempt from drops

    __table_args__ = (UniqueConstraint("course_id", "canvas_id"),)


class Assignment(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    group_canvas_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(500))
    due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    unlock_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_at: Mapped[datetime | None] = mapped_column(DateTime)
    points_possible: Mapped[float | None] = mapped_column(Float)
    grading_type: Mapped[str | None] = mapped_column(String(40))
    submission_types: Mapped[list | None] = mapped_column(JSON)
    is_quiz: Mapped[bool] = mapped_column(Boolean, default=False)
    omit_from_final_grade: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    html_url: Mapped[str | None] = mapped_column(String(500))
    description_html: Mapped[str | None] = mapped_column(EncryptedText("assignment.description_html"), deferred=True, deferred_group="assignment_detail")
    # Status as computed by the sync (graded/submitted/missing/past_due/upcoming/...).
    status: Mapped[str] = mapped_column(String(30), default="upcoming")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    score: Mapped[float | None] = mapped_column(EncryptedFloat("assignment.score"))
    grade: Mapped[str | None] = mapped_column(EncryptedText("assignment.grade"))
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    excused: Mapped[bool] = mapped_column(Boolean, default=False)
    workflow_state: Mapped[str | None] = mapped_column(String(40))
    rubric: Mapped[list | None] = mapped_column(EncryptedJSON("assignment.rubric"), deferred=True, deferred_group="assignment_detail")
    comments: Mapped[list | None] = mapped_column(EncryptedJSON("assignment.comments"), deferred=True, deferred_group="assignment_detail")
    attachments: Mapped[list | None] = mapped_column(EncryptedJSON("assignment.attachments"), deferred=True, deferred_group="assignment_detail")
    rubric_assessment: Mapped[dict | None] = mapped_column(EncryptedJSON("assignment.rubric_assessment"), deferred=True, deferred_group="assignment_detail")
    # Student's own override ("I handed this in on paper"); never touched by sync.
    user_done: Mapped[bool] = mapped_column(Boolean, default=False)

    course: Mapped[Course] = relationship(back_populates="assignments")

    __table_args__ = (UniqueConstraint("course_id", "canvas_id"),)

    @property
    def effective_status(self) -> str:
        if self.user_done and self.status in {"upcoming", "past_due", "no_submission", "missing"}:
            return "done"
        return self.status

    @property
    def percent(self) -> float | None:
        if self.score is None or not self.points_possible:
            return None
        return round(100 * self.score / self.points_possible, 1)


class Module(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    position: Mapped[int | None] = mapped_column(Integer)
    unlock_at: Mapped[datetime | None] = mapped_column(DateTime)
    state: Mapped[str | None] = mapped_column(String(40))
    items: Mapped[list | None] = mapped_column(JSON)

    __table_args__ = (UniqueConstraint("course_id", "canvas_id"),)


class Page(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    body_html: Mapped[str | None] = mapped_column(EncryptedText("page.body_html"))
    html_url: Mapped[str | None] = mapped_column(String(500))
    canvas_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("course_id", "slug"),)


class Announcement(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    message_html: Mapped[str | None] = mapped_column(EncryptedText("announcement.message_html"))
    author: Mapped[str | None] = mapped_column(String(200))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    html_url: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (UniqueConstraint("course_id", "canvas_id"),)


class Discussion(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    message_html: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    html_url: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (UniqueConstraint("course_id", "canvas_id"),)


class CanvasFile(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("canvas_account.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    canvas_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(500))
    path: Mapped[str | None] = mapped_column(String(800))
    content_type: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int | None] = mapped_column(BigInteger)
    canvas_updated_at: Mapped[str | None] = mapped_column(String(64))
    # Version the extension announced vs. the version we actually hold.
    wanted_version: Mapped[str | None] = mapped_column(String(32))
    stored_version: Mapped[str | None] = mapped_column(String(32))
    # Name + type + size, the file's identity before downloading it (ingest.content_fingerprint).
    # A file whose fingerprint we already hold is never downloaded again, whatever its Canvas ID.
    wanted_fingerprint: Mapped[str | None] = mapped_column(String(40))
    stored_fingerprint: Mapped[str | None] = mapped_column(String(40))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    stored_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Deferred: extracted text can be hundreds of KB and most queries never need it.
    text: Mapped[str | None] = mapped_column(EncryptedText("canvas_file.text"), deferred=True)
    # "pending" until the background reader (services/textjobs.py) gets to it, then "extracting"
    # since text_started_at, then ok / empty / unsupported / too_large / error / ai.
    text_status: Mapped[str | None] = mapped_column(String(40), index=True)
    text_started_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("account_id", "canvas_id"),)

    @property
    def is_stored(self) -> bool:
        return bool(self.storage_key) and self.stored_version == self.wanted_version


class CalendarEvent(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("canvas_account.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"))
    canvas_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    start_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(EncryptedText("calendar_event.location"))
    html_url: Mapped[str | None] = mapped_column(String(500))
    # Canvas's all-day events are a calendar date (start_at is midnight in the course's zone, which
    # may not be the student's). None for rows synced before extension 1.4.3.
    all_day: Mapped[bool | None] = mapped_column(Boolean)
    all_day_date: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (UniqueConstraint("account_id", "canvas_id"),)


class SyncRun(db.Model):
    """One snapshot received from the extension."""

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("canvas_account.id", ondelete="CASCADE"))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    files_needed: Mapped[int] = mapped_column(Integer, default=0)
    files_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[list | None] = mapped_column(JSON)
    stats: Mapped[dict | None] = mapped_column(JSON)


class ContentChunk(db.Model):
    """Searchable text from course material, for the tutor and the generators."""

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(20))  # page/assignment/file/syllabus/announcement
    source_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(500))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(EncryptedText("content_chunk.text"))

    # The sharing check reads a student's material in order (services/sharing.py).
    __table_args__ = (Index("ix_content_chunk_user_source", "user_id", "source_type", "source_id", "ordinal"),)


# ---------------------------------------------------------------- study tools


class Deck(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual / ai / import
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # The exam this deck is for (SET NULL: plans are deleted when the student drops them).
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("study_plan.id", ondelete="SET NULL"), index=True)
    # Opt-in sharing (services/sharing.py): private, link (anyone with the link) or class (also
    # listed for classmates). Only cards that pass the sharing checks are ever shown to others.
    share_mode: Mapped[str] = mapped_column(String(10), default="private", server_default="private")
    share_token: Mapped[str | None] = mapped_column(String(32), unique=True)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime)
    share_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # reported, awaiting review
    taken_down_at: Mapped[datetime | None] = mapped_column(DateTime)  # removed after a report; can't be shared again
    copied_from_id: Mapped[int | None] = mapped_column(ForeignKey("deck.id", ondelete="SET NULL"), index=True)

    course: Mapped[Course | None] = relationship()
    # passive_deletes: the database deletes a deck's cards (ON DELETE CASCADE), without loading them first.
    cards: Mapped[list[Card]] = relationship(back_populates="deck", cascade="all, delete-orphan", order_by="Card.position",
                                             passive_deletes=True)


class Card(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("deck.id", ondelete="CASCADE"), index=True)
    front: Mapped[str] = mapped_column(EncryptedText("card.front"))
    back: Mapped[str] = mapped_column(EncryptedText("card.back"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # SM-2 spaced-repetition state.
    ease: Mapped[float] = mapped_column(Float, default=2.5)
    interval_days: Mapped[int] = mapped_column(Integer, default=0)
    repetitions: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Who wrote it: student (typed or imported), ai (from the student's pasted notes), ai_files (from
    # synced class files or uploads), copy (saved from someone else's shared set), or removed (matches a
    # set taken down after a report). Only student and ai cards are the student's to share until the
    # others are rewritten: origin_text keeps the text the card started as, and rewritten says whether
    # the current text is far enough from it (services/sharing.py), so undoing an edit puts it back on hold.
    origin: Mapped[str] = mapped_column(String(10), default="student", server_default="student")
    origin_text: Mapped[str | None] = mapped_column(EncryptedText("card.origin_text"))
    rewritten: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Why the card isn't shown when its deck is shared: None (shown), ai_unedited, not_yours, verbatim,
    # or unchecked (changed while the deck was private; checked again when it's shared).
    share_block: Mapped[str | None] = mapped_column(String(20), default="unchecked", server_default="unchecked")

    deck: Mapped[Deck] = relationship(back_populates="cards")


class PracticeQuiz(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    # [{"question", "choices": [4], "answer": index, "explanation"}]
    questions: Mapped[list] = mapped_column(EncryptedJSON("practice_quiz.questions"))
    source: Mapped[str] = mapped_column(String(20), default="manual")
    # Generated from synced course files or uploads: those are instructors' or publishers' materials,
    # so the quiz stays with its owner and can't be hosted live for others (UVA PROV-005, copyright).
    from_course_files: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seconds_per_question: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("study_plan.id", ondelete="SET NULL"), index=True)
    # Opt-in sharing, as on Deck. A quiz is shared whole or not at all: it can't be shared while any
    # question copies class materials word for word (share_blocked lists those questions' indexes).
    share_mode: Mapped[str] = mapped_column(String(10), default="private", server_default="private")
    share_token: Mapped[str | None] = mapped_column(String(32), unique=True)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime)
    share_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    taken_down_at: Mapped[datetime | None] = mapped_column(DateTime)
    copied_from_id: Mapped[int | None] = mapped_column(ForeignKey("practice_quiz.id", ondelete="SET NULL"), index=True)
    share_blocked: Mapped[list | None] = mapped_column(JSON)
    # A live game made from a deck (source "deck"): taken down with its deck, and never shared itself.
    from_deck_id: Mapped[int | None] = mapped_column(ForeignKey("deck.id", ondelete="SET NULL"), index=True)
    # Recomputed on every save: some question repeats one from the student's AI quiz from class files
    # ("files"), a set saved from someone else ("copy"), or a set taken down after a report ("removed").
    pasted_from: Mapped[str | None] = mapped_column(String(10))

    course: Mapped[Course | None] = relationship()


class QuizAttempt(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("practice_quiz.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    answers: Mapped[list] = mapped_column(EncryptedJSON("quiz_attempt.answers"))
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Summary(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(EncryptedText("summary.content"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "source_type", "source_id"),)


# ---------------------------------------------------------------- live quiz


class LiveSession(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    quiz_id: Mapped[int] = mapped_column(ForeignKey("practice_quiz.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(20), default="lobby")  # lobby/question/reveal/finished
    question_index: Mapped[int] = mapped_column(Integer, default=-1)
    question_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    rewarded: Mapped[bool] = mapped_column(Boolean, default=False)

    quiz: Mapped[PracticeQuiz] = relationship()
    host: Mapped[User] = relationship()
    players: Mapped[list[LivePlayer]] = relationship(back_populates="session", cascade="all, delete-orphan")


class LivePlayer(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("live_session.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    nickname: Mapped[str] = mapped_column(String(40))
    token: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(24))
    score: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped[LiveSession] = relationship(back_populates="players")

    __table_args__ = (UniqueConstraint("session_id", "nickname"),)


class LiveAnswer(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("live_session.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("live_player.id", ondelete="CASCADE"))
    question_index: Mapped[int] = mapped_column(Integer)
    choice: Mapped[int] = mapped_column(Integer)
    correct: Mapped[bool] = mapped_column(Boolean)
    points: Mapped[int] = mapped_column(Integer)
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("player_id", "question_index"),)


# ---------------------------------------------------------------- AI tutor


class TutorConversation(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(EncryptedText("tutor_conversation.title"), default="New conversation")
    # Sources the student attached to this chat ("file:12", "page:3", "upload:7"; see
    # services/sources.py). Their text goes with every question.
    attachments: Mapped[list | None] = mapped_column(EncryptedJSON("tutor_conversation.attachments"), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    course: Mapped[Course | None] = relationship()
    messages: Mapped[list[TutorMessage]] = relationship(cascade="all, delete-orphan", order_by="TutorMessage.id")


class TutorMessage(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("tutor_conversation.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user / assistant
    content: Mapped[str] = mapped_column(EncryptedText("tutor_message.content"))
    sources: Mapped[list | None] = mapped_column(EncryptedJSON("tutor_message.sources"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIUsage(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float)  # at list price when the call finished
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- class chat


class ChatMessage(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    room_key: Mapped[str] = mapped_column(String(300), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(EncryptedText("chat_message.body"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class ChatReport(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_message.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    message: Mapped[ChatMessage] = relationship()

    __table_args__ = (UniqueConstraint("message_id", "reporter_id"),)


class ShareReport(db.Model):
    """A signed-in user's report of a shared deck or quiz (exactly one of deck_id / quiz_id). A set with
    an open report can't be deleted until it's reviewed; a report outlives its reporter's account,
    without their name."""
    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int | None] = mapped_column(ForeignKey("deck.id", ondelete="CASCADE"), index=True)
    quiz_id: Mapped[int | None] = mapped_column(ForeignKey("practice_quiz.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    reason: Mapped[str] = mapped_column(String(20))  # copyright / exam / personal / other
    details: Mapped[str | None] = mapped_column(EncryptedText("share_report.details"))
    # What was shared when it was reported, so editing or emptying the set can't hide it from review.
    snapshot: Mapped[list | None] = mapped_column(EncryptedJSON("share_report.snapshot"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String(20))  # removed / dismissed

    deck: Mapped[Deck | None] = relationship()
    quiz: Mapped[PracticeQuiz | None] = relationship()
    reporter: Mapped[User | None] = relationship()


# ---------------------------------------------------------------- coins


class CoinTransaction(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    # Idempotency key: the same event can never pay out twice (e.g. "submit:<assignment id>").
    ref: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "ref"),)


# ---------------------------------------------------------------- your own files and integrations


class Upload(db.Model):
    """A file the student added themselves: from their computer or from Google Drive. Text is
    read in the background like Canvas files (services/textjobs.py)."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int | None] = mapped_column(BigInteger)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | drive
    external_id: Mapped[str | None] = mapped_column(String(200))  # the Drive file id
    text: Mapped[str | None] = mapped_column(EncryptedText("upload.text"), deferred=True)
    text_status: Mapped[str | None] = mapped_column(String(40), index=True)
    text_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    course: Mapped[Course | None] = relationship()


class Integration(db.Model):
    """A Google account connected through Composio, per kind ("calendar", "drive")."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    # Calendar: {"enabled": bool, "calendar_id": "primary"}; last sync outcome for the page.
    settings: Mapped[dict | None] = mapped_column(JSON, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "kind"),)


class CalendarPush(db.Model):
    """An assignment's event in the student's Google Calendar, so it's updated, not duplicated."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # SET NULL, not CASCADE: when Canvas deletes the assignment we still need the event id to
    # remove it from Google Calendar.
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignment.id", ondelete="SET NULL"), index=True)
    event_id: Mapped[str] = mapped_column(String(1024))
    calendar_id: Mapped[str] = mapped_column(String(300), default="primary")
    fingerprint: Mapped[str] = mapped_column(String(40))
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "assignment_id"),)


class StudyPlan(db.Model):
    """A plan to study for one test, quiz or exam. Canvas rows are SET NULL (sync deletes rows it
    no longer receives), so the title and date are copied; a plan can also be added by hand."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="SET NULL"))
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignment.id", ondelete="SET NULL"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("calendar_event.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(10), default="test")  # final / midterm / test / quiz
    exam_at: Mapped[datetime | None] = mapped_column(DateTime)  # naive UTC
    # The Canvas time the plan last followed: exam_at follows Canvas only when Canvas changes, so a
    # date the student sets by hand sticks.
    source_at: Mapped[datetime | None] = mapped_column(DateTime)
    tier: Mapped[str] = mapped_column(String(10), default="medium")  # micro / low / medium / high / major / final
    share: Mapped[float | None] = mapped_column(Float)  # fraction of the course grade, when known
    method: Mapped[str] = mapped_column(String(20), default="spaced")
    pacing: Mapped[str] = mapped_column(String(12), default="25_5")
    status: Mapped[str] = mapped_column(String(10), default="active")  # active / done
    # What's on it, in the student's words ("chapters 4-6, lectures 8-12").
    scope: Mapped[str | None] = mapped_column(EncryptedText("study_plan.scope"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    course: Mapped[Course | None] = relationship()
    sessions: Mapped[list[StudySession]] = relationship(back_populates="plan", cascade="all, delete-orphan",
                                                        order_by="StudySession.day, StudySession.position")


class StudySession(db.Model):
    """One scheduled study block of a plan, on a local day."""
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("study_plan.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # local "YYYY-MM-DD", like User.last_active_date
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(20))  # pretest / learn / blurt / explain / mixed / practice_test / review / misses / warmup
    minutes: Mapped[int] = mapped_column(Integer, default=30)
    minutes_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    # What the student wrote in a blank-page or explain-it session (their own coursework).
    notes: Mapped[str | None] = mapped_column(EncryptedText("study_session.notes"))

    plan: Mapped[StudyPlan] = relationship(back_populates="sessions")


class DeckTest(db.Model):
    """A scored practice test taken in Test mode over one or more decks (the planner's readiness)."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("study_plan.id", ondelete="SET NULL"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    seconds: Mapped[int | None] = mapped_column(Integer)
    # [{card_id, kind: mc|typed|tf, correct: bool, given: str}]: the student's own answers.
    answers: Mapped[list | None] = mapped_column(EncryptedJSON("deck_test.answers"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssessmentChoice(db.Model):
    """The student's answer to "is this a test?": for one item, or (item_id None) for every item in
    the course with the same family key ("quiz #"), so one tap fixes a whole series."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"))
    item: Mapped[str] = mapped_column(String(200))  # "a:<assignment id>", "e:<event id>" or "family:<key>"
    kind: Mapped[str] = mapped_column(String(10))  # none / final / midterm / test / quiz
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "course_id", "item"),)


class CalendarFeed(db.Model):
    """A personal calendar link (iCal) from another LMS. The server fetches it about hourly and turns
    its due dates and events into a class list under its own account (CanvasAccount.lms == "ics")."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # The link carries a secret token that opens the student's calendar: encrypted, and found by hash.
    url: Mapped[str] = mapped_column(EncryptedText("calendar_feed.url"))
    url_hash: Mapped[str] = mapped_column(String(64))
    lms: Mapped[str] = mapped_column(String(20), default="other")  # brightspace / blackboard / moodle / schoology / other
    host: Mapped[str] = mapped_column(String(255))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("canvas_account.id", ondelete="SET NULL"))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(300))
    event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "url_hash"),)


class LmsDiagnostic(db.Model):
    """A shape-only check the extension ran on an LMS it can't sync yet (no names, grades or text):
    which endpoints answered, with what status, counts and field types. For building new adapters."""
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    lms: Mapped[str] = mapped_column(String(20))
    host: Mapped[str] = mapped_column(String(255))
    extension_version: Mapped[str | None] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_chunk_course_source", ContentChunk.course_id, ContentChunk.source_type, ContentChunk.source_id)
