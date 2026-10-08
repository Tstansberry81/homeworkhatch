"""Which synced Canvas items are tests, quizzes or exams (the exam planner's input).

Deterministic and explainable, no AI: each item gets a score from its name (with topic words like
"ratio test" or "physical exam" removed first), Canvas's own structure (quiz engine, in-person,
grading), its assignment group and share of the grade, and its instructions; hard vetoes catch
lookalikes (exam corrections, study guides, practice exams, policy and training quizzes, final
projects). Scores >= HIGH are planned automatically, >= MEDIUM ask the student once, below that
never plan. Every result carries the reasons, so the page can say why.

Tuned against ~300 synthetic cases (STEM, humanities, languages, nursing, CS, high school); the
student's "Not a test" / "Study for this" answers (AssessmentChoice) always win, and one answer
covers a series ("Quiz 1..12") through the family key.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

VERSION = 2
HIGH = 5.0
MEDIUM = 2.5
KINDS = ("final", "midterm", "test", "quiz")


# ------------------------------------------------------------------ text helpers

def _fold(s: str) -> str:
    """accents -> ASCII ('Capítulo' -> 'capitulo', 'Exámenes' -> 'examenes', 'Prüfung' -> 'prufung')."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def strip_html(s: str | None, limit: int = 4000) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", " ", s)
    return re.sub(r"\s+", " ", _fold(s)).strip().lower()[:limit]


LOCKDOWN_SUFFIX = re.compile(r"\s*[-–—(]*\s*requires\s+(respondus\s+)?lock\s*down\s+browser\)?\s*$", re.I)
SUBTITLE_SPLIT = re.compile(r":|\s[-–—]\s|\(|\)")


def norm(s: str) -> str:
    s = _fold(html.unescape(s or "")).lower().replace("&", " and ")
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)          # 'midterm1' -> 'midterm 1', 'mt2' -> 'mt 2', 'quiz10'
    s = re.sub(r"(\d)([a-z]{3,})", r"\1 \2", s)      # '5quiz' -> '5 quiz' (keeps '2b', '1st', '9am')
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


