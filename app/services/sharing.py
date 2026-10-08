"""Opt-in sharing of flashcard decks and practice quizzes, Quizlet-style.

Nothing is shared unless its owner turns sharing on for that set, and even then only the student's
own work goes out:

* Cards the AI made from synced class files or uploads (origin "ai_files"), cards saved from
  someone else's shared set ("copy") and cards matching a set that was taken down ("removed") stay
  private until the student rewrites them in their own words (`edited_enough`, always measured
  against the text the card started as, so undoing an edit puts the card back on hold). Pasting such
  text into a new card, deck or quiz keeps that origin (`tag_known_copies`, `quiz_origin`).
* Cards and quiz questions that copy the student's synced class materials word for word (any run of
  RUN words in a row, ignoring case and punctuation) are held back. Shorter overlaps are facts and
  terms, which nobody owns. The check runs when a set is shared, edited or hosted live.
* Quizzes the AI made from class files, quizzes saved from someone else, and live games made from a
  deck are never shared; a quiz with a copied question can't be shared or hosted live. A set that
  was taken down or is waiting for review (or comes from one) can't be hosted live.

Shared sets are reachable by link, and "class" sets are also listed for classmates (same Canvas
course, same school). Signed-out visitors see a short preview; everything else needs a free account.
Owners are never named. Signed-in users can report a set: a copyright or exam report hides it at
once until an admin looks, other reasons after AUTO_HIDE_REPORTS reports, and a reported set can't be
deleted until it's reviewed. An upheld report takes the set (and live games made from it) down for
good, deletes copies saved from it, and gives its owner a strike; STRIKES_TO_BLOCK strikes end
sharing, live hosting included, for that account. The legal pages describe exactly this.
"""

from __future__ import annotations

import copy
import random
import re
import secrets
import threading
import unicodedata
from collections import Counter

from sqlalchemy import func, select

from ..extensions import db
from ..models import Card, ContentChunk, Course, Deck, PracticeQuiz, ShareReport, Upload, User, utcnow
from . import learn

MODES = ("private", "link", "class")
RUN = 10                  # words in a row that count as copying the class materials
PAIR_LIMIT = 0.5          # a rewrite keeps less than half of the original's two-word phrases...
WORD_LIMIT = 0.6          # ...and less than 60% of its words
PREVIEW_CARDS = 5         # what a signed-out visitor sees of a shared deck
PREVIEW_QUESTIONS = 2     # ... and of a shared quiz (questions only, no answers)
AUTO_HIDE_REPORTS = 2     # open reports (from different people) that hide a set for other reasons
HIDE_AT_ONCE = ("copyright", "exam")
STRIKES_TO_BLOCK = 3
REASONS = {
    "copyright": "It copies my copyrighted work (I'm the instructor, author or publisher)",
    "exam": "It has questions from a real quiz or exam",
    "personal": "It has someone's personal information",
    "other": "Something else",
}
LIVE_MIN_CARDS = 4        # a live game from a deck needs enough answers for three wrong choices
AUTO_DESCRIPTION = "Generated from "  # the generator's description names the source file or page: never shown to others
MAX_RUNS_PER_PASS = 150_000  # bounds the memory of one check; bigger decks take more passes over the material

BLOCK_NOTES = {
    "ai_unedited": "AI wording from your class files",
    "not_yours": "saved from someone else's set",
    "taken_down": "matches a set that was taken down",
    "verbatim": "copies your class materials word for word",
    "unchecked": "not checked yet",
}
# Origins that aren't the student's until rewritten, and why they're held back.
HELD = {"ai_files": "ai_unedited", "copy": "not_yours", "removed": "taken_down"}
LIVE_OK = (None, "not_yours")  # a live game may use cards saved from a shared set, as they were shared

_WORD = re.compile(r"[^\W_]+")
_SCANS = threading.BoundedSemaphore(2)  # at most two material scans at once on the small server


class ShareError(ValueError):
    pass


def words(text: str | None) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text or "").casefold())


def _run_hashes(w: list[str]) -> set[int]:
    return {hash(tuple(w[i:i + RUN])) for i in range(len(w) - RUN + 1)}


def card_text(front: str | None, back: str | None) -> str:
    return f"{front or ''}\n{back or ''}"


