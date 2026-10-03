"""Calendar links: due dates from LMSs we don't sync directly (Brightspace, Blackboard, Moodle,
Schoology, or anything else with an iCal feed).

The student pastes the personal calendar link their LMS gives them. The server fetches it about
hourly (refresh_due, from the pages where the student looks at their work), reads it, and turns it
into a Canvas-shaped snapshot that goes through the same ingest as the extension's syncs, under an
account of its own (CanvasAccount.lms == "ics", canvas_user_id "feed-<id>"). Deletions, change
detection, the exam planner, Google Calendar and the ICS export then work as they do for Canvas.

The link carries a secret token that opens the student's calendar: it's stored encrypted, never
shown back in full, and never logged (only its host is). Fetching is server-side, so it's guarded
against reaching private networks (SSRF): every address the host resolves to must be public, the
connection goes to the address we checked (no second lookup), redirects are followed by hand and
re-checked, and the body is capped. No AI is involved; the cost is one small GET an hour.

What each LMS's feed looks like was researched from real feeds published on GitHub, vendor help
pages and the LMS's own source (Moodle). The sources are cited next to the rules they support.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import socket
import threading
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context
from sqlalchemy import or_, select, update

from ..extensions import db
from ..models import Assignment, CalendarFeed, CanvasAccount, Course, SyncRun, User, utcnow

MAX_FEEDS = 5  # per student
MAX_URL_LENGTH = 2000
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
CONNECT_TIMEOUT, READ_TIMEOUT = 5, 15
TOTAL_SECONDS = 40  # the read timeout is per read; this stops a server that trickles bytes forever
MAX_EVENTS = 3000  # VEVENTs kept from one calendar
MAX_OCCURRENCES = 500  # repeats of recurring events, all series together
LOOKBACK, LOOKAHEAD = timedelta(days=14), timedelta(days=120)  # where repeats are expanded
EXPAND_SECONDS = 3.0  # time budget for expanding repeats (a hostile RRULE can be slow to evaluate)
STALE_AFTER = timedelta(minutes=60)
MANUAL_REFRESH_GAP = timedelta(seconds=30)  # "Refresh now" can't be used to hammer a site
FULL_REFRESH_AFTER = timedelta(hours=24)  # without If-None-Match: repeats roll forward, statuses move
USER_AGENT = "HomeworkHatch-CalendarLink/1.0 (+https://homeworkhatch.onrender.com)"

LMS_NAMES = {"brightspace": "Brightspace", "blackboard": "Blackboard", "moodle": "Moodle", "schoology": "Schoology"}

_running: set[int] = set()
_lock = threading.Lock()


class FeedError(Exception):
    """Something the student should read: short, human, and never containing the link."""


def lms_name(lms: str | None) -> str | None:
    return LMS_NAMES.get(lms or "")


def _where(lms: str | None) -> str:
    """'Brightspace', or 'your school's site' for an unknown LMS (in messages)."""
    return lms_name(lms) or "your school's site"


# ---------------------------------------------------------------- the link


def _http_allowed() -> bool:
    """Plain http only while developing or testing; students' links are always https."""
    return has_app_context() and current_app.config.get("ENV_NAME") in ("development", "test")


