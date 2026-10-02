"""Flashcards in and out: paste from Quizlet or Anki (or a spreadsheet), download as CSV or Anki.

Everything here is plain text processing: no network, no AI. The student pastes what Quizlet's
"Export -> Copy text" or Anki's "Notes in Plain Text" gives them; we never fetch a Quizlet URL.

Formats `parse_cards` recognizes (auto-detected unless separators are given):

* Anki "Notes in Plain Text": `#separator:tab|comma|semicolon|pipe|...`, `#html:true`,
  `#tags column:N` (and deck / notetype / guid columns) headers; HTML is turned into text; a
  cloze note (`{{c1::answer::hint}}`) becomes one card per cloze number.
* CSV with quoted fields (or a "term,definition" / "front,back" header row).
* Quizlet "Copy text": term and definition split by tab (default), comma or a custom string;
  cards split by new lines (default), semicolons or a custom string. With new lines between
  cards, a line with no separator continues the previous card's definition (Quizlet writes
  multi-line definitions that way).
* The original one-per-line `front :: back` (tab works there too).
"""

from __future__ import annotations

import csv
import html
import io
import re

import nh3

MAX_CARDS = 2000
MAX_FRONT = 2000
MAX_BACK = 4000

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

_ANKI_HEADER = re.compile(r"^#(separator|html|tags column|columns|notetype|deck|guid column|notetype column|"
                          r"deck column|if matches|tags)\s*:", re.I)
CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
_LOOKS_HTML = re.compile(r"<(br|div|p|span|b|i|u|em|strong|img|sub|sup|font|ul|ol|li)\b[^>]*>|&nbsp;|&amp;|&lt;", re.I)


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
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- detection


def _first_row(text: str) -> list[str]:
    for line in text.split("\n"):
        if line.strip():
            try:
                return next(csv.reader([line]))
            except (csv.Error, StopIteration):
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


# ---------------------------------------------------------------- parsers


def _quizlet(text: str, term_sep: str, card_sep: str) -> tuple[list[list[str]], list[str]]:
    cards: list[list[str]] = []
    skipped: list[str] = []
    if card_sep == "\n":
        for line in text.split("\n"):
            if term_sep in line and line.split(term_sep, 1)[0].strip():
                front, back = line.split(term_sep, 1)
                cards.append([front, back])
            elif cards:
                cards[-1][1] += "\n" + line  # a multi-line definition goes on
            elif line.strip():
                skipped.append(line)
    else:
        for chunk in text.split(card_sep):
            if not chunk.strip():
                continue
            if term_sep in chunk:
                front, back = chunk.split(term_sep, 1)
                if front.strip() and back.strip():
                    cards.append([front, back])
                    continue
            skipped.append(chunk)
    kept = []
    for front, back in cards:
        if back.strip():
            kept.append([front, back])
        else:
            skipped.append(front)
    return kept, skipped


def _lines(text: str) -> tuple[list[list[str]], list[str]]:
    """The original format: one card per line, `front :: back` or tab-separated."""
    cards, skipped = [], []
    for line in text.split("\n"):
        if "\t" in line:
            front, back = line.split("\t", 1)
        elif "::" in line:
            front, back = line.split("::", 1)
        else:
            if line.strip():
                skipped.append(line)
            continue
        if front.strip() and back.strip():
            cards.append([front, back])
        else:
            skipped.append(line)
    return cards, skipped


def _csv(text: str, delimiter: str = ",") -> tuple[list[list[str]], list[str]]:
    cards, skipped = [], []
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return _quizlet(text, delimiter, "\n")
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            cards.append([row[0], row[1]])
        else:
            skipped.append(delimiter.join(row))
    return cards, skipped


def html_to_text(value: str) -> str:
    """Anki fields with HTML -> plain text (line breaks kept, tags, images and sounds dropped)."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", value)
    s = re.sub(r"(?i)</(div|p|li|tr|h[1-6])\s*>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"\[sound:[^\]]*\]", "", s)
    s = html.unescape(nh3.clean(s, tags=set()))
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def cloze_cards(text: str, extra: str = "") -> list[list[str]]:
    """`{{c1::Paris}} is the capital of {{c2::France::country}}` -> one card per cloze number.
    The front shows that number's blanks as [...] (or [hint]); the back is the whole sentence."""
    numbers = sorted({int(m.group(1)) for m in CLOZE.finditer(text)})
    full = CLOZE.sub(lambda m: m.group(2), text).strip()
    back = full + (f"\n\n{extra.strip()}" if extra and extra.strip() else "")
    cards = []
    for n in numbers:
        front = CLOZE.sub(lambda m: (f"[{m.group(3)}]" if m.group(3) else "[...]") if int(m.group(1)) == n
                          else m.group(2), text).strip()
        cards.append([front, back])
    return cards