def edited_enough(original: str, current: str) -> bool:
    """Whether `current` is a rewrite of `original` in the student's own words: it keeps no run of RUN
    words, less than PAIR_LIMIT of the original's two-word phrases and less than WORD_LIMIT of its
    words. Padding the original, swapping a few synonyms or undoing an edit doesn't count. Linear
    time, so long or repetitive cards can't stall the server."""
    a, b = words(original), words(current)
    if not a:
        return True
    if len(a) >= RUN and _run_hashes(a) & _run_hashes(b):
        return False
    left = Counter(b)
    kept = 0
    for w in a:
        if left[w] > 0:
            left[w] -= 1
            kept += 1
    if kept / len(a) >= WORD_LIMIT:
        return False
    if len(a) < 2:
        return True
    pairs = {(a[i], a[i + 1]) for i in range(len(a) - 1)}
    theirs = {(b[i], b[i + 1]) for i in range(len(b) - 1)}
    return len(pairs & theirs) / len(pairs) < PAIR_LIMIT


# ---------------------------------------------------------------- word-for-word check


def _material(user_id: int):
    """Every piece of the student's class material here, as word lists: course pages, syllabi,
    assignment descriptions, announcements, file and upload text (the search chunks), and uploads not
    filed under a class (which have no chunks). Consecutive chunks of one source carry the previous
    chunk's last RUN-1 words, so a run can't hide across a chunk boundary."""
    rows = db.session.execute(
        select(ContentChunk.source_type, ContentChunk.source_id, ContentChunk.text)
        .where(ContentChunk.user_id == user_id)
        .order_by(ContentChunk.source_type, ContentChunk.source_id, ContentChunk.ordinal)
        .execution_options(yield_per=200))
    last_key, tail = None, []
    for source_type, source_id, text in rows:
        w = words(text)
        key = (source_type, source_id)
        yield tail + w if key == last_key else w
        tail = w[-(RUN - 1):]
        last_key = key
    loose = db.session.execute(select(Upload.text).where(Upload.user_id == user_id, Upload.course_id.is_(None),
                                                         Upload.text.is_not(None)).execution_options(yield_per=10))
    for text in loose.scalars():
        yield words(text)


def _scan(user_id: int, wanted: dict[int, int], dups: dict[int, set], need: int) -> set[int]:
    hits: set[int] = set()
    for w in _material(user_id):
        for i in range(len(w) - RUN + 1):
            h = hash(tuple(w[i:i + RUN]))
            if h in wanted:
                hits |= dups.get(h) or {wanted[h]}
        if len(hits) >= need:
            break
    return hits


def copied_texts(user_id: int, texts: list[str]) -> set[int]:
    """The indexes of `texts` that copy the student's class materials word for word."""
    batches, wanted, dups, need = [], {}, {}, set()
    for i, text in enumerate(texts):
        runs = _run_hashes(words(text))
        if not runs:
            continue
        if wanted and len(wanted) + len(runs) > MAX_RUNS_PER_PASS:
            batches.append((wanted, dups, need))
            wanted, dups, need = {}, {}, set()
        need.add(i)
        for h in runs:
            if h in wanted and wanted[h] != i:
                dups.setdefault(h, {wanted[h]}).add(i)
            else:
                wanted[h] = i
    if wanted:
        batches.append((wanted, dups, need))
    hits: set[int] = set()
    if not batches:
        return hits
    with _SCANS:
        for w, d, n in batches:
            hits |= _scan(user_id, w, d, len(n))
    return hits


# ---------------------------------------------------------------- checks


def check_cards(deck: Deck, cards: list[Card] | None = None) -> None:
    """Set share_block on `cards` (default: all of them)."""
    db.session.flush()  # new cards become rows first, so the result is saved as an update
    cards = list(deck.cards if cards is None else cards)
    copied = copied_texts(deck.user_id, [card_text(c.front, c.back) for c in cards])
    for i, c in enumerate(cards):  # copying class materials outranks every other reason
        c.share_block = ("verbatim" if i in copied else HELD[c.origin] if c.origin in HELD and not c.rewritten
                         else None)


def card_changed(deck: Deck, card: Card, before: tuple[str, str] | None = None) -> None:
    """After a card is added or edited: a card someone else wrote (the AI from class files, or another
    student) counts as the student's while it's far enough from the text it started as; then it's
    checked now if the deck is shared, or when it's next shared."""
    if card.origin not in HELD:
        tag_known_copies(deck.user_id, [card])  # edited to paste in someone else's (or the AI's) card
    if card.origin in HELD:
        if card.origin_text is None and before is not None:
            card.origin_text = card_text(*before)  # cards from before origin_text existed
        card.rewritten = card.origin_text is not None and edited_enough(card.origin_text, card_text(card.front, card.back))
    if deck.share_mode != "private":
        check_cards(deck, [card])
    else:
        card.share_block = "unchecked"


