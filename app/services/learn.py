"""Learn and Test mode helpers: answer checking, multiple-choice distractors, test building.

No AI anywhere: distractors are other cards' answers from the same set (numbers get nearby
numbers), and typed answers are compared as text. `normalize` / `check_answer` mirror the
browser's copy in static/js/learn.js, so Test mode grades exactly like Learn mode does.
"""

from __future__ import annotations

import math
import random
import re
import unicodedata

# Punctuation that never changes an answer's meaning. Math symbols (- + / ^ = < > %) stay, and
# a decimal point between digits stays, so "-2" isn't "2" and "3.14" isn't "314".
SOFT_PUNCTUATION = ".,;:!?'\"`()[]{}…–—‘’“”«»¿¡$\\*_~#"
_SOFT_TABLE = str.maketrans("", "", SOFT_PUNCTUATION)
_NUM = re.compile(r"([-+−]?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)")
TYPED_MAX = 80  # longer answers aren't asked as typed questions in Test mode


def normalize(text: str | None, strict: bool = False) -> str:
    """Lenient: ignore case, spaces, punctuation and accents. Strict: only case and spacing."""
    s = unicodedata.normalize("NFC", text or "")
    if strict:
        return " ".join(s.split()).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"(\d)\.(?=\d)", "\\1\x00", s).translate(_SOFT_TABLE).replace("\x00", ".")
    return re.sub(r"\s+", "", s)


def is_number(text: str | None) -> bool:
    return bool(_NUM.fullmatch((text or "").strip()))


def one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by exactly one typo: an insertion, a deletion, a substitution,
    or two neighbouring letters swapped ("teh" for "the")."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) != len(b):
        return a[i:] == b[i + 1:]
    if a[i + 1:] == b[i + 1:]:
        return True
    return i + 1 < len(a) and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]


def check_answer(given: str | None, expected: str | None, strict: bool = False) -> str:
    """"right", "almost" (one typo in a word answer, lenient mode only) or "wrong"."""
    g, e = normalize(given, strict), normalize(expected, strict)
    if not g:
        return "wrong"
    if g == e:
        return "right"
    if not strict and len(e) >= 4 and not is_number(expected) and one_edit_apart(g, e):
        return "almost"
    return "wrong"


# ---------------------------------------------------------------- distractors


def _numeric_variants(answer: str, rng: random.Random, k: int) -> list[str]:
    m = _NUM.fullmatch(answer.strip())
    if not m:
        return []
    sign, currency, digits, frac, pct = m.groups()
    value = float(("-" if sign in ("-", "−") else "") + digits.replace(",", "") + (frac or ""))
    decimals = len(frac) - 1 if frac else 0
    if decimals:
        step = 10 ** -decimals
    elif abs(value) < 20 or 1000 <= abs(value) <= 2100:  # small counts and years move by one
        step = 1
    else:
        step = 10 ** max(0, int(math.log10(abs(value))) - 1)
    deltas = [1, -1, 2, -2, 3, -3, 5, -5, 10, -10]
    rng.shuffle(deltas)
    deltas.sort(key=abs)  # nearby first, random direction
    out, seen = [], {answer.strip()}
    for d in deltas:
        v = value + d * step
        if value >= 0 > v:
            continue
        body = f"{abs(v):,.{decimals}f}" if "," in digits else f"{abs(v):.{decimals}f}"
        text = ("-" if v < 0 else "") + currency + body + pct
        if text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) == k:
            break
    return out