def _anki(text: str) -> tuple[list[list[str]], list[str]]:
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
    excluded = set()
    for key in ("tags column", "deck column", "notetype column", "guid column"):
        if headers.get(key, "").isdigit():
            excluded.add(int(headers[key]))
    cards, skipped = [], []
    try:
        rows = list(csv.reader(io.StringIO(body), delimiter=sep))
    except csv.Error:
        rows = [line.split(sep) for line in body.split("\n")]
    for row in rows:
        fields = [cell for idx, cell in enumerate(row, 1) if idx not in excluded]
        if is_html:
            fields = [html_to_text(f) for f in fields]
        if not any(f.strip() for f in fields):
            continue
        if CLOZE.search(fields[0]):
            cards.extend(cloze_cards(fields[0], fields[1] if len(fields) > 1 else ""))
        elif len(fields) >= 2 and fields[0].strip() and fields[1].strip():
            cards.append([fields[0], fields[1]])
        else:
            skipped.append(sep.join(row))
    return cards, skipped


# ---------------------------------------------------------------- entry point


def parse_cards(text: str | None, term_sep: str | None = None,
                card_sep: str | None = None) -> tuple[list[tuple[str, str]], list[str], str]:
    """Pasted text -> (cards as (front, back), lines we couldn't use, detected format).

    `term_sep` / `card_sep` are "auto" (or None), a name ("tab", "comma", "newline",
    "semicolon") or a custom string; giving either one means Quizlet-style parsing."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        return [], [], "empty"
    ts, cs = _sep(term_sep, TERM_SEPS), _sep(card_sep, CARD_SEPS)
    if ts or cs:
        if not ts:
            ts = "\t" if "\t" in text else ","
        raw, skipped = _quizlet(text, ts, cs or _auto_card_sep(text, ts))
        detected = "quizlet"
    elif _looks_anki(text):
        raw, skipped = _anki(text)
        detected = "anki"
    elif len(row := _first_row(text)) >= 2 and _is_header(row[0], row[1]):
        raw, skipped = _csv(text)
        detected = "csv"
    elif CLOZE.search(text):
        raw, skipped = _anki(text)
        detected = "anki"
    else:
        lines = [line for line in text.split("\n") if line.strip()]
        tab_lines = sum(1 for line in lines if "\t" in line)
        colon_lines = sum(1 for line in lines if "::" in line and "\t" not in line)
        if tab_lines > colon_lines:
            raw, skipped = _quizlet(text, "\t", _auto_card_sep(text, "\t"))
            detected = "quizlet"
        elif colon_lines:
            raw, skipped = _lines(text)
            detected = "lines"
        elif _looks_csv(text):
            raw, skipped = _csv(text)
            detected = "csv"
        elif "," in text:
            raw, skipped = _quizlet(text, ",", _auto_card_sep(text, ","))
            detected = "quizlet"
        else:
            return [], [line for line in lines], "unknown"
    cards: list[tuple[str, str]] = []
    for i, (front, back) in enumerate(raw):
        front, back = front.strip(), re.sub(r"\n{3,}", "\n\n", back.strip())
        if not front or not back:
            skipped.append(front or back)
            continue
        if not cards and i == 0 and _is_header(front, back):
            continue  # a header row, not a card
        if len(cards) >= MAX_CARDS:
            skipped.append(f"{len(raw) - i} more cards past the {MAX_CARDS:,}-card limit")
            break
        if len(front) > MAX_FRONT or len(back) > MAX_BACK:
            skipped.append(f"Trimmed a very long card: {_clip(front, 60)}")
            front, back = front[:MAX_FRONT].rstrip(), back[:MAX_BACK].rstrip()
        cards.append((front, back))
    if not cards and detected != "empty":
        detected = "unknown" if not raw else detected
    return cards, [_clip(s) for s in skipped if s and s.strip()], detected


def separator_choice(src, name: str) -> str | None:
    """A form's separator select ("auto", "tab", ..., "custom") plus its `<name>_custom` box."""
    value = str(src.get(name) or "auto")
    if value == "custom":
        return str(src.get(f"{name}_custom") or "") or None
    return value


def preview(src, show: int = 20) -> dict:
    """What the import box shows while typing: a count, the first cards and the skipped lines."""
    cards, skipped, detected = parse_cards(str(src.get("text") or ""), separator_choice(src, "term_sep"),
                                           separator_choice(src, "card_sep"))
    return {"count": len(cards), "cards": [{"front": f, "back": b} for f, b in cards[:show]],
            "skipped": skipped[:20], "skipped_count": len(skipped), "detected": detected,
            "label": DETECTED_LABELS.get(detected, detected)}


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
