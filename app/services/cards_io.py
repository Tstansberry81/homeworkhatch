"""Flashcards in and out: paste from Quizlet or Anki (or a spreadsheet), download as CSV or Anki.

Everything here is plain text processing: no network, no AI. The student pastes what Quizlet's
"Export -> Copy text" or Anki's "Notes in Plain Text" gives them; we never fetch a Quizlet URL.

Formats `parse` recognizes (auto-detected unless separators are given):

* Anki "Notes in Plain Text": `#separator:tab|comma|semicolon|pipe|...`, `#html:true`,
  `#tags column:N` (and deck / notetype / guid columns) headers; HTML is turned into text; a
  cloze note (`{{c1::answer::hint}}`) becomes one card per cloze number.
* CSV with quoted fields (or a "term,definition" / "front,back" header row).
* Quizlet "Copy text": term and definition split by tab (default), comma or a custom string;
  cards split by new lines (default), semicolons or a custom string. With new lines between
  cards, a line with no separator continues the previous card's definition (Quizlet writes
  multi-line definitions that way).
* The original one-per-line `front :: back` (tab works there too).

A front that is a cloze note becomes cloze cards in every format (the back is its "extra").
At most MAX_CARDS cards are kept; past that, cards are only counted (`Parsed.dropped`).
"""

from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass, field

import nh3

MAX_CARDS = 2000
MAX_FRONT = 2000
MAX_BACK = 4000
# One Anki field (HTML and all) is cut here before it's read; cards end up trimmed to
# MAX_FRONT / MAX_BACK anyway. A cloze note is cut to MAX_BACK before it's expanded.
FIELD_MAX = 20_000

TERM_SEPS = {"tab": "\t", "comma": ",", "semicolon": ";", "dash": " - "}
CARD_SEPS = {"newline": "\n", "semicolon": ";"}
ANKI_SEPS = {"tab": "\t", "comma": ",", "semicolon": ";", "pipe": "|", "space": " ", "colon": ":"}
HEADER_ROWS = {("term", "definition"), ("front", "back"), ("question", "answer"), ("word", "definition"),
               ("word", "meaning"), ("term", "meaning")}

DETECTED_LABELS = {
    "anki": "Anki notes",
    "csv": "CSV (spreadsheet)",
    "quizlet": "Quizlet export",
    "lines": "One card per line",
    "empty": "Nothing pasted yet",
    "unknown": "Couldn't find any cards",
}

# Every pattern here runs on text anyone can paste (the public /free-learn preview included),
# so each one is linear: no nested or overlapping repeats, and a repeat that may not find its
# end stops at the next "<" / "{{" / "[" instead of rescanning the rest of the text.
_ANKI_HEADER = re.compile(r"^#(separator|html|tags column|columns|notetype|deck|guid column|notetype column|"
                          r"deck column|if matches|tags)\s*:", re.I)
# {{c1::answer}} or {{c1::answer::hint}}. Both parts stop at "{{" and are possessive, so a
# cloze that never closes costs one scan up to the next "{{" instead of backtracking.
CLOZE = re.compile(r"\{\{c(\d{1,4}+)::((?:(?!::|\}\}|\{\{).)*+)(?:::((?:(?!\}\}|\{\{).)*+))?\}\}", re.S)
_LOOKS_HTML = re.compile(r"<(br|div|p|span|b|i|u|em|strong|img|sub|sup|font|ul|ol|li)\b[^<>]*>|&nbsp;|&amp;|&lt;",
                         re.I)
_DIGITS = re.compile(r"[0-9]{1,9}")


def _sep(value, names: dict) -> str | None:
    """A separator choice from a form: a name ("tab"), "auto"/None, or a custom string.
    Custom strings may spell tabs and new lines as \\t and \\n."""
    if value is None:
        return None
    value = str(value)
    if value.strip().lower() in ("", "auto"):
        return None
    if value.strip().lower() in names:
        return names[value.strip().lower()]
    return value.replace("\\t", "\t").replace("\\n", "\n")


def _clip(s: str, n: int = 120) -> str:
    s = " ".join(s[: n * 4].split())  # only a clipped copy is shown: don't tidy all of a huge line
    return s if len(s) <= n else s[: n - 1] + "…"