def pick_options(i: int, answers: list[str], rng: random.Random, k: int = 3, keys: list[str] | None = None) -> list:
    """Up to k wrong choices for answers[i]: indexes of other answers in the set, or (for a
    number) nearby numbers as strings when the set doesn't have enough numbers of its own."""
    keys = keys if keys is not None else [normalize(a) for a in answers]
    target = keys[i]
    numeric = is_number(answers[i])
    pool = [j for j in range(len(answers)) if j != i and (not numeric or is_number(answers[j]))]
    if len(pool) > 60:
        pool = rng.sample(pool, 60)
    else:
        rng.shuffle(pool)
    out, seen = [], {target}
    for j in pool:
        if keys[j] and keys[j] not in seen:
            seen.add(keys[j])
            out.append(j)
            if len(out) == k:
                return out
    if numeric:
        for text in _numeric_variants(answers[i], rng, k):
            if normalize(text) not in seen and len(out) < k:
                seen.add(normalize(text))
                out.append(text)
    return out


# ---------------------------------------------------------------- Test mode


def answer_of(card, answer_with: str) -> str:
    return card.front if answer_with == "term" else card.back


def prompt_of(card, answer_with: str) -> str:
    return card.back if answer_with == "term" else card.front


def build_test(cards: list, n: int, answer_with: str = "definition", rng: random.Random | None = None) -> list[dict]:
    """A mixed test over `cards`: one question per picked card, kinds mixed about 40% multiple
    choice, 30% typed, 30% true/false. Each question: {"c": card id, "k": "mc"|"typed"|"tf",
    "o": choices (card ids, or numbers as strings) for mc, "s": shown answer (card id or
    string) for tf}. Correctness is never stored here; it's checked against the cards on submit."""
    rng = rng or random.Random()
    if not cards:
        return []
    answers = [answer_of(c, answer_with) for c in cards]
    keys = [normalize(a) for a in answers]
    distinct = len({k for k in keys if k})
    picked = rng.sample(range(len(cards)), min(max(1, n), len(cards)))
    kinds = (["mc", "mc", "mc", "mc", "typed", "typed", "typed", "tf", "tf", "tf"] * (len(picked) // 10 + 1))[:len(picked)]
    rng.shuffle(kinds)
    questions = []
    for i, kind in zip(picked, kinds):
        card = cards[i]
        if kind == "typed" and len(answers[i]) > TYPED_MAX:
            kind = "mc"
        if kind in ("mc", "tf") and distinct < 2 and not is_number(answers[i]):
            kind = "typed"
        q = {"c": card.id, "k": kind}
        if kind in ("mc", "tf"):
            wrong = pick_options(i, answers, rng, 3 if kind == "mc" else 1, keys)
            if not wrong:
                q["k"] = "typed"
            elif kind == "mc":
                options = [card.id] + [cards[j].id if isinstance(j, int) else j for j in wrong]
                rng.shuffle(options)
                q["o"] = options
            else:
                w = wrong[0]
                q["s"] = card.id if rng.random() < 0.5 else (cards[w].id if isinstance(w, int) else w)
        questions.append(q)
    return questions


def grade_question(q: dict, given, cards_by_id: dict, answer_with: str = "definition") -> tuple[bool, str, bool]:
    """Score one submitted Test answer against the real cards: (correct, what they gave, almost)."""
    card = cards_by_id.get(q["c"])
    if card is None:
        return False, "", False
    expected = answer_of(card, answer_with)

    def text_of(token) -> str:
        if isinstance(token, int):
            other = cards_by_id.get(token)
            return answer_of(other, answer_with) if other is not None else ""
        return str(token)

    if q["k"] == "mc":
        options = q.get("o") or []
        try:
            choice = options[int(given)]
        except (TypeError, ValueError, IndexError):
            return False, "", False
        chosen = text_of(choice)
        return choice == card.id or normalize(chosen) == normalize(expected), chosen, False
    if q["k"] == "tf":
        if given not in ("true", "false"):
            return False, "", False
        statement_true = q.get("s") == card.id or normalize(text_of(q.get("s"))) == normalize(expected)
        return (given == "true") == statement_true, given, False
    typed = str(given or "")[:500]
    verdict = check_answer(typed, expected)
    return verdict in ("right", "almost"), typed, verdict == "almost"