def tag_known_copies(user_id: int, cards: list[Card]) -> None:
    """New cards that are exactly (ignoring case and punctuation) one of the student's AI cards from
    class files or saved copies, say after exporting and importing a deck, keep that origin."""
    if not cards:
        return
    known = {}
    rows = db.session.execute(
        select(Card.id, Card.origin, Card.origin_text, Card.rewritten, Card.front, Card.back, Deck.taken_down_at).join(Deck)
        .where(Deck.user_id == user_id, Card.origin.in_(tuple(HELD)) | Deck.taken_down_at.is_not(None)))
    skip = {c.id for c in cards if c.id}
    for card_id, origin, origin_text, rewritten, front, back, taken_down_at in rows:
        if card_id in skip:
            continue
        current = card_text(front, back)
        kind = "removed" if taken_down_at else origin
        # The text it started as; and its current text while that's still not the student's own.
        for text in (origin_text, None if rewritten and not taken_down_at else current):
            if text and words(text):
                known.setdefault(tuple(words(text)), (kind, origin_text or current))
    for c in cards:
        hit = known.get(tuple(words(card_text(c.front, c.back))))
        if hit:
            c.origin, c.origin_text, c.rewritten = hit[0], hit[1], False


def question_text(q: dict) -> str:
    return "\n".join([q.get("question") or "", *(q.get("choices") or []), q.get("explanation") or ""])


def quiz_origin(user_id: int, questions: list[dict], exclude_id: int | None = None) -> str | None:
    """Whether a quiz being saved repeats a whole question (stem and choices) from one of the student's
    AI quizzes from class files ("files"), a set taken down after a report ("removed"), or a quiz or
    live game saved from someone else's set ("copy"). Recomputed on every save, so removing the
    question clears it."""
    mine = {tuple(words(question_text(q))) for q in questions}
    mine.discard(())
    if not mine:
        return None
    rows = db.session.scalars(select(PracticeQuiz).where(
        PracticeQuiz.user_id == user_id, PracticeQuiz.id != (exclude_id or 0),
        PracticeQuiz.from_course_files.is_(True) | (PracticeQuiz.source == "copy") | PracticeQuiz.pasted_from.is_not(None)
        | PracticeQuiz.taken_down_at.is_not(None))).all()
    found = set()
    for quiz in rows:
        if any(tuple(words(question_text(q))) in mine for q in quiz.questions or []):
            found.add("files" if quiz.from_course_files or quiz.pasted_from == "files" else
                      "removed" if quiz.taken_down_at or quiz.pasted_from == "removed" else "copy")
    return next((k for k in ("files", "removed", "copy") if k in found), None)


def check_quiz(quiz: PracticeQuiz) -> None:
    if quiz.from_course_files:
        quiz.share_blocked = None
        return
    quiz.share_blocked = sorted(copied_texts(quiz.user_id, [question_text(q) for q in quiz.questions or []]))


def shown_cards(deck: Deck) -> list[Card]:
    return [c for c in deck.cards if c.share_block is None]


def preview_cards(deck: Deck, limit: int) -> list[Card]:
    return db.session.scalars(select(Card).where(Card.deck_id == deck.id, Card.share_block.is_(None))
                              .order_by(Card.position, Card.id).limit(limit)).all()


def block_counts(deck: Deck) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in deck.cards:
        if c.share_block:
            counts[c.share_block] = counts.get(c.share_block, 0) + 1
    return counts


def public_description(item) -> str | None:
    """The description others see: never the generator's "Generated from <file>" line."""
    d = getattr(item, "description", None)
    return None if not d or d.startswith(AUTO_DESCRIPTION) else d


# ---------------------------------------------------------------- turning sharing on and off


def class_course(item) -> Course | None:
    """The course whose classmates see a "class" set: a Canvas class (other LMSs and calendar links
    have no class ID to match classmates by, so their classes can't share with a class yet). It's listed only to classmates matched by Course.chat_key, the proof of being in the class
    that class chat uses, so a hand-made sync can't read a real class's sets; until the owner's
    extension sends that proof (1.5.2), the set stays "class" but isn't listed yet."""
    course = item.course
    if course is None or course.account is None or course.account.lms != "canvas":
        return None
    return course