def _parts(url: str, *, pasted: bool = False):
    """(scheme, host, port, path?query) of a URL we're willing to fetch, or FeedError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FeedError("That doesn't look like a link. Copy the whole calendar link and paste it here.") from None
    scheme = parts.scheme.lower()
    if pasted and scheme in ("webcal", "webcals"):  # what "subscribe" buttons hand out: https underneath
        scheme = "https"
    allowed = ("https", "http") if _http_allowed() else ("https",)
    if scheme not in allowed:
        raise FeedError("Use the https:// (or webcal://) calendar link from your school's site.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FeedError("That link has a username or password in it. Use the calendar link exactly as your school gives it.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FeedError("That link is missing the website address.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise FeedError("That link's website address isn't valid.") from None
    if port not in (None, 443) and not (scheme == "http" and port == 80):
        raise FeedError("Calendar links on unusual ports aren't supported.")
    default = 443 if scheme == "https" else 80
    target = quote(parts.path or "/", safe=_ASCII) + (f"?{quote(parts.query, safe=_ASCII)}" if parts.query else "")
    return scheme, host, port or default, target


def normalize(url: str) -> str:
    """The link as we store and fetch it: https (webcal:// becomes https://), no fragment, no
    default port. Raises FeedError with a message for the student."""
    url = (url or "").strip().strip("<>\"'").strip()
    if not url:
        raise FeedError("Paste your calendar link first.")
    if len(url) > MAX_URL_LENGTH:
        raise FeedError("That link is too long to be a calendar link.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise FeedError("That link has spaces in it. Copy it again in one piece.")
    if "://" not in url:
        raise FeedError("Use the full calendar link, starting with https:// or webcal://.")
    scheme, host, port, _target = _parts(url, pasted=True)
    parts = urlsplit(url)
    netloc = f"[{host}]" if ":" in host else host  # an IPv6 address keeps its brackets
    netloc = netloc if port == (443 if scheme == "https" else 80) else f"{netloc}:{port}"
    query = quote(parts.query, safe=_ASCII)  # only non-ASCII is escaped; the request line must be ASCII
    path = quote(parts.path or "/", safe=_ASCII)
    if re.search(r"/calendar/export_execute\.php$", path, re.I):
        # Moodle's "This week"/"This month"... exports go stale within days; "Recent and next 60 days"
        # always works (hard-coded in export_execute.php) and isn't covered by the link's token.
        query = re.sub(r"(^|&)preset_time=(weeknow|weeknext|monthnow|monthnext)(?=&|$)", r"\1preset_time=recentupcoming",
                       query)
    return urlunsplit((scheme, netloc, path, query, ""))


_ASCII = "".join(chr(c) for c in range(33, 127))


def url_hash(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode()).hexdigest()


def host_of(normalized_url: str) -> str:
    return (urlsplit(normalized_url).hostname or "")[:255]


# Feed URL shapes, by LMS:
# - Brightspace: https://<host>/d2l/le/calendar/feed/user/feed.ics?token=<token> (optionally with
#   feedOU=<org unit> for one course). Real links: Millersville's guide
#   (https://wiki.millersville.edu/spaces/d2ldocs/pages/96733173/Subscribing+to+a+calendar+with+Outlook),
#   RIT's (https://wiki.ritlug.com/mycourses-assignment-calendar-feed.html).
# - Blackboard Learn, Original and Ultra alike: https://<host>/webapps/calendar/calendarFeed/<token>/learn.ics
#   (feeds on GitHub from CUNY 2018, Pima 2026 (Ultra), Fordham 2026, GWU 2026 (Ultra), e.g.
#   https://github.com/adityapatkar/trmnl/blob/HEAD/plugins/blackboard-week/samples/learn.ics).
# - Moodle: https://<site>/calendar/export_execute.php?userid=..&authtoken=..&preset_what=..&preset_time=..
#   (https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/export_execute.php).
# - Schoology: webcal://<district host>/calendar/feed/ical/<number>/<token>/ical.ics; districts often
#   use their own domain (learn.lcps.org, schoology.dasd.org...), so the path decides, not the host
#   (https://github.com/orangishcat/schoology-ics/blob/master/src/config.py,
#   https://github.com/dajun666/schoology-mcp/blob/main/schoology_mcp/ical.py).
_LMS_PATHS = (
    ("brightspace", re.compile(r"/d2l/le/calendar/feed/", re.I)),
    ("blackboard", re.compile(r"/webapps/calendar/calendarfeed/", re.I)),
    ("moodle", re.compile(r"/calendar/export_execute\.php$", re.I)),
    ("schoology", re.compile(r"/calendar/feed/ical/", re.I)),
)
_LMS_HOSTS = (("brightspace", (".brightspace.com", ".desire2learn.com", ".d2l.com")),
              ("blackboard", (".blackboard.com",)), ("schoology", (".schoology.com",)),
              ("moodle", (".moodlecloud.com",)))


def detect_lms(url: str) -> str:
    parts = urlsplit(url)
    for lms, pattern in _LMS_PATHS:
        if pattern.search(parts.path or ""):
            return lms
    host = "." + (parts.hostname or "").lower()
    for lms, suffixes in _LMS_HOSTS:
        if host.endswith(suffixes):
            return lms
    return "other"


def lms_from_calendar(prodid: str, uids: list[str]) -> str | None:
    """A second opinion from the calendar itself, for links on unrecognised paths.
    PRODIDs seen in real feeds: Brightspace '-//D2L//NONSGML v1.0//EN' (Purdue, NSCC feeds on GitHub),
    Blackboard '-//Blackboard//EN' (all four Blackboard feeds above), Moodle '-//Moodle Pty Ltd//NONSGML
    Moodle Version ...//EN' (calendar/lib.php). Schoology's header isn't known; its UIDs end in
    '@schoology.com' (https://github.com/CarterH93/ICS-Parser-Example-Project)."""
    p = (prodid or "").lower()
    if "//d2l//" in p or "desire2learn" in p or "brightspace" in p:
        return "brightspace"
    if "//blackboard//" in p:
        return "blackboard"
    if "moodle" in p:
        return "moodle"
    if "schoology" in p or any(u.endswith("@schoology.com") for u in uids[:50]):
        return "schoology"
    if any(u.startswith("_blackboard.") or "-_blackboard." in u for u in uids[:50]):
        return "blackboard"
    return None


# ---------------------------------------------------------------- fetching (SSRF-guarded)


@dataclass
class Fetched:
    status: int  # 200, or 304 (not modified since the ETag we sent)
    body: bytes = b""
    etag: str | None = None


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise FeedError("We couldn't find that website. Check the link.") from None
    return list(dict.fromkeys(info[4][0] for info in infos))


def _public(address: str) -> bool:
    """Only ordinary internet addresses: not loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7
    unique-local), link-local (169.254/16 cloud metadata, fe80::/10), multicast, reserved, unspecified,
    carrier-grade NAT, or IPv6 forms that wrap one of those (mapped, 6to4, Teredo, NAT64)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if ip.version == 6:
        if ip.is_site_local:
            return False
        inner = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None]
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            inner.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(x is not None and not _public(str(x)) for x in inner):
            return False
    return True


def _safe_address(host: str, port: int) -> str:
    addresses = _resolve(host, port)
    if not addresses:
        raise FeedError("We couldn't find that website. Check the link.")
    if not all(_public(a) for a in addresses):  # every one: a rebinding host can't hide a private one
        raise FeedError("That link points to a private network address, which we can't open.")
    return addresses[0]


def _open(scheme: str, address: str, port: int, host: str, target: str, headers: dict):
    """One GET to an address we already checked: the connection goes to `address` (no second DNS
    lookup that could return something else), while TLS still verifies the certificate for `host`.
    Returns a urllib3 response that hasn't been read yet."""
    import urllib3

    timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)
    if scheme == "https":
        try:
            import certifi

            ca = certifi.where()
        except ImportError:  # pragma: no cover - requests ships certifi
            ca = None
        pool = urllib3.HTTPSConnectionPool(address, port=port, timeout=timeout, retries=False, maxsize=1,
                                           server_hostname=host, assert_hostname=host,
                                           cert_reqs="CERT_REQUIRED", ca_certs=ca)
    else:
        pool = urllib3.HTTPConnectionPool(address, port=port, timeout=timeout, retries=False, maxsize=1)
    host_header = f"[{host}]" if ":" in host else host
    return pool.urlopen("GET", target, headers={**headers, "Host": host_header}, redirect=False, retries=False,
                        preload_content=False, decode_content=True)


def _status_message(status: int, lms: str | None) -> str:
    where = _where(lms)
    if status in (401, 403):
        return f"{where} refused the link ({status}). Copy a fresh calendar link from {where}."
    if status in (404, 410):
        return f"The link stopped working ({status}). Copy a fresh link from {where}."
    if status == 429 or status >= 500:
        return f"{where} didn't answer properly ({status}). We'll try again later."
    return f"The link didn't work ({status}). Copy a fresh calendar link from {where}."


def _read(resp, deadline: float) -> bytes:
    length = resp.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > MAX_BYTES:
        raise FeedError("That calendar is bigger than 5 MB, too big to read.")
    chunks, size = [], 0
    for chunk in resp.stream(64 * 1024, decode_content=True):
        size += len(chunk)
        if size > MAX_BYTES:  # counted after decompression, so a zip bomb stops here too
            raise FeedError("That calendar is bigger than 5 MB, too big to read.")
        if time.monotonic() > deadline:
            raise FeedError("The calendar took too long to download. We'll try again later.")
        chunks.append(chunk)
    return b"".join(chunks)


def _looks_like_calendar(body: bytes) -> bool:
    return body.lstrip(b"\xef\xbb\xbf \t\r\n")[:15].upper() == b"BEGIN:VCALENDAR"


def fetch_url(url: str, etag: str | None = None, lms: str | None = None, log_ref: str = "") -> Fetched:
    """GET a calendar link with the SSRF guards. Raises FeedError (the message is for the student)."""
    import urllib3

    headers = {"User-Agent": USER_AGENT, "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.5",
               "Accept-Encoding": "gzip, deflate"}
    if etag:
        headers["If-None-Match"] = etag
    deadline = time.monotonic() + TOTAL_SECONDS
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, target = _parts(current)
        address = _safe_address(host, port)
        try:
            resp = _open(scheme, address, port, host, target, headers)
        except urllib3.exceptions.SSLError:
            raise FeedError(f"We couldn't open a secure connection to {host}. We'll try again later.") from None
        except (urllib3.exceptions.HTTPError, OSError, ValueError):
            raise FeedError(f"We couldn't reach {host}. We'll try again later.") from None
        try:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise FeedError(_status_message(resp.status, lms))
                current = urljoin(current, location)  # checked like the first hop on the next turn
                continue
            if resp.status == 304 and etag:
                _log("not modified", log_ref, host)
                return Fetched(304, etag=etag)
            if resp.status != 200:
                _log(f"status {resp.status}", log_ref, host)
                raise FeedError(_status_message(resp.status, lms))
            try:
                body = _read(resp, deadline)
            except (urllib3.exceptions.HTTPError, OSError):
                raise FeedError(f"The download from {host} broke off. We'll try again later.") from None
            if not _looks_like_calendar(body):
                _log("not a calendar", log_ref, host)
                # Moodle answers 200 with plain text when the token stopped matching (it changes with the
                # password) or calendar export is off (calendar/export_execute.php).
                if body.strip()[:40].lower() == b"invalid authentication":
                    raise FeedError("Moodle says this link is no longer valid (it changes when your password does). "
                                    "Copy a fresh link from Moodle's calendar.")
                if body.strip()[:40].lower() == b"no export":
                    raise FeedError("Your school has turned off calendar export in Moodle, so this link can't be read.")
                if b"<html" in body[:2000].lower() or b"<!doctype" in body[:2000].lower():
                    raise FeedError("That link opened a web page (maybe a sign-in page), not a calendar. "
                                    "Copy the calendar's feed (subscribe) link instead.")
                raise FeedError("That link didn't return a calendar. Copy the calendar (iCal) link again.")
            _log(f"{len(body)} bytes", log_ref, host)
            return Fetched(200, body, (resp.headers.get("ETag") or "")[:300] or None)
        finally:
            try:
                resp.release_conn()
            except Exception:  # pragma: no cover - closing is best effort
                pass
    raise FeedError("The link redirected too many times.")


def _log(what: str, ref: str, host: str) -> None:
    """Host only: the full link opens the student's calendar."""
    if has_app_context():
        current_app.logger.info("calendar link %s on %s: %s", ref or "-", host, what)


def fetch(feed: CalendarFeed, etag: str | None = None) -> Fetched:
    return fetch_url(feed.url, etag, feed.lms, log_ref=str(feed.id or ""))


# ---------------------------------------------------------------- reading the calendar


@dataclass
class Item:
    """One occurrence of a calendar entry, in the app's terms (naive UTC times)."""

    uid: str  # stable identity of the entry (see _stable_uid)
    rid: str  # "" or the occurrence's original start, for repeats
    summary: str
    description: str
    location: str
    categories: list[str]
    url: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    first_day: date | None  # all-day entries: the dates they cover (end inclusive)
    last_day: date | None


@dataclass
class Parsed:
    name: str  # X-WR-CALNAME, or ""
    prodid: str
    items: list[Item]
    shape: dict


SHAPE_PROPS = ("SUMMARY", "DESCRIPTION", "LOCATION", "CATEGORIES", "URL", "STATUS", "DTEND", "DURATION", "RRULE",
               "RDATE", "EXDATE", "RECURRENCE-ID", "CLASS", "LAST-MODIFIED", "SEQUENCE")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value).strip()


