"""Citation formatting: MLA 9, APA 7 and Chicago 17 (bibliography style).

Returns HTML with titles italicized where the style requires. Inputs are escaped.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date

MONTHS_MLA = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]
MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]


@dataclass
class Source:
    kind: str  # website / book / article
    authors: list[str] = field(default_factory=list)  # "First Middle Last" each
    title: str = ""
    container: str = ""  # website name or journal
    publisher: str = ""
    year: int | None = None
    month: int | None = None
    day: int | None = None
    url: str = ""
    accessed: date | None = None
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    edition: str = ""
    city: str = ""


def _e(s) -> str:
    return html.escape(str(s or "").strip())


def _split(name: str) -> tuple[str, str]:
    parts = name.strip().split()
    if not parts:
        return "", ""
    if "," in name:  # already "Last, First"
        last, first = name.split(",", 1)
        return first.strip(), last.strip()
    return " ".join(parts[:-1]), parts[-1]


def _initials(first: str) -> str:
    out = []
    for piece in first.replace(".", " ").split():
        if "-" in piece:
            out.append("-".join(p[0].upper() + "." for p in piece.split("-") if p))
        else:
            out.append(piece[0].upper() + ".")
    return " ".join(out)


def _end(s: str) -> str:
    """Add a period unless the text already ends in punctuation."""
    s = s.rstrip()
    return s if not s or s[-1] in ".?!" else s + "."


def _italic(s: str) -> str:
    return f"<i>{_e(s)}</i>" if s.strip() else ""


def _doi_url(src: Source) -> str:
    if src.doi:
        doi = src.doi.strip()
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    return src.url.strip()


# ---------------------------------------------------------------- MLA 9


def _mla_authors(authors: list[str]) -> str:
    names = [a for a in authors if a.strip()]
    if not names:
        return ""
    first, last = _split(names[0])
    lead = f"{last}, {first}".strip(", ")
    if len(names) == 1:
        return _end(_e(lead))
    if len(names) == 2:
        f2, l2 = _split(names[1])
        return _end(f"{_e(lead)}, and {_e(f'{f2} {l2}'.strip())}")
    return _end(f"{_e(lead)}, et al")


def _mla_date(src: Source) -> str:
    if not src.year:
        return ""
    bits = []
    if src.day and src.month:
        bits.append(str(src.day))
    if src.month:
        bits.append(MONTHS_MLA[src.month - 1])
    bits.append(str(src.year))
    return " ".join(bits)


def mla(src: Source) -> str:
    parts = [_mla_authors(src.authors)]
    if src.kind == "book":
        parts.append(_end(_italic(src.title)))
        tail = ", ".join(x for x in [_e(src.edition) + (" ed." if src.edition and not src.edition.endswith("ed.") else ""),
                                     _e(src.publisher), str(src.year or "")] if x.strip())
        parts.append(_end(tail))
    elif src.kind == "article":
        parts.append(f"“{_end(_e(src.title))}”")
        bits = [_italic(src.container)]
        if src.volume:
            bits.append(f"vol. {_e(src.volume)}")
        if src.issue:
            bits.append(f"no. {_e(src.issue)}")
        if src.year:
            bits.append(_mla_date(src))
        if src.pages:
            bits.append(f"{'pp.' if '-' in src.pages or '–' in src.pages else 'p.'} {_e(src.pages)}")
        parts.append(_end(", ".join(b for b in bits if b)))
        link = _doi_url(src)
        if link:
            parts.append(_end(_e(link.replace("https://", "").replace("http://", ""))))
    else:  # website
        parts.append(f"“{_end(_e(src.title))}”")
        bits = [_italic(src.container)]
        if src.publisher and src.publisher.strip() != src.container.strip():
            bits.append(_e(src.publisher))
        if src.year:
            bits.append(_mla_date(src))
        if src.url:
            bits.append(_e(src.url.replace("https://", "").replace("http://", "")))
        parts.append(_end(", ".join(b for b in bits if b)))
        if src.accessed:
            parts.append(f"Accessed {src.accessed.day} {MONTHS_MLA[src.accessed.month - 1]} {src.accessed.year}.")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- APA 7


def _apa_authors(authors: list[str]) -> str:
    names = [a for a in authors if a.strip()]
    formatted = []
    for n in names[:20]:
        first, last = _split(n)
        formatted.append(f"{last}, {_initials(first)}".strip(", ") if first else last)
    if not formatted:
        return ""
    if len(formatted) == 1:
        return _e(formatted[0])
    return _e(", ".join(formatted[:-1]) + ", & " + formatted[-1])


def _apa_date(src: Source) -> str:
    if not src.year:
        return "(n.d.)."
    if src.kind == "website" and src.month:
        day = f" {src.day}" if src.day else ""
        return f"({src.year}, {MONTHS_FULL[src.month - 1]}{day})."
    return f"({src.year})."


def apa(src: Source) -> str:
    author = _apa_authors(src.authors)
    lead = _end(author) + " " + _apa_date(src) if author else None
    if src.kind == "book":
        title = _italic(src.title) + (f" ({_e(src.edition)} ed.)" if src.edition else "")
        body = [_end(title), _end(_e(src.publisher)) if src.publisher else ""]
        link = _doi_url(src)
    elif src.kind == "article":
        body = [_end(_e(src.title))]
        journal = _italic(src.container)
        if src.volume:
            journal += f", <i>{_e(src.volume)}</i>"
        if src.issue:
            journal += f"({_e(src.issue)})"
        if src.pages:
            journal += f", {_e(src.pages)}"
        body.append(_end(journal))
        link = _doi_url(src)
    else:
        body = [_end(_italic(src.title))]
        if src.container:
            body.append(_end(_e(src.container)))
        link = src.url.strip()
    if lead is None:  # no author: title moves to the front
        first, rest = body[0], body[1:]
        parts = [first, _apa_date(src)] + rest
    else:
        parts = [lead] + body
    if link:
        parts.append(_e(link))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- Chicago 17 (bibliography)


def _chicago_authors(authors: list[str]) -> str:
    names = [a for a in authors if a.strip()]
    if not names:
        return ""
    first, last = _split(names[0])
    out = [f"{last}, {first}".strip(", ")]
    if len(names) > 10:
        return _end(_e(", ".join(out + [" ".join(_split(n)) for n in names[1:7]]) + ", et al"))
    rest = [f"{f} {l}".strip() for f, l in (_split(n) for n in names[1:])]
    if not rest:
        return _end(_e(out[0]))
    return _end(_e(", ".join(out + rest[:-1]) + ", and " + rest[-1]))


def chicago(src: Source) -> str:
    parts = [_chicago_authors(src.authors)]
    if src.kind == "book":
        parts.append(_end(_italic(src.title)))
        place = ": ".join(x for x in [_e(src.city), _e(src.publisher)] if x)
        parts.append(_end(", ".join(x for x in [place, str(src.year or "")] if x)))
    elif src.kind == "article":
        parts.append(f"“{_end(_e(src.title))}”")
        bit = _italic(src.container)
        if src.volume:
            bit += f" {_e(src.volume)}"
        if src.issue:
            bit += f", no. {_e(src.issue)}"
        if src.year:
            bit += f" ({src.year})"
        if src.pages:
            bit += f": {_e(src.pages)}"
        parts.append(_end(bit))
        link = _doi_url(src)
        if link:
            parts.append(_end(_e(link)))
    else:
        parts.append(f"“{_end(_e(src.title))}”")
        if src.container:
            parts.append(_end(_e(src.container)))
        if src.year:
            date_text = " ".join(x for x in [MONTHS_FULL[src.month - 1] if src.month else "",
                                            f"{src.day}," if src.day and src.month else "", str(src.year)] if x)
            parts.append(_end(f"Published {date_text}" if src.month else str(src.year)))
        elif src.accessed:
            parts.append(_end(f"Accessed {MONTHS_FULL[src.accessed.month - 1]} {src.accessed.day}, {src.accessed.year}"))
        if src.url:
            parts.append(_end(_e(src.url)))
    return " ".join(p for p in parts if p)


STYLES = {"mla": ("MLA 9", mla), "apa": ("APA 7", apa), "chicago": ("Chicago 17", chicago)}