def set_mode(user: User, item, mode: str, confirmed: bool) -> None:
    """Share a deck or quiz by link or with its class, or make it private. Raises ShareError. Sharing
    a private set again makes a new link, so an old one that got around stays dead."""
    if mode not in MODES:
        raise ShareError("Pick how to share it.")
    if mode == "private":
        item.share_mode = "private"
        return
    if user.sharing_blocked:
        raise ShareError("Sharing is turned off for your account after repeated takedowns. Your sets are still yours to study.")
    if item.taken_down_at:
        raise ShareError("This set was taken down after a report, so it can't be shared again.")
    if not confirmed:
        raise ShareError("Confirm the set is your own work before sharing it.")
    if mode == "class" and class_course(item) is None:
        raise ShareError("Pick the Canvas class this set is for first (in its settings), then share it with that class.")
    if isinstance(item, Deck):
        check_cards(item)
        if not shown_cards(item):
            item.share_mode = "private"
            raise ShareError("None of these cards can be shared yet: rewrite the AI's cards and cards you saved from "
                             "someone else in your own words, and don't copy your class materials word for word.")
    else:
        refusal = quiz_refusal(item)
        if refusal:
            item.share_mode = "private"
            raise ShareError(refusal)
        check_quiz(item)
        if item.share_blocked:
            item.share_mode = "private"  # a quiz is shared whole or not at all
            n = len(item.share_blocked)
            raise ShareError(f"{n} question{'s' if n != 1 else ''} copy your class materials word for word "
                             f"(number{'s' if n != 1 else ''} {', '.join(str(i + 1) for i in item.share_blocked)}). "
                             "Put them in your own words, then share.")
    if item.share_mode == "private" or not item.share_token:
        item.share_token = secrets.token_urlsafe(16)
    item.share_mode = mode
    item.shared_at = item.shared_at or utcnow()


def quiz_refusal(quiz: PracticeQuiz) -> str | None:
    """Why a quiz can never be shared, whatever its questions say now (or None)."""
    if quiz.from_course_files or quiz.pasted_from == "files":
        return ("Quizzes the AI made from class files stay private, including questions pasted from one. Write your "
                "own quiz, or make one from your own pasted notes, to share it.")
    if quiz.source == "deck":
        return "Live games made from a deck can't be shared. Share the deck itself."
    if quiz.pasted_from == "removed":
        return "Some questions match a set that was taken down after a report, so this quiz can't be shared or hosted live."
    if quiz.source == "copy" or quiz.pasted_from == "copy":
        return "Quizzes with questions saved from someone else's set can't be shared again. Write your own to share it."
    return None


def find(token: str | None):
    """The deck or quiz shared under a link, or None (private, hidden, taken down, or its owner can't
    share any more)."""
    if not token or len(token) > 32:
        return None
    for model in (Deck, PracticeQuiz):
        item = db.session.scalar(select(model).where(model.share_token == token))
        if item is not None:
            return item if is_visible(item) else None
    return None


def is_visible(item) -> bool:
    if isinstance(item, PracticeQuiz) and (item.share_blocked or quiz_refusal(item)):
        return False
    owner = db.session.get(User, item.user_id)
    return (item.share_mode in ("link", "class") and not item.share_hidden and not item.taken_down_at
            and owner is not None and owner.active and not owner.sharing_blocked)


def class_sets(viewer: User, course: Course, limit: int = 30) -> list[tuple[str, object]]:
    """("deck" | "quiz", set) for sets classmates shared with this class (same Canvas course at the
    same school, matched by Course.chat_key), newest first; never the viewer's own."""
    if not course.chat_key:
        return []
    out = []
    for model in (Deck, PracticeQuiz):
        rows = db.session.scalars(
            select(model).join(Course, Course.id == model.course_id).join(User, User.id == model.user_id)
            .where(Course.chat_key == course.chat_key, model.user_id != viewer.id, model.share_mode == "class",
                   model.share_hidden.is_(False), model.taken_down_at.is_(None), User.active.is_(True),
                   User.sharing_blocked.is_(False))
            .order_by(model.shared_at.desc()).limit(limit)).all()
        out += [("deck" if model is Deck else "quiz", r) for r in rows if model is Deck or is_visible(r)]
    out.sort(key=lambda x: x[1].shared_at or utcnow(), reverse=True)
    return out[:limit]


