"""Chat moderation (class rooms and direct messages): masks profanity, blocks slurs and threats, rate-limits spam."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select

from ..extensions import db
from ..models import ChatMessage, utcnow

MAX_LENGTH = 1000
RATE_WINDOW = timedelta(seconds=15)
RATE_LIMIT = 6

# Masked (the message still posts).
# Words with innocent classroom uses ("Philip K. Dick", "damn" in a quoted text) are left alone.
_PROFANITY = ["fuck", "fucking", "shit", "bitch", "bastard", "pussy", "cunt", "asshole", "motherfucker", "bullshit",
              "slut", "whore"]
# Blocked outright: hate slurs and violent threats have no place in a class room.
# (Terms with common academic meanings — "retarded potential", "chink in the armor" — are not listed.)
_BLOCKED = [r"n[i1!]gg(?:er|a)", r"\bf[a@]gg?(?:ot)?s?\b", r"\bk[i1]ke\b", r"\bsp[i1]c\b", r"\btr[a@]nny\b",
            r"\bkill (?:yo)?urself\b", r"\bkys\b", r"\bi(?:'ll| will) (?:kill|shoot|stab) (?:you|u)\b"]

_profanity_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in _PROFANITY) + r")(?:s|ed|ing|er)?\b", re.I)
_blocked_re = re.compile("|".join(_BLOCKED), re.I)


# Ways to take a conversation off the site. Class rooms mix ages, so these are refused there, in
# messages involving anyone under 18, and in display names. Best effort: we block common forms, tuned
# so ordinary class talk (dates, decimals, problem lists, data, code, @everyone, "ig" for "I guess",
# Zoom links, a professor's .edu address) passes. Every quantifier is bounded and patterns start only
# at word or token boundaries, so a long input can't make a check slow.
_ZERO_WIDTH = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
_APPS = r"snap(?:chat)?|sc|insta(?:gram)?|ig|kik|whats ?app|we ?chat|venmo|cash ?app|discord|tele(?:gram)?|tik ?tok|fb"
_WEAK = r"signal|facebook|threads|twitter|groupme|dc|gram"  # everyday words too: only with a handle-looking name
_COLON_APPS = (r"snap(?:chat)?|insta(?:gram)?|kik|whats ?app|we ?chat|venmo|cash ?app|discord|tele(?:gram)?|tik ?tok|fb|"
               r"signal|facebook|threads|twitter|groupme")
_MAILS = r"gmail|yahoo|hotmail|outlook|icloud|proton(?:mail)?|aol|msn"
# Looks like a handle: starts with a letter or _, and has an _, two digits in a row, or a dot inside it.
_HANDLE = r"(?=[a-z0-9_.]{0,30}(?:_|\d\d|[a-z0-9]\.[a-z0-9]))[a-z_][a-z0-9_.]{2,29}"
_STRICT_HANDLE = r"(?=[a-z0-9_.]{0,30}(?:_|[a-z0-9]\.[a-z0-9]))[a-z_][a-z0-9_.]{2,29}"
_WORD = r"[a-z_][a-z0-9_.]{2,29}"
_AT_NAME = rf"@(?!(?:everyone|here|channel|all)\b){_WORD}"  # an explicit @name right after an app counts
_SEP = "[\\s:=@,>\\-\u2013\u2014\u2192\U0001F449]{1,5}"   # "snap - x", "snap: x", "snap -> x", "snap \U0001F449 x"
_NOT_A_NAME = (r"(?:down|broken|dead|bugged|glitching|weird|private|public|off|on|gone|deleted|banned|hacked|lagging|"
               r"full|empty|new|old|trash|fine|good|bad|open|closed|here|there|the|not|too|same|free|working|blocked)\b")
_END = r"(?=\s{0,3}(?:$|[,.!?;)\n]))"
_TLDS = r"com|net|org|io|co|us|me|gov|uk|ca|ai|app|dev|info|biz|xyz|live|email"
_CONTACT = re.compile(
    # snap mal_1985 · sc: mal_1985 · hmu on ig x_y · snap me malcolm_x · snapchat username is mal.x · insta @malcolm
    rf"\b(?:{_APPS}|{_WEAK})\b(?:\s{{1,3}}me)?(?:\s{{0,3}}(?:name|handle|user(?:name)?|tag|id))?(?:\s{{1,3}}is|'s|\s{{0,3}}=)?"
    rf"{_SEP}(?:@?{_HANDLE}|{_AT_NAME})"
    # discord mal#1985
    rf"|\b(?:discord|dc)\b{_SEP}[a-z_][\w.]{{1,31}}#\d{{4}}\b"
    # my telegram is malcolmx · my insta's malcolm · my gmail is jake2004
    rf"|\bmy\s{{1,3}}(?:{_APPS}|{_MAILS}|e-?mail)(?:\s{{0,3}}(?:name|handle|user(?:name)?|tag|id))?(?:\s{{1,3}}is|'s|\s{{0,3}}[:=])"
    rf"\s{{0,3}}@?(?!{_NOT_A_NAME}){_WORD}{_END}"
    # telegram: malcolmx
    rf"|\b(?:{_COLON_APPS})\s{{0,3}}:\s{{0,3}}@?(?!{_NOT_A_NAME}){_WORD}{_END}"
    # mal_1985 on snap
    rf"|(?<![\w.]){_STRICT_HANDLE}\s{{1,3}}on\s{{1,3}}(?:{_APPS}|{_WEAK})\b"
    # 👻 mal1985 (not "happy halloween 👻 boo")
    rf"|\U0001F47B\s{{0,3}}[:\-]?\s{{0,3}}@?{_HANDLE}"
    # @mal_1985 (not @everyone, @Sarah or @10am)
    rf"|(?<![\w@.])@{_HANDLE}"
    # emails, spaces around the @ or dot too, except school (.edu) addresses and git@host: remotes
    rf"|(?<![\w.+-])[\w.+-]{{1,64}}\s{{0,3}}@\s{{0,3}}[\w-]{{1,63}}(?:\.[\w-]{{1,63}}){{0,3}}\s{{0,3}}\.\s{{0,3}}(?:{_TLDS})\b(?![(:\w])"
    # jake2004 at gmail · mal at gmail dot com · mal (at) yahoo (dot) com
    rf"|(?<![\w.+-])[\w.+-]{{3,64}}\s{{1,3}}(?:at|@)\s{{1,3}}(?:{_MAILS})\b"
    r"|(?<![\w.+-])[\w.+-]{1,64}\s{0,3}(?:\(at\)|\[at\]|\sat\s)\s{0,3}[\w-]{1,63}\s{0,3}(?:\(dot\)|\[dot\]|\sdot\s)\s{0,3}(?:com|net|org|io|me)\b"
    # text me 555-1234 (a 7-digit local number after a phone word)
    r"|\b(?:call|text|txt|phone|cell|number|num|hmu|whats ?app)\b(?:\s{1,3}(?:me|at|is|my|on|it's))*\s{0,3}[:#]?\s{0,3}"
    r"(?<![\d.])\d{3}[\s-]?\d{4}(?![\d.])"
    # +44 7700 900123
    r"|(?<![\w+])\+\d{1,3}[\s.-]?\d(?:[\s.-]?\d){7,12}(?!\d)"
    # invite and profile links
    r"|(?<![\w.])(?:https?://)?(?:www\.)?(?:discord\s{0,2}\.\s{0,2}gg\s{0,2}/|discord(?:app)?\.com/(?:invite|users)/|t\.me/|"
    r"telegram\.me/|signal\.me/|wa\.me/|chat\.whatsapp\.com/|snapchat\.com/(?:add|t)/|instagram\.com/|instagr\.am/|ig\.me/|"
    r"m\.me/|tiktok\.com/@|vm\.tiktok\.com/|kik\.me/|groupme\.com/join_group|linktr\.ee/)",
    re.I)

# Phone numbers however they're spaced: digits split by up to 3 of these characters at a time.
_DIGIT_RUN = re.compile(r"\d(?:[\s().\-/_]{0,3}\d)*")
_TEN = {(10,), (3, 7), (3, 3, 4), (6, 4), (3, 3, 2, 2)}
_ELEVEN = {(11,), (1, 10), (1, 3, 7), (1, 3, 3, 4)}


def _phone_shape(groups: list[str]) -> bool:
    """A North American number in a phone's grouping (410 555 1234, 4105551234, 1-410-555-1234, or
    one digit at a time), not a Zoom ID, ISBN, decimal or list of problem numbers."""
    sizes, digits = tuple(len(g) for g in groups), "".join(groups)
    ones = all(n == 1 for n in sizes)
    if sizes in _ELEVEN or (ones and len(sizes) == 11):
        if digits[0] != "1":
            return False
        digits = digits[1:]
    elif not (sizes in _TEN or (ones and len(sizes) == 10)):
        return False
    if sizes[-3:] == (3, 3, 4):  # written like a phone number: any real-looking area code will do
        return digits[0] in "23456789"
    return digits[0] in "23456789" and digits[3] in "23456789"


def _has_phone(text: str) -> bool:
    for run in _DIGIT_RUN.finditer(text):
        groups = re.findall(r"\d+", run.group())
        if sum(len(g) for g in groups) < 10:
            continue
        for i in range(len(groups)):
            for j in range(i + 1, min(len(groups), i + 11) + 1):
                if _phone_shape(groups[i:j]):
                    return True
    return False


def has_contact(text: str) -> bool:
    text = _ZERO_WIDTH.sub("", (text or "")[:4000])
    return bool(_CONTACT.search(text) or _has_phone(text))


class Rejected(ValueError):
    pass


def check_contact(body: str) -> None:
    """Refuse messages carrying contact details or invite links (class rooms, and DMs with a minor)."""
    if has_contact(body):
        raise Rejected("For everyone's safety, phone numbers, emails, social handles and invite links "
                       "can't be shared here.")


def clean(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise Rejected("Message is empty.")
    if len(body) > MAX_LENGTH:
        raise Rejected(f"Keep messages under {MAX_LENGTH} characters.")
    if _blocked_re.search(body):
        raise Rejected("That message breaks the community rules and wasn't sent.")
    return _profanity_re.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), body)


def check_rate(user_id: int) -> None:
    """Class-chat messages and direct messages share one budget."""
    from ..models import DirectMessage

    since = utcnow() - RATE_WINDOW
    recent = (db.session.scalar(select(func.count(ChatMessage.id)).where(
        ChatMessage.user_id == user_id, ChatMessage.created_at >= since)) or 0) + (db.session.scalar(
        select(func.count(DirectMessage.id)).where(DirectMessage.sender_id == user_id, DirectMessage.created_at >= since)) or 0)
    if recent >= RATE_LIMIT:
        raise Rejected("Slow down a little — you're sending messages too fast.")


def clean_name(name: str) -> str:
    """Display names and live-quiz nicknames are shown to classmates: no slurs, threats or contact details.
    Longer names are cut to 80 characters first."""
    name = (name or "").strip()[:80].strip()
    if _blocked_re.search(name) or _profanity_re.search(name):
        raise Rejected("Pick a different name.")
    if name_has_contact(name):
        raise Rejected("Names can't include phone numbers, emails or social handles.")
    return name


_NAME_APPS = r"snap(?:chat)?|sc|insta(?:gram)?|ig|kik|whats ?app|we ?chat|discord|tele(?:gram)?|tik ?tok|venmo|cash ?app|fb"


def name_has_contact(name: str) -> bool:
    """Contact details in a name, judged more strictly than messages since a name shows on every post:
    7+ digits however they're split, or an app name next to anything (snap_mal1985, ig.mal, Mal SC)."""
    name = _ZERO_WIDTH.sub("", (name or "")[:200])
    if has_contact(name) or has_contact(re.sub(r"\s{0,3}([@.])\s{0,3}", r"\1", name)):
        return True
    if re.search(r"\d{7,}", re.sub(r"[\s._\-()/]+", "", name)):
        return True
    words = re.findall(r"[a-z0-9]+", name, re.I)
    apps = [w for w in words if re.fullmatch(_NAME_APPS, w, re.I)]
    return bool(apps) and any(len(w) >= 3 for w in words if w not in apps)