def _tidy(back: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", back.strip())


def _outside_cloze(line: str) -> str:
    """The line with every cloze blanked out (same length), to find separators outside them:
    `The {{c1::cat}} sat :: el gato` splits at the second "::", not inside the cloze."""
    if "{{" not in line:
        return line
    return CLOZE.sub(lambda m: "\x00" * (m.end() - m.start()), line)


# ---------------------------------------------------------------- detection


def _first_row(text: str) -> list[str]:
    for line in text.split("\n"):
        if line.strip():
            try:
                return next(csv.reader([line]))
            except (csv.Error, ValueError, StopIteration):
                return []
    return []


def _is_header(front: str, back: str) -> bool:
    return (front.strip().lower(), back.strip().lower()) in HEADER_ROWS


def _looks_csv(text: str) -> bool:
    row = _first_row(text)
    if len(row) >= 2 and _is_header(row[0], row[1]):
        return True
    return bool(re.search(r'(^|,)[ ]*"[^"\n]*"[ ]*(,|$)', text, re.M)) and "\t" not in text


def _looks_anki(text: str) -> bool:
    for line in text.split("\n"):
        if not line.strip():
            continue
        if _ANKI_HEADER.match(line):
            return True
        break
    return False


def _auto_card_sep(text: str, term_sep: str) -> str:
    """New lines unless splitting on semicolons finds more cards (Quizlet's ";" option)."""
    if ";" not in text or term_sep == ";":
        return "\n"
    by_line = sum(1 for line in text.split("\n") if term_sep in line)
    by_semi = sum(1 for chunk in text.split(";") if term_sep in chunk)
    return ";" if by_semi > by_line else "\n"


# ---------------------------------------------------------------- what the parsers found


@dataclass
class Parsed:
    cards: list[tuple[str, str]]
    skipped: list[str]  # lines we couldn't use, and notes about trimmed cards
    detected: str
    dropped: int = 0  # cards past the MAX_CARDS limit, left out

    @property
    def total(self) -> int:
        return len(self.cards) + self.dropped


def limit_note(parsed: Parsed, verb: str = "were imported") -> str:
    """"Only the first 2,000 of 5,000 cards were imported." when the limit cut the set short."""
    if not parsed.dropped:
        return ""
    return f"Only the first {len(parsed.cards):,} of {parsed.total:,} cards {verb}."


@dataclass
class _Out:
    """Cards as a parser finds them: tidied, checked, cloze notes expanded, long ones trimmed,
    and at most MAX_CARDS of them. Past the limit nothing more is built: cards are only counted."""

    cards: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dropped: int = 0
    seen: int = 0  # rows offered as cards (the first may be a "term,definition" header)

    @property
    def full(self) -> bool:
        return len(self.cards) >= MAX_CARDS

    def skip(self, line: str) -> None:
        if not self.full and line and line.strip():
            self.skipped.append(line)

    def add(self, front: str, back: str) -> None:
        self.seen += 1
        if "{{" in front and CLOZE.search(front):
            self._cloze(front, back)
            return
        front, back = front.strip(), _tidy(back)
        if not front or not back:
            self.skip(front or back)
            return
        if self.seen == 1 and _is_header(front, back):
            return  # a header row, not a card
        self._keep(front, back)

    def _keep(self, front: str, back: str, note: bool = True) -> None:
        if self.full:
            self.dropped += 1
            return
        if len(front) > MAX_FRONT or len(back) > MAX_BACK:
            if note:
                self.skipped.append(f"Trimmed a very long card: {_clip(front, 60)}")
            front, back = front[:MAX_FRONT].rstrip(), back[:MAX_BACK].rstrip()
        self.cards.append((front, back))

    def _cloze(self, text: str, extra: str) -> None:
        if len(text) > MAX_BACK:  # cut before expanding: each cloze number copies the whole note
            if not self.full:
                self.skipped.append(f"Trimmed a very long card: {_clip(text, 60)}")
            text = text[:MAX_BACK]
        numbers = sorted({int(m.group(1)) for m in CLOZE.finditer(text)})
        room = max(0, MAX_CARDS - len(self.cards))
        self.dropped += max(0, len(numbers) - room)
        if not room:
            return
        for front, back in cloze_cards(text, extra, numbers[:room]):
            front, back = front.strip(), _tidy(back)
            if front and back:
                self._keep(front, back, note=False)


# ---------------------------------------------------------------- parsers


def _quizlet(out: _Out, text: str, term_sep: str, card_sep: str) -> None:
    if card_sep == "\n":
        current: tuple[str, list[str]] | None = None  # (term, the definition's lines)

        def finish(card: tuple[str, list[str]]) -> None:
            front, lines = card
            back = "\n".join(lines)  # joined once, not re-copied for every continuation line
            if back.strip():
                out.add(front, back)
            else:
                out.skip(front)

        for line in text.split("\n"):
            front, sep, back = line.partition(term_sep)
            if sep and front.strip():
                if current is not None:
                    finish(current)
                current = (front, [back])
            elif current is not None:
                current[1].append(line)  # a multi-line definition goes on
            elif line.strip():
                out.skip(line)
        if current is not None:
            finish(current)
        return
    for chunk in text.split(card_sep):
        if not chunk.strip():
            continue
        front, sep, back = chunk.partition(term_sep)
        if sep and front.strip() and back.strip():
            out.add(front, back)
        else:
            out.skip(chunk)


def _lines(out: _Out, text: str) -> None:
    """The original format: one card per line, `front :: back` or tab-separated. A cloze note
    (alone on its line, or as the front) becomes cloze cards."""
    for line in text.split("\n"):
        plain = _outside_cloze(line)
        if "\t" in plain:
            at, width = plain.index("\t"), 1
        elif "::" in plain:
            at, width = plain.index("::"), 2
        elif plain != line:  # a cloze note with no back
            out.add(line, "")
            continue
        else:
            out.skip(line)
            continue
        front, back = line[:at], line[at + width:]
        has_cloze = plain[:at] != front
        if front.strip() and (back.strip() or has_cloze):
            out.add(front, back)
        else:
            out.skip(line)


def _csv(out: _Out, text: str, delimiter: str = ",") -> None:
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except (csv.Error, ValueError, TypeError):
        _quizlet(out, text, delimiter, "\n")
        return
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            out.add(row[0], row[1])
        else:
            out.skip(delimiter.join(row))


def html_to_text(value: str) -> str:
    """Anki fields with HTML -> plain text (line breaks kept, tags, images and sounds dropped)."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", value)
    s = re.sub(r"(?i)</(div|p|li|tr|h[1-6])\s*>", "\n", s)
    s = re.sub(r"(?i)<li[^<>]*>", "- ", s)
    s = re.sub(r"\[sound:[^\[\]]*\]", "", s)
    s = html.unescape(nh3.clean(s, tags=set()))
    s = s.replace("\xa0", " ")
    s = "\n".join(line.rstrip(" \t") for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def cloze_cards(text: str, extra: str = "", numbers: list[int] | None = None) -> list[list[str]]:
    """`{{c1::Paris}} is the capital of {{c2::France::country}}` -> one card per cloze number
    (only `numbers`, when given). The front shows that number's blanks as [...] (or [hint]);
    the back is the whole sentence."""
    if numbers is None:
        numbers = sorted({int(m.group(1)) for m in CLOZE.finditer(text)})
    full = CLOZE.sub(lambda m: m.group(2), text).strip()
    back = full + (f"\n\n{extra.strip()}" if extra and extra.strip() else "")
    cards = []
    for n in numbers:
        front = CLOZE.sub(lambda m: (f"[{m.group(3)}]" if m.group(3) else "[...]") if int(m.group(1)) == n
                          else m.group(2), text).strip()
        cards.append([front, back])
    return cards


def _anki(out: _Out, text: str) -> None:
    headers: dict[str, str] = {}
    lines = text.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.startswith("#") and ":" in line:
            key, _, value = line[1:].partition(":")
            headers[key.strip().lower()] = value.strip()
            start = i + 1
            continue
        break
    body = "\n".join(lines[start:])
    raw_sep = headers.get("separator", "")
    sep = ANKI_SEPS.get(raw_sep.lower(), raw_sep if len(raw_sep) == 1 else "\t")
    if "html" in headers:
        is_html = headers["html"].lower() == "true"
    else:
        is_html = bool(_LOOKS_HTML.search(body))
    excluded = {int(headers[key]) for key in ("tags column", "deck column", "notetype column", "guid column")
                if _DIGITS.fullmatch(headers.get(key, ""))}
    try:
        rows = list(csv.reader(io.StringIO(body), delimiter=sep))
    except (csv.Error, ValueError, TypeError):  # e.g. #separator:" (csv can't split on its quote)
        rows = [line.split(sep) for line in body.split("\n")]
    for row in rows:
        fields = [cell[:FIELD_MAX] for idx, cell in enumerate(row, 1) if idx not in excluded]
        if is_html and not out.full:  # past the limit, cards are only counted
            fields = [html_to_text(f) for f in fields]
        if not any(f.strip() for f in fields):
            continue
        if "{{" in fields[0] and CLOZE.search(fields[0]):
            out.add(fields[0], fields[1] if len(fields) > 1 else "")
        elif len(fields) >= 2 and fields[0].strip() and fields[1].strip():
            out.add(fields[0], fields[1])
        else:
            out.skip(sep.join(row))


# ---------------------------------------------------------------- entry point


def parse(text: str | None, term_sep: str | None = None, card_sep: str | None = None) -> Parsed:
    """Pasted text -> the cards as (front, back), the lines we couldn't use, the detected format,
    and how many cards the MAX_CARDS limit left out.

    `term_sep` / `card_sep` are "auto" (or None), a name ("tab", "comma", "newline",
    "semicolon") or a custom string; giving either one means Quizlet-style parsing."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        return Parsed([], [], "empty")
    out = _Out()
    ts, cs = _sep(term_sep, TERM_SEPS), _sep(card_sep, CARD_SEPS)
    if ts or cs:
        if not ts:
            ts = "\t" if "\t" in text else ","
        _quizlet(out, text, ts, cs or _auto_card_sep(text, ts))
        detected = "quizlet"
    elif _looks_anki(text):
        _anki(out, text)
        detected = "anki"
    elif len(row := _first_row(text)) >= 2 and _is_header(row[0], row[1]):
        _csv(out, text)
        detected = "csv"
    else:
        lines = [line for line in text.split("\n") if line.strip()]
        plain = [_outside_cloze(line) for line in lines]
        cloze_lines = sum(1 for line, bare in zip(lines, plain) if bare != line)
        tab_lines = sum(1 for bare in plain if "\t" in bare)
        colon_lines = sum(1 for bare in plain if "::" in bare and "\t" not in bare)
        if cloze_lines * 2 > len(lines) and not tab_lines and not colon_lines:
            _anki(out, text)  # cloze notes, one per line
            detected = "anki"
        elif tab_lines > colon_lines:
            _quizlet(out, text, "\t", _auto_card_sep(text, "\t"))
            detected = "quizlet"
        elif colon_lines:
            _lines(out, text)
            detected = "lines"
        elif _looks_csv(text):
            _csv(out, text)
            detected = "csv"
        elif "," in text:
            _quizlet(out, text, ",", _auto_card_sep(text, ","))
            detected = "quizlet"
        else:
            return Parsed([], [_clip(line) for line in lines], "unknown")
    if not out.cards and not out.seen:
        detected = "unknown"
    return Parsed(out.cards, [_clip(s) for s in out.skipped if s and s.strip()], detected, out.dropped)


def parse_cards(text: str | None, term_sep: str | None = None,
                card_sep: str | None = None) -> tuple[list[tuple[str, str]], list[str], str]:
    """`parse` as (cards, lines we couldn't use, detected format)."""
    parsed = parse(text, term_sep, card_sep)
    return parsed.cards, parsed.skipped, parsed.detected


def separator_choice(src, name: str) -> str | None:
    """A form's separator select ("auto", "tab", ..., "custom") plus its `<name>_custom` box."""
    value = str(src.get(name) or "auto")
    if value == "custom":
        return str(src.get(f"{name}_custom") or "") or None
    return value


def preview(src, show: int = 20) -> dict:
    """What the import box shows while typing: a count, the first cards, the skipped lines, and
    (for a set over the limit) how many cards would be left out."""
    parsed = parse(str(src.get("text") or ""), separator_choice(src, "term_sep"), separator_choice(src, "card_sep"))
    return {"count": len(parsed.cards), "cards": [{"front": f, "back": b} for f, b in parsed.cards[:show]],
            "skipped": parsed.skipped[:20], "skipped_count": len(parsed.skipped), "detected": parsed.detected,
            "label": DETECTED_LABELS.get(parsed.detected, parsed.detected),
            "dropped": parsed.dropped, "truncated": parsed.dropped > 0, "total": parsed.total,
            "note": limit_note(parsed, "will be imported")}


# ---------------------------------------------------------------- export


def _pairs(cards) -> list[tuple[str, str]]:
    out = []
    for c in cards:
        if isinstance(c, (tuple, list)):
            out.append((str(c[0]), str(c[1])))
        else:
            out.append((c.front or "", c.back or ""))
    return out


def export_csv(cards) -> str:
    """A spreadsheet: "front,back" header, then one quoted-as-needed row per card."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["front", "back"])
    for front, back in _pairs(cards):
        writer.writerow([front, back])
    return buf.getvalue()


def _anki_field(value: str) -> str:
    # Anki reads CSV-style quoting: a field with a tab, new line or quote is wrapped in quotes
    # (quotes doubled). A field starting with # is quoted too, or Anki takes the line as a comment.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(ch in value for ch in '\t\n"') or value.startswith("#"):
        return '"' + value.replace('"', '""') + '"'
    return value


def export_anki(cards) -> str:
    """Anki "Notes in Plain Text": File -> Import in Anki makes a Basic note per card."""
    lines = ["#separator:tab", "#html:false", "#columns:Front\tBack"]
    lines += [f"{_anki_field(front)}\t{_anki_field(back)}" for front, back in _pairs(cards)]
    return "\n".join(lines) + "\n"