def shown_count(item) -> int:
    if isinstance(item, Deck):
        return db.session.scalar(select(func.count(Card.id)).where(Card.deck_id == item.id, Card.share_block.is_(None))) or 0
    return len(item.questions or [])


def copy_count(item) -> int:
    model = type(item)
    return db.session.scalar(select(func.count(model.id)).where(model.copied_from_id == item.id)) or 0


# ---------------------------------------------------------------- copies


def _viewer_course(viewer: User, item) -> int | None:
    """The viewer's own section of the set's class, if they have one."""
    key = item.course.chat_key if item.course is not None else None
    if not key:
        return None
    return db.session.scalar(select(Course.id).where(Course.user_id == viewer.id, Course.chat_key == key).limit(1))


def copy_to(viewer: User, item):
    """Save a shared set to the viewer's account (only what's shown to others). The copy is theirs to
    study, edit and play live, but not to share again until they rewrite it."""
    if isinstance(item, Deck):
        new = Deck(user_id=viewer.id, title=item.title[:200], description=public_description(item), source="copy",
                   course_id=_viewer_course(viewer, item), copied_from_id=item.id)
        new.cards = [Card(front=c.front, back=c.back, position=i, origin="copy", origin_text=card_text(c.front, c.back))
                     for i, c in enumerate(shown_cards(item))]
    else:
        new = PracticeQuiz(user_id=viewer.id, title=item.title[:200], questions=copy.deepcopy(item.questions), source="copy",
                           course_id=_viewer_course(viewer, item), copied_from_id=item.id,
                           seconds_per_question=item.seconds_per_question)
    db.session.add(new)
    return new


def _copies(item) -> list:
    """Copies saved from a set, and copies of those copies."""
    model, out, frontier = type(item), [], [item.id]
    while frontier:
        rows = db.session.scalars(select(model).where(model.copied_from_id.in_(frontier))).all()
        out += rows
        frontier = [r.id for r in rows]
    return out


# ---------------------------------------------------------------- live games


def _lineage(item) -> list:
    """The set, the deck a live game was made from, and the sets it was saved from, up to the original."""
    out, seen = [item], set()
    if isinstance(item, PracticeQuiz) and item.from_deck_id:
        deck = db.session.get(Deck, item.from_deck_id)
        if deck is not None:
            out.append(deck)
    for x in list(out):
        while x is not None and x.copied_from_id and (type(x), x.id) not in seen:
            seen.add((type(x), x.id))
            x = db.session.get(type(x), x.copied_from_id)
            if x is not None:
                out.append(x)
    return out


def live_refusal(user: User, item) -> str | None:
    """Why `user` can't host `item` live, or None. Live games are sharing too, so a set that was taken
    down or is waiting for review, or that came from one, can't be hosted."""
    if user.sharing_blocked:
        return "Sharing is turned off for your account after repeated takedowns, so you can't host live games."
    if isinstance(item, PracticeQuiz) and (item.from_course_files or item.pasted_from in ("files", "removed")):
        return quiz_refusal(item)
    for x in _lineage(item):
        if x.taken_down_at:
            return "This set (or the one it came from) was taken down after a report, so it can't be hosted live."
        if x.share_hidden or open_reports(x):
            return "This set (or the one it came from) was reported, so it can't be hosted live until we review it."
    return None


def live_questions(deck: Deck, rng: random.Random | None = None) -> list[dict]:
    """Multiple-choice questions for a live game from a deck: the cards that could be shared, plus
    cards saved from someone's shared set as they were shared (other cards' backs are the wrong
    choices). Raises ShareError when there aren't enough."""
    rng = rng or random.Random()
    check_cards(deck)
    cards = [c for c in deck.cards if c.share_block in LIVE_OK]
    answers = [c.back for c in cards]
    keys = [learn.normalize(a) for a in answers]
    questions = []
    for i, card in enumerate(cards):
        wrong = learn.pick_options(i, answers, rng, 3, keys)
        if len(wrong) < 3:
            continue
        choices = [card.back] + [answers[j] if isinstance(j, int) else j for j in wrong]
        order = list(range(len(choices)))
        rng.shuffle(order)
        questions.append({"question": card.front[:2000], "choices": [choices[k][:500] for k in order],
                          "answer": order.index(0), "explanation": ""})
    rng.shuffle(questions)
    if len(questions) < LIVE_MIN_CARDS:
        raise ShareError(f"A live game needs at least {LIVE_MIN_CARDS} cards with different answers in your own words "
                         "(not the AI's wording of class files, and not copied from class materials).")
    return questions[:50]