def split_name(raw: str) -> tuple[str, str]:
    parts = SUBTITLE_SPLIT.split(raw, maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def R(p: str) -> re.Pattern:
    return re.compile(p)


# ------------------------------------------------------------------ keyword families (norm()ed text)

FINAL_EXAM = R(r" final (exam|examination|test|assessment)s? | (cumulative|comprehensive) (final|exam|test|examination)s? "
               r"|(?<! mid) semester (exam|test|final)s? | end of (course|year|semester|term) (exam|test|assessment)s? "
               r"| eoc (exam|test|assessment)s? | examen(es)? finale?s? ")
# 'final' alone counts only when every word after it (in that text) is in this allowlist.
FINAL_OK = {"exam", "exams", "examination", "test", "assessment", "part", "pt", "section", "sec", "version", "form",
            "day", "cumulative", "comprehensive", "written", "online", "oral", "in", "class", "take", "home", "sitting",
            "retake", "makeup", "make", "up", "conflict", "alternate", "a", "b", "c", "d", "am", "pm", "with", "w",
            "lockdown", "lock", "down", "browser", "respondus", "honorlock", "proctorio", "proctored", "gradescope",
            "canvas", "multiple", "choice", "mc", "mcq", "frq", "free", "response", "portion", "listening", "speaking", "i", "ii", "iii", "iv", "room", "hall", "sec", "spring", "fall", "winter", "summer"}
MIDTERM = R(r" mid ?terms? | mid (semester|quarter) (exam|test|examination)s? | prelims? | preliminary (exam|examination)s? | hour exams? "
            r"| mt (\d+[a-z]?|[ivx]+) | mid ?sems? | mid ?year (exam|test|assessment|final)s? "
            r"| examen(es)? parcial(es)? | parcial(es)? (\d+|[ivx]+) | parcial(es)? $")
EXAM = R(r" exam(s|ination|inations)? | examen(es)? (oral|orales|escrito|\d+[a-z]?|[ivx]+|de|del) "
         r"| klausur(en)? | prufung(en)? | pruefung(en)? ")
TEST = R(r" tests? | retests? | post ?tests? | pruebas? | controles? (\d+|continu) | interro(gation)?s? (\d+|ecrite) ")
PRACTICAL = R(r" (lab|laboratory) practicals? | practicals? (exam|test)s? | practicals? \d+ | practicals? $")
ASSESSMENT = R(r" (assessment|benchmark|summative)s? ")
QUIZ = R(r" quiz(zes)? ")
WEAK = R(r" (knowledge|concept|reading|comprehension|vocab|vocabulary|skills?) checks? | checkpoints? "
         r"| test your (knowledge|understanding|skills) | check (your|for) understanding | quests? (\d+|[ivx]+) ")   # quest
LOCKDOWN_WORDS = R(r" (lock ?down browser|respondus) ")

# 'test'/'exam' used as a topic or a verb, removed before TEST/EXAM are matched.
TEST_TOPIC = R(
    r" (t|z|f|chi square|chi squared|ratio|root|comparison|limit comparison|integral|divergence|convergence|alternating series"
    r"|nth term|n th term|derivative|first derivative|second derivative|vertical line|horizontal line|hypothesis|significance"
    r"|goodness of fit|sign|signed rank|rank sum|wilcoxon|mann whitney|tukey|wald|likelihood ratio|turing|taste|stress|field"
    r"|pilot|crash|drug|blood|urine|skin|tb|patch|litmus|flame|benedict s|benedicts|biuret|iodine|starch|glucose tolerance"
    r"|pregnancy|strep|covid|usability|beta|acid) tests? "
    r"| tests? (your|yourself|the|an|my|our|their|this|these|those|each|it|them|whether|if|how|out|drive|tubes?|banks?"
    r"|taking|anxiety|statistics?|scores?) ")
EXAM_TOPIC = R(
    r" (physical|eye|vision|hearing|cranial nerves?|breast|pelvic|rectal|prostate|head to toe|focused"
    r"|mental status|newborn|well child|cross|self|skin|foot|fundoscopic|otoscopic) exam(s|ination|inations)? "
    r"| exam(ination)?s? of ")

A_NOUN = r"(exam|exams|examination|examinations|test|tests|quiz|quizzes|midterm|midterms|final|finals|prelim|prelims|practical|practicals)"
NUMBER = r"( \d+[a-z]?| [ivx]+)?"

HARD_ADMIN = R(
    r" (feedback|course|mid ?semester|mid ?term|mid ?quarter|end of (term|semester|course)|student|pre course|post course"
    r"|interest|background|anonymous|exit|entry|experience|satisfaction|evaluation|welcome|intro) surveys? | surveys? $| surveys? \d+ "
    r"| questionnaires? | poll everywhere | (class|course|anonymous|in class) polls? | polls? $| polls? \d+ "
    r"| course (evaluations?|feedback) | (mid ?(semester|term|quarter)|end of (term|semester|course)) (evaluation|feedback)s? "
    r"| feedback (form|survey) | attendance | sign in sheet | introduce yourself | introductions? (post|discussion|survey|video|thread) "
    r"| getting to know | student info(rmation)? (sheet|form|card)? | consent (form|survey|agreement) "
    r"| syllabus (quiz|acknowledgements?|acknowledgments?|agreement|contract|check|scavenger hunt|signature|sign off) | syllabus $"
    r"| academic integrity | honou?r (code|pledge|statement) | plagiarism (quiz|tutorial|module|certificate|test) "
    r"| (lab )?safety (quiz|contract|training|agreement|test) | orientation (quiz|module|survey|activity|assignment|checklist) "
    r"| course orientation | start here | welcome (quiz|survey|assignment|activity|module|discussion) "
    r"| (lock ?down|respondus|honorlock|proctorio|proctor ?u|examity)( browser)? (practice|test|setup|set up|check|tutorial|trial|demo) "
    r"| (practice|setup|set up|trial|demo|tutorial) (quiz |exam |test )?(for |with |using )?(the )?(lock ?down|respondus|honorlock|proctorio|proctor ?u|examity) "  # (no bare 'test')
    r"|^ test (the |your )?(lock ?down|respondus|honorlock|proctorio|proctor ?u|examity) "
    rf"| {A_NOUN}{NUMBER} (sign ?up|scheduling|conflict form|conflict request|accommodations?|registration|key|keys|solutions?|answers"
    r"|statistics|stats|results|regrades?"
    r"|answer keys?|curves?|curve adjustments?|adjustments?|appeals?|grade appeals?|disputes?|extra credit|bonus|bonus points?"
    r"|bonus questions?|ec|room assignments?|seating|seating charts?|seating assignments?|exemptions?|exempt) "
    rf"| (sign ?up|conflict form|accommodation|registration|regrade request) (for )?(the )?{A_NOUN} "
    r"| regrades? | (retake|regrade|extension|accommodation|conflict|make ?up) requests? | pre ?tests? | pre ?assessments? "
    r"| (retake|reassessment|re assessment|make ?up|redo|exemption|conflict|accommodation|absence) (forms?|requests?|applications?|permission|contracts?|slips?|tickets?) "
    r"| request (a |an |for )?(retake|regrade|extension|make ?up|reassessment) "
    r"| diagnostic (exam|test|quiz|assessment|survey) | placement (exam|test|quiz|assessment) | exit (ticket|slip)s? "
    r"| bell ?ringers? | bell ?work | do now | participation (grade|points|credit|quiz|week|check|activity|log)s? | participation \d+ | participation $"
    r"| i ?clickers? | clickers? | top ?hat | pre ?lab | (final|midterm) (course )?(grade|score|mark|average)s? | absences? | check ?ins? "
    r"| academic (honesty|dishonesty|misconduct) | (ai|chatgpt|generative ai|gen ai|artificial intelligence)( use| usage| tools?)? "
    r"(policy|policies|agreement|acknowledg(e)?ments?|pledge|statement|quiz) | getting started (quiz|module|survey|checklist|assignment) "
    r"| course (tour|policies|policy|overview|expectations|navigation) | (policy|policies) (quiz|agreement|acknowledg(e)?ments?|contract) "
    r"| acknowledg(e)?ments? | quiz (bowl|show|night|game)s? | pub quiz | trivia "
    r"| (score|grade|points?) (adjustments?|changes?|appeals?|disputes?|corrections?) "
    r"| (training|certification|certificate|compliance|tutorial)( module)? (quiz|quizzes|test|tests|exam|exams|assessment)s? "
    r"| citi | title ix | alcohol ?edu | haven | mandated reporter ")
SYLLABUS_ANY = R(r" syllabus ")
CONTENT_REF = R(r" (chapters?|ch|units?|modules?|lessons?|lectures?|weeks?|sections?) (\d+|[ivx]+) ")
AFTER = R(
    rf" {A_NOUN}( exam)?{NUMBER} (corrections?|reflections?|wrappers?|analysis|revisions?|redemption|remediations?) "
    rf"| (corrections?|reflections?|wrappers?|analysis|remediation) (on|for|of) (the |your )?{A_NOUN} "
    r"| post (exam|exams|quiz|quizzes|midterm|midterms|final|finals|prelim|prelims) | exam wrappers? "                # no 'post test'
    r"| self (assessment|evaluation|grading|grade|test)s? | peer (assessment|review|evaluation|grading)s? | post ?mortem | retrospective ")
AFTER_LABEL = R(r" (corrections?|reflections?|wrappers?|analysis|revisions?|redemption|remediations?) ")
PREP_HARD = R(
    rf" study guides? | practice( [a-z]+)?{NUMBER} {A_NOUN} | practice (problems|questions|set|sets|assessment|round)s? | {A_NOUN}{NUMBER} practice "
    rf"| mock( [a-z]+){{0,2}} {A_NOUN} | sample {A_NOUN} | sample questions | cheat sheet | crib sheet | formula sheet | equation sheet "
    r"| note ?cards? | index cards? | notes? sheet | flash ?cards? | quizlet | study (plan|session|set|questions|guide) "
    r"| quiz yourself | warm ?ups? | kahoot | blooket | gimkit ")
PRACTICE_FIELD = R(r" (clinical|professional|nursing|evidence based|medical|legal|ethical|best|teaching|advanced|pharmacy|dental"
                   r"|general|family|private|community|scope of|standards of) practices? ")   # 'practice' as a field
PREP_LABEL = R(r" (practice|mock|study guide|cheat sheet|formula sheet) ")
PREP_SOFT = R(r" review | prep | preparation | jam | cram | cramming | boot ?camp | blitz ")
ABOUT = R(rf" {A_NOUN}{NUMBER} (info|information|details|instructions|guidelines|policy|policies|logistics|overview) ")
_DELIV = (r"project|paper|essay|report|presentation|portfolio|draft|proposal|poster|video|podcast|journal|outline"
          r"|bibliography|write ?up|case study|memo|blog|homework|hw|problem sets?|psets?|worksheet"
          )
_DELIV2 = r"critique|crit|conference|recital|showcase|exhibition|exhibit|seminar|debate|speech|performance task|socratic"
CONFLICT = R(rf" ({_DELIV})s? | lab (report|notebook|write ?up)s? ")
CONFLICT_AFTER = R(rf" {A_NOUN}( [a-z0-9]+)* ({_DELIV2})s? ")   # only AFTER the test noun ('Midterm Critique', not 'Speech Science Exam')
HW_GENERAL = R(rf" ({_DELIV}|{_DELIV2}|exercise|activity|discussion|forum|response|annotation|lab report|notebook|reflection"
               r"|problems|questions|drill|composition|composicion|gallery walk)s? | labs? \d+ | labs? $| assignment \d+ "
               r"|^ \d+ \d+ | (section|sec|ch|chapter) \d+ \d+ ")
CODE = R(r" unit tests | unit test (your|the|my|each|all|every|it|this|these|that) | tests? (cases?|suites?|coverage|harness|plans?|driven|files?) "
         r"| testing | write (unit )?tests? | tests? for | pytest | junit | jest | tdd "
         r"| autograde[rd]?s? | hidden tests? | public tests? | test results? ")
AUTHOR = R(r" (create|write|make|design|build|author|draft|generate)( [a-z0-9]+){0,4} (quiz|quizzes|test|tests|exam|exams|questions?) ")
EXTRA = R(r" extra credit | bonus | optional ")
MAKEUP = R(r" make ?up | conflict (exam|test|quiz) | alternate (exam|sitting) | deferred | absent | excused ")
RETAKE = R(r" re ?takes? | redo | re ?tests? | second chance ")
OBJ = R(rf" (for|before|after|about|toward|towards|ahead of|prior to|until|till) (the |your |our |next |upcoming |this |each |all )?{A_NOUN} ")
LABEL_FILLER = {"form", "forms", "sheet", "assignment", "activity", "due", "optional", "extra", "credit", "for", "the", "and",
                "on", "of", "your", "part", "submission", "upload", "packet", "session", "sessions", "1", "2", "3", "4", "5",
                "exam", "test", "quiz", "midterm", "final", "time", "game", "hw", "homework",
                "version", "versions", "mode", "round", "attempt", "copy", "only"}
D_PROCTOR = [re.compile(p) for p in (
    r"lock ?down browser", r"respondus", r"honorlock", r"proctorio", r"proctor ?u\b", r"examity", r"examsoft",
    r"\bproctor(ed|ing)?\b", r"webcam", r"closed[- ]book", r"closed[- ]notes?", r"no notes", r"one attempt",
    r"single attempt", r"time limit", r"\btimed\b", r"\d+ minutes to (complete|finish)", r"you will have \d+ (minutes|hours)",
    r"cheat sheet", r"one (page|sheet) of notes", r"formula sheet", r"scantron", r"blue ?books?", r"testing center",
    r"bring (a |your )?(pencil|calculator|student id|id card)", r"honor pledge")]
D_NOT_TIMED = re.compile(r"\b(no|without an?|not|isn't|is not) (a )?(time limit|timer|timed)\b|\buntimed\b")
D_SCOPE = re.compile(r"\b(covers?|covering|cumulative|comprehensive|material from|topics? covered|will cover)\b"
                     r"|\b(chapters?|units?|modules?|lectures?|weeks?|sections?) \d+\s*(-|–|to|through|and|&)\s*\d+")
D_PRODUCT = re.compile(r"word count|\b\d{3,4} words\b|double[- ]spaced|\bmla\b|\bapa\b|chicago style|works cited|citations?\b"
                       r"|bibliography|submit (a|your) (pdf|file|document|paper|essay|draft)|upload (a|your) (pdf|file|document|paper|essay|draft)")
# practice/ungraded must describe THIS item, not mention a practice exam elsewhere.
D_PRACTICE = re.compile(
    r"\b(this|it)( \w+)? (is|will be|are) (only )?(for practice|practice only|ungraded|optional|graded (on|for) completion)"
    r"|\b(this|it)( \w+)? (is not|isn't|will not be|won't be|does not|doesn't|do not|don't) (be )?(graded|count)"
    r"|\bungraded\b(?! (practice )?(exam|quiz|test)s? (is|are|will be) (posted|available))"
    r"|completion (grade|credit)|graded (on|for) completion|full credit for (any|completing|completion)"
    r"|any attempt receives full credit|unlimited attempts")

G_ASSESS = R(r" (exams?|examinations?|tests?|midterms?|finals?|final exams?|semester exams?|quiz|quizzes|prelims?"
             r"|unit tests?|(knowledge|concept|reading|comprehension) checks?|pruebas?|examenes|examens|klausuren|practicals?|lab practicals?) ")
G_GENERIC = R(r" (assessments?|summative|major grades?|major assessments?|evaluaciones) ")
G_HW = R(r" (homework|hw|assignments|programming|machine problems?|problem sets?|psets?|labs?|lab reports?|projects?|papers?|essays?"
         r"|writing|discussions?|reading responses?|journals?|reflections?|presentations?|portfolios?|activities|classwork"
         r"|in class work|worksheets?|exercises?|speeches|critiques?) ")
G_NOSTAKES = R(r" (participation|attendance|extra credit|bonus|practice|engagement|surveys?|ungraded|clickers?|polls?|completion"
               r"|review|prep|preparation|corrections?|wrappers?|remediation|study) ")
G_FINAL = R(r" finals? | final exams? | semester exams? ")
G_MIDTERM = R(r" mid ?terms? | prelims? ")
DEFAULT_GROUPS = {" assignments ", " assignment ", " imported assignments ", " ungraded ", " other "}

EVIDENCE = {"n_lockdown", "s_quiz_engine", "s_window", "g_assess", "g_mixed", "d_proctor", "e_duration"}   # no s_in_person
PRODUCT_TYPES = {"online_upload", "online_text_entry", "online_url", "media_recording", "student_annotation"}

EV_SESSION = R(r" review | office hours? | help (session|desk|hours)s? | study (session|hall|group|night)s? | tutoring | tutor "
               r"| q and a | q a | supplemental instruction | si | workshop | recitation | discussion section | lecture \d+ "
               r"| no (class|lecture|lab|section|recitation|discussion) | cancel(l)?ed | cancel(l)?ation "
               r"| reading (day|days|period) | (exam|exams|finals?|midterms?) (week|weeks|period|season|schedule|days) "
               r"| deadline | due | drop | withdraw "
               r"| jam | cram | cramming | boot ?camp | blitz | marathon | party | social | conferences? | meetings? | appointments? "
               r"| (grades?|scores?|results) (are |will be )?(posted|released|returned|available|published|due|in) "
               r"| (posted|released|returned|published|graded) $")


# ------------------------------------------------------------------ data

@dataclass
class Item:
    name: str
    submission_types: list = field(default_factory=list)
    is_quiz: bool = False
    points_possible: float | None = None
    grading_type: str | None = "points"
    omit_from_final_grade: bool = False
    unlock_at: datetime | None = None
    lock_at: datetime | None = None
    due_at: datetime | None = None
    description_html: str | None = None
    rubric: list | None = None
    group_name: str | None = None
    group_count: int = 2
    share: float | None = None
    user_kind: str | None = None
    inherited_kind: str | None = None
    is_event: bool = False
    start_at: datetime | None = None
    end_at: datetime | None = None
    all_day: bool | None = None


@dataclass
class Result:
    kind: str
    confidence: str
    score: float
    reasons: list
    hints: dict = field(default_factory=dict)

    @property
    def is_assessment(self):
        return self.kind != "none"


def grade_shares(assignments, groups, group_weighting=None) -> dict:
    countable = [a for a in assignments if (a["points_possible"] or 0) > 0 and not a.get("omit_from_final_grade")]
    gw = {g["id"]: (g.get("weight") or 0.0) for g in groups}
    weighted = group_weighting if group_weighting is not None else any(w > 0 for w in gw.values())
    out = {}
    if weighted:
        total_w = max(100.0, sum(gw.values()))
        per_group: dict = {}
        count: dict = {}
        for a in countable:
            per_group[a["group_id"]] = per_group.get(a["group_id"], 0.0) + a["points_possible"]
            count[a["group_id"]] = count.get(a["group_id"], 0) + 1
        names = {g["id"]: norm(g.get("name") or "") for g in groups}
        for a in countable:
            gid = a["group_id"]
            expect = 2 if re.search(r" (exams|tests|midterms|prelims) ", names.get(gid, "")) else \
                     4 if re.search(r" (quizzes|checks) ", names.get(gid, "")) else 1
            scale = max(count[gid], expect) / count[gid]
            out[a["id"]] = (gw.get(gid, 0.0) / total_w) * a["points_possible"] / (per_group[gid] * scale)
    else:
        total = sum(a["points_possible"] for a in countable)
        for a in countable:
            out[a["id"]] = a["points_possible"] / total if total else 0.0
    return out


# ------------------------------------------------------------------ helpers

STEM_DROP = R(r" (part|pt|section|sec|version|form|day) ([a-d]|\d+|[ivx]+) ")


def stem(name: str, sibling: bool = False) -> str:
    """Family key. modifiers (extra credit/bonus/optional, make-up, retake, practice) stay in the key so one
    'Not a test' on 'Quiz 6 (Extra Credit)' doesn't silence every 'Quiz #'. sibling=True also drops part/section/version
    tokens so 'Exam 1 Part A' and 'Exam 1 Part B' collapse into one planner item."""
    raw = LOCKDOWN_SUFFIX.sub("", name or "")
    h, _ = split_name(raw)
    base = h if re.search(r"\d", h) else raw
    s = norm(base)
    if sibling:
        s = STEM_DROP.sub(" ", s)
    s = re.sub(r" (\d+[a-z]?|[ivx]+) ", " # ", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    n = norm(raw)
    mods = [t for t, rx in (("+optional", EXTRA), ("+makeup", MAKEUP), ("+retake", RETAKE), ("+practice", PREP_LABEL)) if rx.search(n)]
    return " ".join([s] + mods)


def _final_alone(t: str) -> bool:
    for m in re.finditer(r" (finals?) ", t):
        prev = t[:m.start()].split()[-1:] or [""]
        if prev[0] in {"semi", "quarter"}:
            continue
        after = t[m.end():].split()
        if all(w in FINAL_OK or re.fullmatch(r"\d+[a-z]?|[ivx]+", w) for w in after):   # allowlist
            return True
    return False


def _detopic(t: str) -> str:
    return EXAM_TOPIC.sub(" ", TEST_TOPIC.sub(" ", t))


def _keywords(t: str, code_ctx: bool) -> dict:
    kw = {}
    d = _detopic(t)
    if FINAL_EXAM.search(t):
        kw["final_exam"] = 5.5
    elif _final_alone(t):
        kw["final"] = 4.0
    if MIDTERM.search(t):
        kw["midterm"] = 5.0
    if EXAM.search(d):
        kw["exam"] = 4.5
    if TEST.search(d) and not code_ctx:
        kw["test"] = 4.0
    if PRACTICAL.search(t):
        kw["practical"] = 4.0
    if ASSESSMENT.search(t):
        kw["assessment"] = 3.0
    if QUIZ.search(t):
        kw["quiz"] = 4.0
    if WEAK.search(t):
        kw["weak"] = 2.0
    return kw


CORE = {"final_exam", "final", "midterm", "exam", "test", "practical", "quiz"}


def _label_only(regex: re.Pattern, sub: str):
    toks = [t for t in sub.split() if t not in LABEL_FILLER]
    if not 1 <= len(toks) <= 3:
        return None
    rest = f" {' '.join(toks)} "
    m = regex.search(rest)
    return m if m and m.group(0).strip() == rest.strip() else None


# ------------------------------------------------------------------ classifier

def classify(it: Item) -> Result:
    reasons: list = []
    hints: dict = {}

    def add(w, code, text):
        reasons.append((w, code, text))

    if it.user_kind:
        k = it.user_kind if it.user_kind in KINDS else "none"
        return Result(k, "user", 0.0, [(0, "user", "you marked this")], hints)
    if it.inherited_kind:
        k = it.inherited_kind if it.inherited_kind in KINDS else "none"
        return Result(k, "high", 0.0, [(0, "inherit", "you marked another item like this")], hints)

    raw = it.name or ""
    lockdown_title = bool(LOCKDOWN_SUFFIX.search(raw))
    raw_clean = LOCKDOWN_SUFFIX.sub("", raw)
    head_raw, sub_raw = split_name(raw_clean)
    n, head, sub = norm(raw_clean), norm(head_raw), norm(sub_raw)

    head_code = bool(CODE.search(head))
    head_kw = _keywords(head, head_code)
    code_ctx = head_code or (bool(CODE.search(n)) and not (head_kw.keys() & CORE))
    kw = _keywords(n, code_ctx)
    for k, w in _keywords(head, code_ctx).items():      # union with the head ('Anatomy Practical: Skeletal System')
        kw.setdefault(k, w)
    scope = head if (head_kw.keys() & (CORE | {"assessment", "weak"})) else n
    use_label = scope is head and bool(sub.strip())
    # mirror of the subtitle label rule: the HEAD is just a label and the test word is in the subtitle
    head_label = scope is n and bool(sub.strip()) and bool(head.strip())
    prep_scope = PRACTICE_FIELD.sub(" field ", scope)
    def neg(regex, label_regex=None, s=None):
        m = regex.search(s or scope)
        if m:
            return m
        if use_label:
            return _label_only(label_regex or regex, sub)
        if head_label:
            return _label_only(label_regex or regex, head)
        return None

    # hard exclusions
    if (m := neg(HARD_ADMIN)) or (SYLLABUS_ANY.search(scope) and not CONTENT_REF.search(scope) and (m := SYLLABUS_ANY.search(scope))):
        return Result("none", "high", -99.0, [(-99, "x_admin", f"'{m.group(0).strip()}' is admin/survey/participation, not a test")], hints)
    if "discussion_topic" in (it.submission_types or []):
        return Result("none", "high", -99.0, [(-99, "x_discussion", "Canvas discussion")], hints)
    if (m := neg(AFTER, AFTER_LABEL)):
        return Result("none", "high", -99.0, [(-99, "x_after", f"'{m.group(0).strip()}' is work done after a test")], hints)
    if (m := neg(PREP_HARD, PREP_LABEL, prep_scope)):    # events too ('Mock Exam (in class)', 'Study Guide Posted')
        hints["prep_for"] = bool(kw)
        return Result("none", "high", -99.0, [(-99, "x_prep", f"'{m.group(0).strip()}' is practice/prep material")], hints)

    if kw:
        best = max(kw, key=kw.get)
        add(kw[best], f"n_{best}", f"name says '{best.replace('_', ' ')}'")
    core = kw.keys() & CORE
    if lockdown_title or LOCKDOWN_WORDS.search(n):
        add(3.0, "n_lockdown", "title says it requires LockDown Browser")
    if code_ctx and TEST.search(n) and "test" not in kw:
        add(-1.0, "n_code", "'test' here means code tests")

    negated = cap_medium = False
    if not it.is_event and (m := neg(PREP_SOFT)):
        add(-6.0, "x_review", f"'{m.group(0).strip()}' is usually review for a test"); negated = cap_medium = True
        hints["prep_for"] = bool(kw)
    if AUTHOR.search(n):
        add(-5.0, "x_author", "the student writes the quiz"); negated = True
    if kw and ((m := neg(CONFLICT)) or (m := CONFLICT_AFTER.search(scope))):
        add(-5.0, "x_deliverable", f"'{m.group(0).strip()}' is a product to hand in"); negated = True
    elif not kw and (m := HW_GENERAL.search(n)):
        add(-2.5, "n_hw", f"name says '{m.group(0).strip()}'")
    if kw and ABOUT.search(head):
        add(-2.0, "x_about", "an info item about a test"); negated = True
    # the test noun is only the object of a preposition ('Key Terms for Exam 2', 'open late for finals')
    if kw and not MAKEUP.search(n) and not RETAKE.search(n) and (m := OBJ.search(scope)):
        first = re.search(rf" {A_NOUN} ", scope)
        if first and first.start() >= m.start():
            add(-2.0, "x_object", f"'{m.group(0).strip()}': about a test, not the test"); negated = True
    if EXTRA.search(n):
        add(-1.0, "n_extra", "extra credit / optional"); hints["optional"] = True
    if MAKEUP.search(n):
        add(-1.0, "n_makeup", "make-up / conflict sitting (may not apply to you)"); hints["makeup"] = True
    if RETAKE.search(n):
        hints["retake"] = True

    hard_struct = False
    if not it.is_event:
        types = set(it.submission_types or [])
        if it.is_quiz or "online_quiz" in types:
            add(3.0, "s_quiz_engine", "Canvas quiz")
        if types and types <= {"on_paper", "none"}:
            add(1.0, "s_in_person", "nothing submitted online (in person)")
        if types & PRODUCT_TYPES and "online_quiz" not in types:
            add(-1.5, "s_product", "you upload/type a submission")
        if "external_tool" in types and (core or "assessment" in kw):
            add(1.0, "s_lti_kw", "external tool (New Quizzes/Gradescope) named like a test")
        if "wiki_page" in types:
            add(-4.0, "s_wiki", "page submission"); hard_struct = True
        if it.grading_type == "not_graded" or "not_graded" in types:
            add(-1.0, "s_not_graded", "not graded")
        elif it.grading_type == "pass_fail":
            add(-1.0, "s_pass_fail", "complete/incomplete")
        if it.omit_from_final_grade:
            add(-2.0, "s_omit", "doesn't count toward the grade")
        if not it.points_possible and it.grading_type != "not_graded":
            add(-1.0, "s_zero", "worth 0 points")
        if it.rubric:
            add(-1.0, "s_rubric", "graded with a rubric")
        end = it.lock_at or it.due_at
        if it.unlock_at and end and timedelta(0) < end - it.unlock_at <= timedelta(hours=36):
            add(1.5, "s_window", "open for 36 hours or less")
        gname = norm(it.group_name or "")
        if it.group_name and it.group_count > 1 and gname not in DEFAULT_GROUPS:
            g_nolab = re.sub(r" lab practicals? ", " practicals ", gname)
            a, h, z, gen = G_ASSESS.search(gname), G_HW.search(g_nolab), G_NOSTAKES.search(gname), G_GENERIC.search(gname)
            if z:
                add(-3.0, "g_nostakes", f"in group '{it.group_name}'")
            elif a and not h:
                add(3.0, "g_assess", f"in group '{it.group_name}'")
            elif gen and not h:
                add(2.0, "g_generic", f"in group '{it.group_name}'")
            elif (a or gen) and h:
                add(1.0, "g_mixed", f"in group '{it.group_name}'")
            elif h:
                add(-2.0, "g_hw", f"in group '{it.group_name}'")
        desc = strip_html(it.description_html)
        if desc:
            d_timed = D_NOT_TIMED.sub(" ", desc)
            hits = {p.pattern for p in D_PROCTOR if p.search(d_timed)}
            if hits:
                add(3.0 if len(hits) >= 2 else 2.0, "d_proctor", "instructions mention proctoring/time limit/closed book")
            if (m := D_SCOPE.search(desc)):
                add(0.5, "d_scope", "instructions say what it covers"); hints["scope"] = m.group(0)
            if D_PRODUCT.search(desc):
                add(-1.5, "d_product", "instructions ask for a written product")
            if D_PRACTICE.search(desc):
                add(-2.5, "d_practice", "instructions say practice/ungraded/completion"); negated = cap_medium = True
        evidence = bool(kw) or any(c in EVIDENCE for _, c, _ in reasons)
        if it.share is not None and it.points_possible:
            s = it.share
            w = 2.5 if s >= 0.15 else 1.5 if s >= 0.08 else 0.5 if s >= 0.04 else (-1.0 if 0 < s < 0.0075 else 0.0)  # spec: 0 < s
            if w > 0 and not evidence:
                add(0.0, "w_no_evidence", f"{s:.0%} of the grade, but nothing says it's a test")
            elif w:
                add(w, "w_share", f"{s:.1%} of the grade")
    else:
        if (m := neg(EV_SESSION)):
            add(-6.0, "e_session", f"'{m.group(0).strip()}' is a session/notice, not the test"); negated = True
        if it.all_day:
            add(-1.0, "e_allday", "all-day event")
        elif it.start_at and it.end_at:
            mins = (it.end_at - it.start_at).total_seconds() / 60
            if 40 <= mins <= 240:
                add(1.0, "e_duration", f"{int(mins)}-minute event")
            elif it.all_day is None and (mins <= 0 or mins >= 23 * 60):   # spec: only rows synced before all_day existed
                add(-1.0, "e_allday", "all-day / date-only event")

    score = round(sum(w for w, *_ in reasons), 2)
    counts = it.is_event or ((it.points_possible or 0) > 0 and not it.omit_from_final_grade)
    if core and not negated and not hard_struct and counts and score < MEDIUM:
        add(0.0, "floor", "name is a test word with nothing against it")
        score = MEDIUM

    if score < MEDIUM:
        return Result("none", "high" if score < 0 else "low", score, reasons, hints)
    conf = "high" if score >= HIGH and not cap_medium else "medium"
    return Result(_subtype(it, kw, head, n, code_ctx), conf, score, reasons, hints)


QUIZ_NOUNS = {"quiz", "quizzes", "check", "checks", "checkpoint", "checkpoints", "quest", "quests"}
TEST_NOUNS = {"exam", "exams", "examination", "test", "tests", "final", "finals", "midterm", "midterms", "prelim", "prelims",
              "practical", "practicals", "assessment", "assessments", "examen", "prueba", "pruebas", "klausur", "mt"}


def _subtype(it: Item, kw: dict, head: str, n: str, code_ctx: bool) -> str:
    hd = _detopic(head)
    hk = set()
    if FINAL_EXAM.search(head) or _final_alone(head):
        hk.add("final")
    if MIDTERM.search(head):
        hk.add("midterm")
    if EXAM.search(hd) or (TEST.search(hd) and not code_ctx) or PRACTICAL.search(head) or ASSESSMENT.search(head):
        hk.add("test")
    if QUIZ.search(head) or WEAK.search(head):
        hk.add("quiz")
    if not hk:
        if kw.keys() & {"final_exam", "final"}:
            hk.add("final")
        if "midterm" in kw:
            hk.add("midterm")
        if kw.keys() & {"exam", "test", "practical", "assessment"}:
            hk.add("test")
        if kw.keys() & {"quiz", "weak"}:
            hk.add("quiz")
    # head noun = the LAST test noun ('Final Exam Prep Quiz' -> quiz, 'Quiz-format Exam' -> test)
    if "quiz" in hk and hk - {"quiz"}:
        toks = (hd if hk & {"quiz"} and (QUIZ.search(head) or WEAK.search(head)) else n).split()
        last = None
        for t in toks:
            if t in QUIZ_NOUNS:
                last = "quiz"
            elif t in TEST_NOUNS:
                last = "test"
        if last == "quiz":
            hk = {"quiz"}
        elif last == "test":
            hk.discard("quiz")
    g = norm(it.group_name or "")
    final_group = bool(it.group_name) and bool(G_FINAL.search(g)) and not G_HW.search(g)
    midterm_group = bool(it.group_name) and bool(G_MIDTERM.search(g))
    if "final" in hk:
        return "final"
    if "midterm" in hk:
        return "midterm"
    if "test" in hk:
        if kw.keys() & {"final", "final_exam"} or final_group:
            return "final"
        if "midterm" in kw or midterm_group:
            return "midterm"
        only = kw.keys() - {"weak"}
        if (only == {"assessment"} and (it.is_quiz or "external_tool" in (it.submission_types or [])) and (it.share or 0) < 0.05) \
                or (only == {"test"} and re.search(r" pruebas? ", n) and (it.share or 0) < 0.05):   # 'Prueba de vocabulario'
            return "quiz"
        return "test"
    if "quiz" in hk:
        return "quiz"
    if final_group:
        return "final"
    if midterm_group:
        return "midterm"
    if it.group_name and re.search(r" (exams?|tests?|examinations?|summative|examenes|pruebas?) ", g):
        return "test"
    if it.group_name and re.search(r" quiz(zes)? | checks? ", g):
        return "quiz"
    return "test" if (it.share or 0) >= 0.08 else "quiz"


def apply_inheritance(items: list[Item], overridden: dict[str, str]) -> None:
    for it in items:
        if not it.user_kind and (k := overridden.get(stem(it.name))):
            it.inherited_kind = k


NUM = re.compile(r" (\d+|[ivx]+) ")


def _num(name):
    m = NUM.search(norm(name))
    return m.group(1) if m else None


def merge(assignments, events, days: int = 3):
    used, out = set(), []
    for ev, er in events:
        if not er.is_assessment:
            continue
        best, best_key = None, None
        for i, (a, ar) in enumerate(assignments):
            if i in used or not ar.is_assessment:
                continue
            close = a.due_at is None or abs((a.due_at - ev.start_at).total_seconds()) <= days * 86400
            n_a, n_e = _num(a.name), _num(ev.name)
            same_num = n_a is not None and n_a == n_e
            same_kind = ar.kind == er.kind
            compatible = (ar.kind == "quiz") == (er.kind == "quiz")  # 'Quiz 2' is not the 'Exam 2' event
            if not close or not compatible or not (same_num or (same_kind and (n_a is None or n_e is None))):
                continue
            key = (same_num, same_kind, -abs((a.due_at - ev.start_at).total_seconds()) if a.due_at else -1e12)
            if best_key is None or key > best_key:
                best, best_key = i, key
        if best is None:
            out.append((None, ev))
        else:
            used.add(best)
            out.append((assignments[best][0], ev))
    out += [(a, None) for i, (a, ar) in enumerate(assignments) if ar.is_assessment and i not in used]
    return out


# ------------------------------------------------------------------ the app's data


def sibling_key(name: str) -> str:
    """Parts of one sitting share this key ('Exam 1 Part A' / 'Part B'); different numbers don't
    ('Chapter 4 Quiz' / 'Chapter 5 Quiz'), nor different tests in one unit ('Week 9: Exam')."""
    raw = LOCKDOWN_SUFFIX.sub("", name or "")
    h, sub = split_name(raw)
    label_only = bool(CONTENT_REF.fullmatch(norm(h))) and bool(sub.strip())
    base = h if re.search(r"\d", h) and not label_only else raw
    return re.sub(r"\s+", " ", STEM_DROP.sub(" ", norm(base))).strip()


def makeup_key(name: str) -> str:
    """'Exam 2 (Make-up)', 'Makeup Exam 2' and 'Exam 2' share this; 'Exam 3' doesn't."""
    n = MAKEUP.sub(" ", norm(LOCKDOWN_SUFFIX.sub("", name or "")))
    n = re.sub(r" (sitting|session|version|exam room)s? ", " ", n)
    return re.sub(r"\s+", " ", STEM_DROP.sub(" ", n)).strip()


def family_id(family: str, natural_kind: str) -> str:
    """How a "same for everything like it" answer is stored: the series key plus the classifier's own
    kind for the item, so a 'No' on 'Week 3: Reading Check' can't hide 'Week 9: Midterm Exam', and a
    'Yes' on 'Exam 2' can't turn 'Exam 1: Corrections' into a test. Long keys are shortened stably."""
    key = f"{family}|{natural_kind}"
    if len(key) > 150:
        import hashlib

        key = f"{key[:120]}~{hashlib.sha1(key.encode()).hexdigest()[:16]}"
    return key


@dataclass
class Found:
    """A test, quiz or exam found in a student's synced Canvas data."""

    key: str  # "a:<assignment id>" or "e:<calendar event id>"
    title: str
    kind: str  # final / midterm / test / quiz (or none, with include_none)
    confidence: str  # user / high / medium
    when: datetime | None  # naive UTC; None when Canvas has no date
    course: object
    assignment: object = None
    event: object = None
    share: float | None = None  # fraction of the course grade, when known
    family: str = ""
    natural: str = ""  # what the classifier says without the student's answers
    reasons: list = field(default_factory=list)
    hints: dict = field(default_factory=dict)


def _choices(user_id: int) -> tuple[dict, dict]:
    from sqlalchemy import select

    from ..extensions import db
    from ..models import AssessmentChoice

    exact, family = {}, {}
    for c in db.session.scalars(select(AssessmentChoice).where(AssessmentChoice.user_id == user_id)):
        if c.item.startswith("family:"):
            family[(c.course_id, c.item[7:])] = c.kind
        else:
            exact[c.item] = c.kind
    return exact, family


def _judge(it: Item, course_id: int, key: str, exact: dict, family: dict) -> tuple[Result, Result]:
    """(the result to use, the classifier's own result). The student's exact answer wins; a series
    answer applies only to items the classifier puts in the same kind."""
    natural = classify(it)
    it.user_kind = exact.get(key)
    it.inherited_kind = None if it.user_kind else family.get((course_id, family_id(stem(it.name), natural.kind)))
    return (classify(it) if (it.user_kind or it.inherited_kind) else natural), natural


DONE_STATUSES = {"graded", "submitted", "submitted_late", "excused"}


def _assignment_item(a, group_names: dict, n_groups: int, shares: dict) -> Item:
    return Item(name=a.name, submission_types=a.submission_types or [], is_quiz=bool(a.is_quiz),
                points_possible=a.points_possible, grading_type=a.grading_type,
                omit_from_final_grade=bool(a.omit_from_final_grade), unlock_at=a.unlock_at, lock_at=a.lock_at,
                due_at=a.due_at, description_html=a.description_html, rubric=a.rubric,
                group_name=group_names.get(a.group_canvas_id), group_count=n_groups, share=shares.get(a.id))


@dataclass
class Judged:
    """One assignment's verdict for the whole course (past items too): what it is, how sure, why."""
    kind: str  # final / midterm / test / quiz / none
    confidence: str  # user / high / medium / low
    family: str
    natural: str
    reasons: list
    share: float | None

    @property
    def is_test(self) -> bool:
        return self.kind in KINDS and self.confidence in ("user", "high", "medium")

    def why(self, limit: int = 2) -> str:
        if self.confidence == "user":
            return "you said so"
        reasons = sorted((r for r in self.reasons if r[0] > 0), key=lambda r: -r[0])[:limit]
        return "; ".join(text for _w, _code, text in reasons) or "looks like regular coursework"


def judge_course(user_id: int, course, assignments=None, groups=None) -> dict[int, Judged]:
    """Every assignment in one course, judged test or not, with the student's own answers winning
    (services/split.py's Brain Grade). Unlike find(), it covers past and graded work."""
    from sqlalchemy import select
    from sqlalchemy.orm import undefer_group

    from ..extensions import db
    from ..models import Assignment, AssignmentGroup

    if groups is None:
        groups = db.session.scalars(select(AssignmentGroup).where(AssignmentGroup.course_id == course.id)).all()
    if assignments is None:
        assignments = db.session.scalars(select(Assignment).options(undefer_group("assignment_detail"))
                                         .where(Assignment.course_id == course.id)).all()
    shares = grade_shares(
        [{"id": a.id, "points_possible": a.points_possible, "omit_from_final_grade": a.omit_from_final_grade,
          "group_id": a.group_canvas_id} for a in assignments],
        [{"id": g.canvas_id, "weight": g.weight, "name": g.name} for g in groups], course.group_weighting)
    group_names = {g.canvas_id: g.name for g in groups}
    exact, family = _choices(user_id)
    out = {}
    for a in assignments:
        it = _assignment_item(a, group_names, len(groups), shares)
        r, natural = _judge(it, course.id, f"a:{a.id}", exact, family)
        out[a.id] = Judged(r.kind, r.confidence, stem(a.name), natural.kind, r.reasons, shares.get(a.id))
    return out


def find(user, days: int = 60, back_hours: int = 12, include_none: bool = False) -> list[Found]:
    """Upcoming assessments (and undated ones) across the student's visible courses, soonest first.
    With include_none, items judged "not a test" come back too (kind "none"), for the review list."""
    from sqlalchemy import select
    from sqlalchemy.orm import undefer_group

    from .. import queries
    from ..extensions import db
    from ..models import Assignment, AssignmentGroup, CalendarEvent, utcnow

    now = utcnow()
    lo, hi = now - timedelta(hours=back_hours), now + timedelta(days=days)
    courses = {c.id: c for c in queries.visible_courses(user.id)}
    if not courses:
        return []
    exact, family = _choices(user.id)
    out: list[Found] = []
    for course in courses.values():
        groups = db.session.scalars(select(AssignmentGroup).where(AssignmentGroup.course_id == course.id)).all()
        everything = db.session.scalars(select(Assignment).where(Assignment.course_id == course.id)).all()
        shares = grade_shares(
            [{"id": a.id, "points_possible": a.points_possible, "omit_from_final_grade": a.omit_from_final_grade,
              "group_id": a.group_canvas_id} for a in everything],
            [{"id": g.canvas_id, "weight": g.weight, "name": g.name} for g in groups], course.group_weighting)
        group_names = {g.canvas_id: g.name for g in groups}

        def undated_and_done(a) -> bool:  # last month's graded exam with no date isn't coming up
            return a.due_at is None and (a.status in DONE_STATUSES or a.score is not None or a.submitted_at is not None)

        window = [a for a in everything if (a.due_at is None or lo <= a.due_at <= hi) and not undated_and_done(a)]
        if window:  # their instructions are encrypted and deferred: load just these
            ids = [a.id for a in window]
            window = db.session.scalars(select(Assignment).options(undefer_group("assignment_detail"))
                                        .where(Assignment.id.in_(ids))).all()
        pairs = []  # (item, result, natural result, assignment)
        for a in window:
            it = _assignment_item(a, group_names, len(groups), shares)
            r, natural = _judge(it, course.id, f"a:{a.id}", exact, family)
            pairs.append((it, r, natural, a))
        events = []
        for e in db.session.scalars(select(CalendarEvent).where(
                CalendarEvent.user_id == user.id, CalendarEvent.course_id == course.id,
                CalendarEvent.start_at >= lo, CalendarEvent.start_at <= hi)):
            it = Item(name=e.title, is_event=True, start_at=e.start_at, end_at=e.end_at, all_day=e.all_day)
            r, natural = _judge(it, course.id, f"e:{e.id}", exact, family)
            events.append((it, r, natural, e))

        # An exam often exists twice: the gradebook assignment and the calendar event with the room.
        # Pair them using the classifier's own view, so a "Not a test" on the assignment also hides
        # its event instead of the event coming back on its own.
        suppressed = {id(it) for it, r, nat, _ in pairs + events if not r.is_assessment and nat.is_assessment}
        merged = merge([(it, nat if id(it) in suppressed else r) for it, r, nat, _ in pairs],
                       [(it, nat if id(it) in suppressed else r) for it, r, nat, _ in events])
        by_item = {id(it): (r, nat, a) for it, r, nat, a in pairs}
        ev_by_item = {id(it): (r, nat, e) for it, r, nat, e in events}
        seen = set()
        for a_it, e_it in merged:
            if (a_it is not None and id(a_it) in suppressed) or (e_it is not None and id(e_it) in suppressed):
                continue  # the student said it isn't a test (with include_none it's listed as such below)
            if a_it is not None:
                r, nat, a = by_item[id(a_it)]
                ev = ev_by_item[id(e_it)][2] if e_it is not None else None
                seen.add(id(a_it))
                when = a.due_at or (ev.start_at if ev else None)
                out.append(Found(f"a:{a.id}", a.name, r.kind, r.confidence, when, course, a, ev, a_it.share,
                                 stem(a.name), nat.kind, r.reasons, r.hints))
            else:
                r, nat, ev = ev_by_item[id(e_it)]
                out.append(Found(f"e:{ev.id}", ev.title, r.kind, r.confidence, ev.start_at, course, None, ev, None,
                                 stem(ev.title), nat.kind, r.reasons, r.hints))
        if include_none:
            for it, r, nat, a in pairs:
                if id(it) not in seen and not r.is_assessment:
                    out.append(Found(f"a:{a.id}", a.name, "none", r.confidence, a.due_at, course, a, None, it.share,
                                     stem(a.name), nat.kind, r.reasons, r.hints))
    # Low confidence never reaches the planner; "medium" asks once.
    out = [f for f in out if f.kind == "none" or f.confidence in ("user", "high", "medium")]
    return sorted(out, key=lambda f: (f.when is None, f.when or now, f.title))


def why(found: Found, limit: int = 3) -> str:
    """The top reasons, in words, for the "why do we think this is a test?" tooltip."""
    reasons = sorted((r for r in found.reasons if r[0] > 0), key=lambda r: -r[0])[:limit]
    return "; ".join(text for _w, _code, text in reasons) or "you said so"