def _categories(value) -> list[str]:
    out = []
    for v in value if isinstance(value, list) else [value] if value is not None else []:
        cats = getattr(v, "cats", None)
        out += [str(c).strip() for c in cats] if cats is not None else [str(v).strip()]
    return [c for c in out if c]


def _zone(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def _decoded(comp, name: str):
    try:
        value = comp.decoded(name)
    except Exception:
        return None
    return value if isinstance(value, (date, timedelta)) else None


def _utc(value: datetime, floating: ZoneInfo) -> datetime:
    """Naive UTC. Floating times (no zone) are read in the calendar's or the student's zone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=floating)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, dtime.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def _rid_key(value, floating: ZoneInfo) -> str:
    """How an occurrence is named, the same for the series and its overrides (RECURRENCE-ID)."""
    if isinstance(value, datetime):
        return _utc(value, floating).strftime("%Y%m%dT%H%M%SZ")
    return value.isoformat()


# Blackboard's 2018-era UIDs start with a timestamp that changes on every download
# ("20180829T175006Z-_blackboard.platform.gradebook2.GradableItem-_8947705_1@host"); the
# "_blackboard.<kind>-_<n>_1" part is the stable one (CUNY feed,
# https://github.com/apag101/CUNYSPS/blob/HEAD/Misc/Fall2018Calendar.ics).
_BB_UID = re.compile(r"_blackboard\.[\w.]+-_\d+_\d+")


def _stable_uid(uid: str) -> str:
    m = _BB_UID.search(uid)
    return m.group(0) if m else uid


def _occurrence_starts(comp, start, floating: ZoneInfo, lo: datetime, hi: datetime, budget: list[int],
                       deadline: float) -> list | None:
    """Starts of a repeating entry's occurrences between lo and hi (aware UTC), at most budget[0] of
    them all together. None when the rule can't be read (the entry then counts once)."""
    from dateutil.rrule import rruleset, rrulestr

    rules = comp.get("RRULE")
    rules = rules if isinstance(rules, list) else [rules]
    is_date = not isinstance(start, datetime)
    tz = None if is_date else (start.tzinfo or floating)
    # Work in the entry's own wall-clock time, as RFC 5545 does (so 9:00 stays 9:00 across DST).
    base = datetime.combine(start, dtime.min) if is_date else start.replace(tzinfo=None)

    def wall(value) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is not None and tz is not None:
                return value.astimezone(tz).replace(tzinfo=None)
            if value.tzinfo is not None:  # an all-day series with a timed exception: use its date
                return datetime.combine(value.astimezone(floating).date(), dtime.min)
            return value
        if isinstance(value, date):
            return datetime.combine(value, base.time())
        return None

    zone = tz or floating
    lo_w = lo.astimezone(zone).replace(tzinfo=None) - (timedelta(days=1) if is_date else timedelta(0))
    hi_w = hi.astimezone(zone).replace(tzinfo=None)
    rset = rruleset()
    try:
        for rule in rules:
            text = rule.to_ical().decode() if hasattr(rule, "to_ical") else str(rule)
            if re.search(r"FREQ=(SECONDLY|MINUTELY)", text, re.I):
                return None  # not something a class calendar uses; too costly to expand
            # UNTIL in the entry's wall-clock time (dateutil wants it to match DTSTART), and never
            # past the window: that also bounds rules that never match anything.
            m = re.search(r"UNTIL=(\d{8})(T(\d{6})(Z)?)?", text, re.I)
            until = hi_w
            if m:
                if m.group(2):
                    u = datetime.strptime(m.group(1) + m.group(3), "%Y%m%d%H%M%S")
                    if m.group(4):
                        u = u.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
                else:
                    u = datetime.strptime(m.group(1), "%Y%m%d").replace(hour=23, minute=59, second=59)
                until = min(u, hi_w)
                text = text[:m.start()] + text[m.end():]
            text = re.sub(r";;+", ";", text).strip(";") + f";UNTIL={until:%Y%m%dT%H%M%S}"
            with warnings.catch_warnings():  # COUNT together with UNTIL: dateutil warns, still works
                warnings.simplefilter("ignore")
                rset.rrule(rrulestr(text, dtstart=base, ignoretz=True))
        for prop, add in (("RDATE", rset.rdate), ("EXDATE", rset.exdate)):
            values = comp.get(prop)
            for v in values if isinstance(values, list) else [values] if values is not None else []:
                for d in getattr(v, "dts", []):
                    w = wall(d.dt)
                    if w is not None:
                        add(w)
    except Exception:
        return None
    out = []
    for occ in rset:
        if occ > hi_w or budget[0] <= 0 or time.monotonic() > deadline:
            break
        if occ < lo_w:
            continue
        out.append(occ.date() if is_date else occ.replace(tzinfo=tz))
        budget[0] -= 1
    return out


def parse(body: bytes, tz: ZoneInfo, now: datetime | None = None) -> Parsed:
    """Read a calendar into Items. Cancelled entries are skipped; repeats are expanded from LOOKBACK
    to LOOKAHEAD around now (at most MAX_OCCURRENCES); at most MAX_EVENTS entries are kept, the
    upcoming and recent ones first. Raises FeedError when it isn't a calendar we can read."""
    from icalendar import Calendar

    now = now or utcnow()
    try:
        calendars = Calendar.from_ical(body.lstrip(b"\xef\xbb\xbf"), multiple=True)
    except Exception:
        raise FeedError("We couldn't read that calendar. Copy the calendar link again.") from None
    if not calendars:
        raise FeedError("We couldn't read that calendar. Copy the calendar link again.")
    first = calendars[0]
    name = _text(first.get("X-WR-CALNAME"))
    prodid = _text(first.get("PRODID"))
    floating = _zone(_text(first.get("X-WR-TIMEZONE"))) or tz
    shape: dict = {"prodid": prodid[:100], "calendar_name": bool(name), "vevents": 0, "props": {}, "x_props": {},
                   "times": {"utc": 0, "zoned": 0, "floating": 0, "all_day": 0, "unreadable": 0},
                   "cancelled": 0, "recurring": 0, "occurrences": 0, "overrides": 0}
    props, xprops = shape["props"], shape["x_props"]

    comps = [c for cal in calendars for c in cal.walk("VEVENT")]
    shape["vevents"] = len(comps)
    masters, overrides, singles = {}, {}, []
    for comp in comps:
        for key in comp.keys():
            if key in SHAPE_PROPS:
                props[key] = props.get(key, 0) + 1
            elif key.startswith("X-") and (key[:40] in xprops or len(xprops) < 25):
                xprops[key[:40]] = xprops.get(key[:40], 0) + 1
        start = _decoded(comp, "DTSTART")
        if not isinstance(start, date):
            shape["times"]["unreadable"] += 1
            continue
        kind = ("all_day" if not isinstance(start, datetime) else "floating" if start.tzinfo is None
                else "utc" if start.utcoffset() == timedelta(0) and "TZID" not in comp.get("DTSTART").params
                else "zoned")
        shape["times"][kind] += 1
        uid = _stable_uid(_text(comp.get("UID"))) or "nouid-" + hashlib.sha1(
            f"{_text(comp.get('SUMMARY'))}|{start}".encode()).hexdigest()
        cancelled = _text(comp.get("STATUS")).upper() == "CANCELLED"
        if "RECURRENCE-ID" in comp:
            rid = _decoded(comp, "RECURRENCE-ID")
            if isinstance(rid, date):
                overrides[(uid, _rid_key(rid, floating))] = (comp, start, cancelled)
                continue
        if cancelled:
            shape["cancelled"] += 1
            continue
        if "RRULE" in comp:
            masters[uid] = (comp, start)
        else:
            singles.append((uid, "", comp, start))

    lo, hi = (now - LOOKBACK).replace(tzinfo=timezone.utc), (now + LOOKAHEAD).replace(tzinfo=timezone.utc)
    budget, deadline = [MAX_OCCURRENCES], time.monotonic() + EXPAND_SECONDS
    occurrences = []
    for uid, (comp, start) in masters.items():
        shape["recurring"] += 1
        starts = _occurrence_starts(comp, start, floating, lo, hi, budget, deadline)
        if starts is None:
            singles.append((uid, "", comp, start))
            continue
        for occ in starts:
            key = _rid_key(occ, floating)
            override = overrides.pop((uid, key), None)
            if override is not None:
                shape["overrides"] += 1
                o_comp, o_start, o_cancelled = override
                if o_cancelled:
                    shape["cancelled"] += 1
                    continue
                occurrences.append((uid, key, o_comp, o_start, None))
            else:
                occurrences.append((uid, key, comp, occ, occ))
    shape["occurrences"] = len(occurrences)
    shape["occurrences_capped"] = budget[0] <= 0 or time.monotonic() > deadline
    for (uid, key), (comp, start, cancelled) in overrides.items():  # an override whose series we didn't expand
        if cancelled:
            shape["cancelled"] += 1
        elif uid not in masters:
            singles.append((uid, key, comp, start))

    items = []
    for uid, rid, comp, start, occ in [(u, r, c, s, None) for u, r, c, s in singles] + occurrences:
        try:
            items.append(_item(uid, rid, comp, start, occ, floating, tz))
        except Exception:  # one malformed entry (mixed date types...) doesn't sink the calendar
            shape["times"]["unreadable"] += 1
    if len(items) > MAX_EVENTS:  # keep what's coming up and what just happened
        recent = now - LOOKBACK
        items.sort(key=lambda i: (i.start < recent, abs((i.start - now).total_seconds())))
        items = items[:MAX_EVENTS]
        shape["events_capped"] = True
    shape["items"] = len(items)
    return Parsed(name, prodid, items, shape)


def _item(uid: str, rid: str, comp, start, occurrence, floating: ZoneInfo, tz: ZoneInfo) -> Item:
    master_start = _decoded(comp, "DTSTART")
    end = _decoded(comp, "DTEND")
    duration = _decoded(comp, "DURATION")
    if not isinstance(end, date):
        end = start + duration if isinstance(duration, timedelta) else None
    elif occurrence is not None and isinstance(master_start, date):
        end = occurrence + (end - master_start)  # each repeat keeps the series' length
    all_day = not isinstance(start, datetime)
    if all_day:
        last = end - timedelta(days=1) if isinstance(end, date) and not isinstance(end, datetime) and end > start \
            else start
        first_day, last_day = start, last
        start_at = _local_midnight(start, tz)
        end_at = _local_midnight(last + timedelta(days=1), tz)
    else:
        first_day = last_day = None
        start_at = _utc(start, floating)
        end_at = _utc(end, floating) if isinstance(end, datetime) else None
    # Moodle runs names through format_string, so "&" arrives as "&amp;" (lib/classes/formatting.php).
    return Item(uid=uid, rid=rid, summary=html.unescape(_text(comp.get("SUMMARY")))[:500],
                description=_text(comp.get("DESCRIPTION"))[:20000],
                location=html.unescape(_text(comp.get("LOCATION")))[:500],
                categories=[html.unescape(c) for c in _categories(comp.get("CATEGORIES"))[:10]],
                url=_text(comp.get("URL"))[:1000],
                start=start_at, end=end_at, all_day=all_day, first_day=first_day, last_day=last_day)


# ---------------------------------------------------------------- what each entry is


DASH = r"\s+[-–—]\s+"  # Brightspace writes " - "; en/em dashes cost nothing to accept

# Brightspace appends the date's role to the item's title. Real feeds (Purdue 2024, NSCC 2024, UVM
# 2025 on GitHub: https://github.com/RishabhAgarwal143/Gradia/blob/HEAD/Flask_server/Calendar_examples/feed.ics,
# https://github.com/gax1985/NSCC/blob/HEAD/NSCC/Winter%20Semester%202024/Professional%20Practices%20for%20IT/feed.ics)
# only show " - Due", " - Available" and " - Availability Ends"; events instructors create have no
# suffix. " - Start Date", " - End Date" and " - Availability Starts" were never seen but are
# accepted in case other versions use them. The suffix follows the student's language: "à échéance"
# is from a French project (https://github.com/MrTh0m/Brightspace_agenda/blob/HEAD/api.php); other
# languages fall back to "event".
BRIGHTSPACE_DUE = re.compile(DASH + r"due\s*$|\s+(?:[-–—]\s+)?à échéance\s*$", re.I)
BRIGHTSPACE_ENDS = re.compile(DASH + r"(availability ends|end date)\s*$", re.I)
BRIGHTSPACE_STARTS = re.compile(DASH + r"(available|availability starts|start date)\s*$", re.I)
# The DESCRIPTION ends with a type header and the item's own link, then "View event - <link>":
#   Quizzes:\nQUIZ: Input/Output - https://<host>/d2l/lms/quizzing/quizzing.d2l?ou=280902&qi=310293
#   ...View event - https://<host>/d2l/le/calendar/<ou>/event/<id>/detailsview?ou=<ou>#<id>
BRIGHTSPACE_ITEM_LINK = re.compile(r"https://[^\s\"'<>]+/d2l/(lms/(dropbox|quizzing|discussions|survey)|le/content)/[^\s\"'<>]+", re.I)
BRIGHTSPACE_EVENT_LINK = re.compile(r"https://[^\s\"'<>]+/d2l/le/calendar/(\d+)/event/\d+[^\s\"'<>]*", re.I)

# Moodle names calendar events after the activity, in the language of whoever saved it. English
# strings from Moodle's own language packs (MOODLE_405_STABLE, wording current since 3.4):
# - mod/assign 'calendardue' '{$a} is due', 'calendarextension' '{$a} is due (extension)' (4.5),
#   'calendargradingdue' '{$a} is due to be graded' (graders only); overrides add ' (Due date)';
# - mod/quiz 'quizeventopens'/'quizeventcloses' '{$a} opens'/'{$a} closes' (lesson, choice, feedback,
#   data and scorm say the same); quiz overrides are named '{$a} - Override' first;
# - mod/forum 'calendardue' '{$a} is due'; core_completion 'completionexpectedfor'
#   '{$a->instancename} should be completed'; mod/bigbluebuttonbn '{$a} is scheduled for';
# - mod/workshop '{$a} opens for submissions', '{$a} deadline for submissions', '{$a} opens for
#   assessment', '{$a} deadline for assessment' (3.3 and older: '{$a} (submissions deadline)' etc.);
# - 3.3 and older: '{$a} (Quiz opens)' / '(Quiz closes)'.
# https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/lang/en/assign.php,
# .../mod/quiz/lang/en/quiz.php, .../mod/workshop/lib.php, .../completion/classes/api.php.
# Other languages fall back to "event".
MOODLE_EVENT = re.compile(r"\s+(is due to be graded|opens|opens for submissions|opens for assessment|is scheduled for)\s*$"
                          r"|\s*\(([\w ]+ opens|submissions open|assessments? open)\)\s*$", re.I)
MOODLE_DUE = re.compile(r"\s+(is due|is due \(extension\)|closes|deadline for submissions)\s*$"
                        r"|\s*\((due date|[\w ]+ closes|submissions deadline)\)\s*$", re.I)
MOODLE_ASSESS_DUE = re.compile(r"\s+deadline for assessment\s*$|\s*\(assessments? deadline\)\s*$", re.I)  # name kept
MOODLE_SOFT = re.compile(r"\s+should be completed\s*$", re.I)
MOODLE_OVERRIDE = re.compile(r"\s+[-–]\s+override\s*$", re.I)
MOODLE_SITE = {"site events", "site event"}  # CATEGORIES of site-wide events: not a class

# Blackboard adds nothing to titles and leaves the course out; the UID says what an entry is
# (four real feeds, 2018-2026, e.g. https://github.com/BanksChrissy/Paramedic/blob/HEAD/Paramedic/learn.ics):
# gradebook2.GradableItem = a graded column with a due date (assignment, test, graded discussion);
# data.calendar.CalendarEntry = an event made in the calendar; data.discussionboard.Engagement = an
# Ultra discussion's participation date (its GradableItem twin carries the due date).
BLACKBOARD_DUE_UID = re.compile(r"gradebook2\.GradableItem", re.I)

# Schoology: titles are the item's own, with no course and no prefix; the URL says what it is
# (http://<host>/assignment/<id>, /event/<id>/profile, /course/<section>/materials/discussion/view/<id>,
# /assessment/<id>), and DTSTART is the due time (DTEND is an hour later). From a real 2022 feed dump
# (https://github.com/CarterH93/ICS-Parser-Example-Project/blob/main/Sample%20Console%20Data.rtf) and
# projects reading live feeds (https://github.com/torwager/schoology/blob/main/docs/design-notes.md).
SCHOOLOGY_DUE_URL = re.compile(r"/(assignment|assessment)/\d+|/discussion/view/\d+", re.I)
SCHOOLOGY_QUIZ_URL = re.compile(r"/assessment/\d+", re.I)

# Any other calendar: only unmistakable markers make something a due date.
GENERIC_DUE = re.compile(DASH + r"due\s*$|\s+is due\s*$|\s*\(due\)\s*$|^\s*due\s*:\s*", re.I)
QUIZ_WORDS = re.compile(r"\bquiz(zes)?\b|\bquizzing\b", re.I)


@dataclass
class Entry:
    item: Item
    kind: str  # "due", "soft_due" (the deadline only if the item has no real due date), "event"
    name: str
    is_quiz: bool = False
    course: str | None = None
    html_url: str | None = None
    # Which entries are the same item: an item's "Due" and its "Availability Ends" or "Available".
    keys: set = field(default_factory=set)
    starts: bool = False  # an "opens" / "Available" marker of an item
    unfiled: bool = False  # an opening whose item has a deadline: on the calendar, not in the class


def _https_link(text: str) -> str | None:
    m = re.search(r"https://[^\s\"'<>]+", text or "")
    return m.group(0).rstrip(").,;") if m else None


def _item_url(item: Item, host: str) -> str | None:
    """The entry's own URL when it's https (http on the feed's own host is upgraded: Schoology
    writes http:// links to pages it serves over https)."""
    url = item.url
    if url.startswith("http://") and (urlsplit(url).hostname or "").lower() in (host, f"www.{host}"):
        url = "https://" + url[7:]
    return url if url.startswith("https://") else None


def _strip(title: str, m: re.Match) -> str:
    return title[:m.start()].strip() or title


def classify(item: Item, lms: str, host: str) -> Entry:
    """Due date or plain event, the name without the LMS's suffix, quiz or not, and the item's link."""
    title = item.summary.strip() or "Untitled"
    if lms == "brightspace":
        link = BRIGHTSPACE_ITEM_LINK.search(item.description)
        event_link = BRIGHTSPACE_EVENT_LINK.search(item.description)
        html_url = _item_url(item, host) or (link.group(0) if link else None) or (event_link.group(0) if event_link else None)
        quiz = bool(re.search(r"(^|\n)\s*quizzes\s*:", item.description, re.I) or (link and "/quizzing/" in link.group(0)))
        keys = set()
        if link:  # the quiz's qi= or the dropbox folder's db= ties an item's Available/Due/Ends together
            q = parse_qs(urlsplit(link.group(0)).query)
            keys.add(next((f"{k}:{q[k][0]}" for k in ("qi", "db", "forumId", "topicId", "si") if q.get(k)),
                          link.group(0)))
        for pattern, kind in ((BRIGHTSPACE_DUE, "due"), (BRIGHTSPACE_ENDS, "soft_due"), (BRIGHTSPACE_STARTS, "event")):
            m = pattern.search(title)
            if m:
                name = _strip(title, m)
                return Entry(item, kind, name, quiz or bool(QUIZ_WORDS.search(name)), html_url=html_url,
                             keys=keys | {name.lower()}, starts=pattern is BRIGHTSPACE_STARTS)
        return Entry(item, "event", title, html_url=html_url)
    if lms == "moodle":
        html_url = _item_url(item, host)
        quiz = "quiz" in title.lower()
        m = MOODLE_EVENT.search(title)
        if m:
            name = MOODLE_OVERRIDE.sub("", _strip(title, m))
            starts = "graded" not in m.group(0).lower() and "scheduled" not in m.group(0).lower()
            return Entry(item, "event", title, html_url=html_url, keys={name.lower()}, starts=starts)
        for pattern, kind, strip in ((MOODLE_DUE, "due", True), (MOODLE_ASSESS_DUE, "due", False),
                                     (MOODLE_SOFT, "soft_due", True)):
            m = pattern.search(title)
            if m:
                name = MOODLE_OVERRIDE.sub("", _strip(title, m)) if strip else title
                return Entry(item, kind, name, quiz and kind == "due", html_url=html_url, keys={name.lower()})
        return Entry(item, "event", title, html_url=html_url)
    if lms == "blackboard":
        due = bool(BLACKBOARD_DUE_UID.search(item.uid))
        return Entry(item, "due" if due else "event", title, due and bool(QUIZ_WORDS.search(title)),
                     html_url=_item_url(item, host), keys={title.lower()})
    if lms == "schoology":
        url = _item_url(item, host) or _https_link(item.description)
        path = urlsplit(item.url or "").path
        due = bool(SCHOOLOGY_DUE_URL.search(path)) or bool(GENERIC_DUE.search(title))
        quiz = bool(SCHOOLOGY_QUIZ_URL.search(path) or QUIZ_WORDS.search(title))
        name = GENERIC_DUE.sub("", title).strip() or title
        return Entry(item, "due" if due else "event", name if due else title, due and quiz, html_url=url,
                     keys={name.lower()})
    html_url = _item_url(item, host) or _https_link(item.description)
    if GENERIC_DUE.search(title):
        name = GENERIC_DUE.sub("", title).strip() or title
        return Entry(item, "due", name, bool(QUIZ_WORDS.search(name)), html_url=html_url, keys={name.lower()})
    return Entry(item, "event", title, html_url=html_url)


def due_time(entry: Entry, lms: str, tz: ZoneInfo) -> datetime | None:
    """When a due entry is due (naive UTC). An all-day due date is due at 23:59 that day in the
    student's zone (the LMS's real time isn't knowable). Brightspace, Blackboard and Moodle write due
    dates as zero-length entries (DTSTART = DTEND), where either works; Schoology's DTSTART is the due
    time and DTEND an hour later; other calendars' entries ending at the due time (as our own export
    does) make DTEND the safer default."""
    item = entry.item
    day = item.last_day or item.first_day if item.all_day else None
    if lms == "schoology" and not item.all_day and item.start and item.end in (None, item.start):
        # Older Schoology feeds wrote an undated-time item as local midnight in UTC, start = end
        # (https://github.com/CarterH93/ICS-Parser-Example-Project): that's a date, not 12:00 AM.
        local = item.start.replace(tzinfo=timezone.utc).astimezone(tz)
        if local.time() == dtime.min:
            day = local.date()
    if day is not None:
        return datetime.combine(day, dtime(23, 59), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    if lms == "schoology":
        return item.start
    return item.end if item.end and item.end >= (item.start or item.end) else item.start


# ---------------------------------------------------------------- classes


# A course code inside a longer name: "ECE 36800-001", "COMM1700", "ANTH1400A".
CODE = re.compile(r"\b([A-Z]{2,6}[ -]?\d{2,5}[A-Z]?(?:-\d{1,4})?)\b")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _course_key(name: str) -> str:
    return hashlib.sha1(_norm(name).lower().encode()).hexdigest()[:16]


def group_courses(entries: list[Entry], lms: str, calendar_name: str) -> None:
    """Fill entry.course with the class each entry belongs to, where the feed says.
    - Brightspace puts the course offering's name in LOCATION ("Spring 2024 ECE 36800-001 LEC"); an
      instructor's event with its own location reads "<location> (<course>)"; institution events carry
      the institution's name, which is also in X-WR-CALNAME ("All Courses - <institution>"). The
      "View event" link's /d2l/le/calendar/<ou>/ ties an entry to its course too (Purdue, NSCC, UVM feeds).
    - Moodle writes the course's short name as CATEGORIES (export_execute.php: add_property('categories',
      format_string($courses[$event->courseid]->shortname))); site events say "Site events", user and
      category events have none.
    - Blackboard and Schoology don't name the course anywhere (see above): everything goes into one class.
    - Other calendars: CATEGORIES when present."""
    if lms == "brightspace":
        institution = re.sub(r"^\s*all courses\s*[-–—]\s*", "", calendar_name or "", flags=re.I).strip().lower()
        known, by_ou = set(), {}
        for e in entries:
            loc = _norm(e.item.location)
            if e.keys:  # items with a Due/Available/Ends suffix: LOCATION is the course
                if loc and loc.lower() != institution:
                    known.add(loc)
                    m = BRIGHTSPACE_EVENT_LINK.search(e.item.description)
                    if m:
                        by_ou.setdefault(m.group(1), loc)
        for e in entries:
            loc = _norm(e.item.location)
            m = BRIGHTSPACE_EVENT_LINK.search(e.item.description)
            paren = re.match(r"^(.*\S)\s+\(([^()]+)\)$", loc)
            if loc in known:
                e.course = loc
            elif paren and _norm(paren.group(2)) in known:
                e.course = _norm(paren.group(2))
            elif m and m.group(1) in by_ou:
                e.course = by_ou[m.group(1)]
            elif not loc or loc.lower() == institution:
                e.course = None
            elif paren:
                e.course = _norm(paren.group(2))
            else:
                e.course = loc
        return
    if lms in ("moodle", "other"):
        for e in entries:
            cat = _norm(e.item.categories[0]) if e.item.categories else ""
            e.course = cat if cat and cat.lower() not in MOODLE_SITE else None


# ---------------------------------------------------------------- the snapshot


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:40]


def build_snapshot(feed: CalendarFeed, parsed: Parsed, tz: ZoneInfo, now: datetime | None = None) -> tuple[dict, dict]:
    """The calendar as a Canvas-shaped snapshot for ingest.ingest_snapshot (the contract in
    tests/conftest.py BASE_SNAPSHOT), plus counts. Lists the feed can't supply are null ("keep");
    assignments and events are the feed's to decide, so they're always lists."""
    now = now or utcnow()
    lms = feed.lms
    entries = [classify(i, lms, feed.host) for i in parsed.items]
    group_courses(entries, lms, parsed.name)
    # "Availability Ends" / "should be completed" is the deadline only when the item has no due date.
    hard = {(e.course, k) for e in entries if e.kind == "due" for k in e.keys}
    for e in entries:
        if e.kind == "soft_due":
            e.kind = "event" if any((e.course, k) in hard for k in e.keys) else "due"
    # An item's "opens"/"Available" marker stays on the calendar but out of its class, so the exam
    # planner sees the quiz once (at its deadline), not also as a second test on the day it opens.
    deadlines = {(e.course, k) for e in entries if e.kind == "due" for k in e.keys}
    for e in entries:
        if e.starts and any((e.course, k) in deadlines for k in e.keys):
            e.unfiled = True
    fallback_name = _norm(parsed.name) or f"Calendar ({feed.host})"
    any_course = any(e.course for e in entries)
    courses: dict[str, dict] = {}

    def course_for(name: str | None) -> str:
        name = (name or fallback_name)[:300]
        key = _course_key(name)
        if key not in courses:
            code = CODE.search(name)
            courses[key] = {
                "id": key, "name": name, "course_code": (code.group(1) if code else None) if lms != "moodle" else name,
                "class_key": f"ics-{feed.id}::{name.lower()}", "on_dashboard": None, "term": None, "grade": None,
                "html_url": None, "syllabus_html": None, "files_tab_hidden": False,
                "assignment_groups": [], "assignments": [], "modules": None, "pages": None, "files": None,
                "discussions": None, "quizzes": None, "announcements": None,
            }
        return key

    events, due_count = [], 0
    for e in entries:
        item = e.item
        if e.kind == "due":
            due = due_time(e, lms, tz)
            if due is None:
                continue
            cid = course_for(e.course)
            courses[cid]["assignments"].append({
                "id": _id(item.uid, item.rid), "name": e.name[:500], "due_at": _iso(due), "unlock_at": None,
                "lock_at": None, "points_possible": None, "grading_type": None,
                "submission_types": ["online_quiz"] if e.is_quiz else [], "is_quiz": e.is_quiz,
                "html_url": e.html_url, "description_html": None,
                # The feed can't say what was handed in: "past_due" only means the date passed.
                "status": "upcoming" if due > now else "past_due", "submission": None,
            })
            due_count += 1
        else:
            # Events without a class stay unfiled, unless the feed names no classes at all.
            cid = None if e.unfiled else course_for(e.course) if (e.course or not any_course) else None
            events.append({
                "id": _id(item.uid, item.rid), "title": item.summary[:500] or "Event", "start_at": _iso(item.start),
                "end_at": _iso(item.end), "course_id": cid, "location": item.location[:300] or None,
                "html_url": e.html_url, "all_day": item.all_day,
                "all_day_date": item.first_day.isoformat() if item.all_day and item.first_day else None,
            })
    for c in courses.values():
        c["assignments"].sort(key=lambda a: (a["due_at"] or "", a["id"]))
    events.sort(key=lambda ev: (ev["start_at"] or "", ev["id"]))
    snapshot = {
        "schema_version": 1, "lms": "ics", "base_url": f"https://[{feed.host}]" if ":" in feed.host else f"https://{feed.host}",
        # Its own identity per link, so it never merges with a future direct sync of the same school.
        "user": {"id": f"feed-{feed.id}", "name": None},
        "synced_at": _iso(now),
        "courses": sorted(courses.values(), key=lambda c: (c["name"].lower(), c["id"])),
        "calendar_events": events, "errors": [], "restricted": [],
    }
    counts = {"due": due_count, "events": len(events), "courses": len(courses),
              "fallback_course": any(c["name"] == fallback_name for c in courses.values())}
    return snapshot, counts


# ---------------------------------------------------------------- refreshing


def _user_zone(user: User) -> ZoneInfo:
    return _zone(user.timezone) or ZoneInfo("UTC")


def _account(feed: CalendarFeed) -> CanvasAccount | None:
    account = db.session.get(CanvasAccount, feed.account_id) if feed.account_id else None
    return account if account is not None and account.user_id == feed.user_id else None


def _send_etag(feed: CalendarFeed, account: CanvasAccount | None, now: datetime) -> str | None:
    """Ask "changed since?" only within a day of the last full import: repeats roll forward and
    due dates pass even when the file doesn't change."""
    if not feed.etag or account is None or not account.last_snapshot_id:
        return None
    run = db.session.get(SyncRun, account.last_snapshot_id)
    return feed.etag if run is not None and now - run.received_at < FULL_REFRESH_AFTER else None


def _mark_passed(account: CanvasAccount, now: datetime) -> None:
    """The file didn't change, but time did: due dates that passed are no longer upcoming."""
    course_ids = select(Course.id).where(Course.account_id == account.id)
    db.session.execute(update(Assignment).where(Assignment.course_id.in_(course_ids), Assignment.status == "upcoming",
                                                Assignment.due_at.is_not(None), Assignment.due_at <= now)
                       .values(status="past_due"))


def refresh(feed: CalendarFeed, force: bool = False) -> dict | None:
    """Fetch one link now and bring its classes up to date. Returns counts, or None when it failed
    (feed.last_error says why, in words for the student). Never raises."""
    from . import gcal, ingest, integrations

    feed_id, host = feed.id, feed.host
    now = utcnow()
    try:
        user = db.session.get(User, feed.user_id)
        account = _account(feed)
        # An attempt counts as a check, so a broken link isn't retried on every page view.
        feed.last_fetched_at = now
        db.session.commit()
        got = fetch(feed, None if force else _send_etag(feed, account, now))
        if got.status == 304 and account is not None:
            account.last_sync_at = now
            _mark_passed(account, now)
            feed.last_error = None
            db.session.commit()
            return {"unchanged": True, "due": None, "events": None, "courses": None}
        parsed = parse(got.body, _user_zone(user), now)
        if feed.lms == "other":
            feed.lms = lms_from_calendar(parsed.prodid, [i.uid for i in parsed.items]) or "other"
        snapshot, counts = build_snapshot(feed, parsed, _user_zone(user), now)
        run, _needed = ingest.ingest_snapshot(user, snapshot, [])
        feed = db.session.get(CalendarFeed, feed_id)
        account = db.session.get(CanvasAccount, run.account_id)
        account.lms = "ics"
        feed.account_id = account.id
        feed.etag = got.etag
        feed.event_count = counts["due"] + counts["events"]
        feed.last_error = None
        # What the calendar is made of, for building better rules (counts and property names, no text).
        run.stats = {**(run.stats or {}), "calendar_link": feed_id, "lms": feed.lms,
                     "shape": {**parsed.shape, **counts}}
        db.session.commit()
        unchanged = bool((run.stats or {}).get("unchanged"))
    except FeedError as exc:
        message = str(exc)
    except Exception as exc:  # never the link in the log: nothing below this point was given it
        current_app.logger.exception("calendar link %s on %s failed: %s", feed_id, host, type(exc).__name__)
        message = "Something went wrong reading this calendar. We'll try again later."
    else:
        try:
            if integrations.available() and gcal.enabled(user) and (not unchanged or gcal.due_for_refresh(user)):
                gcal.kick(user.id)  # new or changed due dates go to Google Calendar
        except Exception as exc:  # the import worked; Google Calendar records its own errors
            db.session.rollback()
            current_app.logger.error("google calendar after calendar link %s failed: %s", feed_id, type(exc).__name__)
        return {**counts, "unchanged": unchanged}
    db.session.rollback()
    row = db.session.get(CalendarFeed, feed_id)
    if row is not None:
        row.last_error = message[:500]
        db.session.commit()
    return None


def refresh_due(user) -> bool:
    """From the pages where a student looks at their work: refresh links older than an hour, in the
    background (inline in tests). Never raises, so a calendar problem can't break a page."""
    try:
        cutoff = utcnow() - STALE_AFTER
        due = db.session.scalar(select(CalendarFeed.id).where(
            CalendarFeed.user_id == user.id,
            or_(CalendarFeed.last_fetched_at.is_(None), CalendarFeed.last_fetched_at < cutoff)).limit(1))
        if due is None:
            return False
        kick(user.id)
        return True
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("calendar link refresh for user %s failed: %s", getattr(user, "id", None),
                                 type(exc).__name__)
        return False


def kick(user_id: int) -> None:
    """Refresh a student's stale links; one run per student at a time (gcal.kick's pattern)."""
    app = current_app._get_current_object()
    if app.config.get("EXTRACT_INLINE"):  # tests: run inline
        _run(user_id)
        return
    with _lock:
        if user_id in _running:
            return
        _running.add(user_id)

    def work():
        with app.app_context():
            try:
                _run(user_id)
            except Exception as exc:  # pragma: no cover - refresh() already records its own errors
                app.logger.error("calendar link refresh for user %s failed: %s", user_id, type(exc).__name__)
            finally:
                db.session.remove()
                with _lock:
                    _running.discard(user_id)

    threading.Thread(target=work, name="calendar-links", daemon=True).start()


def _run(user_id: int) -> None:
    for feed_id in db.session.scalars(select(CalendarFeed.id).where(CalendarFeed.user_id == user_id)
                                      .order_by(CalendarFeed.id)).all():
        now = utcnow()
        # Claim it: only one refresh per link even with several workers or tabs.
        claimed = db.session.execute(update(CalendarFeed).where(
            CalendarFeed.id == feed_id,
            or_(CalendarFeed.last_fetched_at.is_(None), CalendarFeed.last_fetched_at < now - STALE_AFTER))
            .values(last_fetched_at=now)).rowcount
        db.session.commit()
        if claimed:
            feed = db.session.get(CalendarFeed, feed_id)
            if feed is not None:
                refresh(feed)


def remove(feed: CalendarFeed) -> None:
    """Delete a link and everything it brought in: its account's classes, due dates and events go with
    the account (ON DELETE CASCADE). Google Calendar then drops the events of the removed due dates."""
    from . import gcal, integrations

    user = db.session.get(User, feed.user_id)
    account = _account(feed)
    if account is not None and account.lms == "ics":
        db.session.delete(account)
    db.session.delete(feed)
    db.session.commit()
    if user is not None and integrations.available() and gcal.enabled(user):
        gcal.kick(user.id)


# ---------------------------------------------------------------- for pages


def feed_for(account: CanvasAccount) -> CalendarFeed | None:
    if getattr(account, "lms", "canvas") != "ics":
        return None
    return db.session.scalar(select(CalendarFeed).where(CalendarFeed.account_id == account.id))


def source_name(account: CanvasAccount | None) -> str:
    """The LMS an account's data comes from: "Canvas", "Brightspace"... or "your school's site"."""
    if account is None or getattr(account, "lms", "canvas") != "ics":
        return "Canvas"
    feed = feed_for(account)
    return _where(feed.lms if feed else None)


def source_names(user) -> str:
    """What a student's classes sync from, for copy like "Found in Canvas": "Canvas", "Brightspace",
    "Canvas and Moodle"; "Canvas" when nothing is connected yet (the default product copy)."""
    if user is None or not getattr(user, "is_authenticated", True) or getattr(user, "id", None) is None:
        return "Canvas"
    names = []
    for account in db.session.scalars(select(CanvasAccount).where(CanvasAccount.user_id == user.id)
                                      .order_by(CanvasAccount.id)):
        name = source_name(account) if account.lms == "ics" else "Canvas"
        name = "your calendar link" if name == _where(None) else name
        if name not in names:
            names.append(name)
    if not names:
        return "Canvas"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def source_label(account: CanvasAccount) -> str:
    """Where a page says an account syncs from: the Canvas host, or "from your Brightspace calendar link"."""
    if getattr(account, "lms", "canvas") != "ics":
        return account.host
    feed = feed_for(account)
    name = lms_name(feed.lms if feed else None)
    return f"from your {name} calendar link" if name else f"from your calendar link ({account.host})"