# ---------------------------------------------------------------- reports and takedowns


def _report_col(item):
    return ShareReport.deck_id if isinstance(item, Deck) else ShareReport.quiz_id


def open_reports(item) -> list[ShareReport]:
    return db.session.scalars(select(ShareReport).where(_report_col(item) == item.id, ShareReport.resolved.is_(False))
                              .order_by(ShareReport.created_at)).all()


def report(reporter: User, item, reason: str, details: str | None) -> bool:
    """Record a report (one open report per person per set; reporting again with a more serious reason
    updates it). Returns whether the set is now hidden."""
    if reason not in REASONS:
        raise ShareError("Pick a reason.")
    details = (details or "").strip()[:2000] or None
    mine = db.session.scalar(select(ShareReport).where(_report_col(item) == item.id, ShareReport.reporter_id == reporter.id,
                                                       ShareReport.resolved.is_(False)))
    if mine is None:
        mine = ShareReport(reporter_id=reporter.id, reason=reason, details=details, snapshot=snapshot(item))
        if isinstance(item, Deck):
            mine.deck_id = item.id
        else:
            mine.quiz_id = item.id
        db.session.add(mine)
    elif reason in HIDE_AT_ONCE or mine.reason not in HIDE_AT_ONCE:
        mine.reason = reason
        mine.details = details or mine.details
    db.session.flush()
    reports = open_reports(item)
    if any(r.reason in HIDE_AT_ONCE for r in reports) or len({r.reporter_id for r in reports}) >= AUTO_HIDE_REPORTS:
        item.share_hidden = True
    return item.share_hidden


def snapshot(item) -> list:
    """What others could see of a set: [front, back] per card, or the questions."""
    if isinstance(item, Deck):
        return [[c.front, c.back] for c in shown_cards(item)][:500]
    return copy.deepcopy(item.questions or [])[:200]


def deletion_refusal(item) -> str | None:
    if open_reports(item):
        return "This set has a report we haven't reviewed yet, so it can't be deleted until we do."
    return None


def _resolve(item, outcome: str) -> None:
    for r in open_reports(item):
        r.resolved = True
        r.outcome = outcome


def take_down(item) -> int:
    """Uphold the reports: the set (and any live game made from it) goes private for good, copies saved
    from it are deleted, and its owner gets one strike (STRIKES_TO_BLOCK strikes end their sharing).
    Taking down a set that's already down only closes its reports. Returns how many copies went."""
    if item.taken_down_at:
        _resolve(item, "removed")
        return 0
    item.share_mode = "private"
    item.share_hidden = False
    item.taken_down_at = utcnow()
    _resolve(item, "removed")
    copies = _copies(item)
    if isinstance(item, Deck):  # live games made from the set, or from copies of it
        for game in db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.from_deck_id.in_([item.id] + [c.id for c in copies]))):
            if game.user_id == item.user_id:
                game.share_mode = "private"
                game.taken_down_at = item.taken_down_at
            else:
                db.session.delete(game)
    for c in copies:
        db.session.delete(c)
    owner = db.session.get(User, item.user_id)
    owner.share_strikes = (owner.share_strikes or 0) + 1
    if owner.share_strikes >= STRIKES_TO_BLOCK:
        block_sharing(owner)
    return len(copies)


def block_sharing(user: User) -> None:
    user.sharing_blocked = True
    for model in (Deck, PracticeQuiz):
        for item in db.session.scalars(select(model).where(model.user_id == user.id, model.share_mode != "private")):
            item.share_mode = "private"


def dismiss(item) -> None:
    item.share_hidden = False
    _resolve(item, "dismissed")


def hidden_without_reports() -> list[tuple[str, object]]:
    """Sets still hidden with no open report (say, every reporter deleted their account): the admin
    queue lists them so they can be reviewed."""
    out = []
    for kind, model, col in (("deck", Deck, ShareReport.deck_id), ("quiz", PracticeQuiz, ShareReport.quiz_id)):
        has_open = select(ShareReport.id).where(col == model.id, ShareReport.resolved.is_(False)).exists()
        out += [(kind, x) for x in db.session.scalars(select(model).where(model.share_hidden.is_(True), ~has_open))]
    return out
