"""Demo data: a realistic fake Canvas snapshot, so the app can be explored without a school account.

`flask seed-demo` creates the account demo / demo12345 and runs this snapshot through the
same ingest code the browser extension uses.
"""

from __future__ import annotations

import io
from datetime import timedelta

from .models import utcnow

HOST = "https://demo-university.instructure.com"


def _iso(days: float, hour: int = 23, minute: int = 59) -> str:
    t = (utcnow() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t.isoformat() + "Z"


def _assignment(aid, name, group, due, points, status="upcoming", score=None, late=False, kind="online_upload",
                desc=None, quiz=False, submitted=None, missing=None, deducted=None):
    """`missing` marks graded work Canvas still calls missing (a zero for work never turned in);
    `deducted` is points Canvas's late policy took off."""
    missing = status == "missing" if missing is None else missing
    sub = {"workflow_state": "graded" if status == "graded" else ("submitted" if status.startswith("submitted") else "unsubmitted"),
           "score": score, "late": late, "missing": missing, "points_deducted": deducted,
           "late_policy_status": "missing" if missing and status == "graded" else ("late" if deducted else None),
           "submitted_at": None if missing else (submitted or (_iso(due - 0.5) if status in {"graded", "submitted", "submitted_late"} else None))}
    return {"id": str(aid), "name": name, "group_id": group, "due_at": _iso(due), "points_possible": points,
            "submission_types": [kind], "status": status, "is_quiz": quiz,
            "html_url": f"{HOST}/courses/{str(aid)[:3]}/assignments/{aid}",
            "description_html": desc or f"<p>{name}. Show your work and cite sources where needed.</p>",
            "submission": sub}


CALC_NOTES = """Unit 3: Derivatives

The derivative measures instantaneous rate of change. Definition: f'(x) = lim h->0 (f(x+h) - f(x)) / h.

Power rule: d/dx x^n = n x^(n-1). Constant multiple and sum rules let us differentiate polynomials term by term.

Product rule: (fg)' = f'g + fg'. Quotient rule: (f/g)' = (f'g - fg') / g^2.

Chain rule: if y = f(g(x)) then dy/dx = f'(g(x)) * g'(x). Example: d/dx sin(x^2) = cos(x^2) * 2x.

Implicit differentiation: differentiate both sides with respect to x, treating y as a function of x, then solve for dy/dx.

Linearization: L(x) = f(a) + f'(a)(x - a) approximates f near x = a."""

HISTORY_NOTES = """Reconstruction (1865-1877)

Presidential Reconstruction under Andrew Johnson offered lenient terms to former Confederate states, which passed Black Codes restricting freed people.

Radical Republicans in Congress, led by Thaddeus Stevens and Charles Sumner, passed the Civil Rights Act of 1866 over Johnson's veto and the Reconstruction Acts of 1867, dividing the South into five military districts.

The 13th Amendment (1865) abolished slavery. The 14th Amendment (1868) guaranteed citizenship and equal protection. The 15th Amendment (1870) prohibited denying the vote based on race.

The Compromise of 1877 resolved the disputed 1876 election in favor of Rutherford B. Hayes and withdrew federal troops from the South, ending Reconstruction."""


def snapshot() -> dict:
    calc_id, hist_id, hist_disc, cs_id = "210", "320", "321", "430"
    calc = {
        "id": calc_id, "name": "Calculus I", "course_code": "MATH 1310-001", "class_key": "fa::calculus i",
        "on_dashboard": True, "term": {"id": "fa", "name": "Fall"}, "html_url": f"{HOST}/courses/{calc_id}",
        "grade": {"current_score": 88.4, "current_grade": "B+", "final_score": 51.2, "final_grade": "F"},
        "syllabus_html": "<h2>Grading</h2><ul><li>Homework 30% (lowest dropped)</li><li>Quizzes 20%</li><li>Exams 50%</li></ul>"
                         "<p>Late homework: 50% credit up to 2 days late.</p>",
        "files_tab_hidden": False,
        "assignment_groups": [
            {"id": "g1", "name": "Homework", "weight": 30, "position": 1, "drop_lowest": 1, "drop_highest": 0},
            {"id": "g2", "name": "Quizzes", "weight": 20, "position": 2, "drop_lowest": 0, "drop_highest": 0},
            {"id": "g3", "name": "Exams", "weight": 50, "position": 3, "drop_lowest": 0, "drop_highest": 0},
        ],
        "assignments": [
            _assignment(2101, "HW 1: Limits", "g1", -24, 20, "graded", 19),
            _assignment(2102, "HW 2: Continuity", "g1", -17, 20, "graded", 12, late=True, deducted=4),
            _assignment(2111, "HW 2.5: Related rates", "g1", -13, 20, "graded", 0, missing=True),
            _assignment(2113, "HW 3.5: Optimization", "g1", -8, 20, "graded", 0, missing=True),  # homework drops 1 zero
            _assignment(2112, "Quiz 2: Limits", "g2", -15, 10, "graded", 10, kind="online_quiz", quiz=True),
            _assignment(2103, "HW 3: Derivative rules", "g1", -10, 20, "graded", 18),
            _assignment(2104, "HW 4: Chain rule", "g1", -3, 20, "submitted"),
            _assignment(2105, "HW 5: Implicit differentiation", "g1", 2, 20,
                        desc="<p>Problems 3.5 #1-25 odd. Use implicit differentiation and check with Desmos.</p>"),
            _assignment(2106, "Quiz 3: Derivatives", "g2", -6, 10, "graded", 9, kind="online_quiz", quiz=True),
            _assignment(2107, "Quiz 4: Chain rule", "g2", -1, 10, "missing", kind="online_quiz", quiz=True),
            _assignment(2108, "Midterm Exam 2", "g3", 6, 100, "no_submission", kind="on_paper"),
            _assignment(2109, "Midterm Exam 1", "g3", -20, 100, "graded", 84, kind="on_paper"),
            _assignment(2110, "HW 6: Linearization", "g1", 9, 20),
        ],
        "modules": [{"id": "m1", "name": "Unit 3: Derivatives", "position": 1, "items": [
            {"id": "i1", "title": "Derivative rules (notes)", "type": "Page", "page_url": "derivative-rules"},
            {"id": "i2", "title": "Unit 3 notes", "type": "File", "content_id": "8801"},
            {"id": "i3", "title": "HW 5: Implicit differentiation", "type": "Assignment", "content_id": "2105"},
        ]}],
        "pages": [{"url": "derivative-rules", "title": "Derivative rules", "updated_at": _iso(-12),
                   "html_url": f"{HOST}/courses/{calc_id}/pages/derivative-rules",
                   "body_html": "<h2>Derivative rules</h2><p><strong>Chain rule:</strong> d/dx f(g(x)) = f'(g(x))·g'(x).</p>"
                                "<p><strong>Product rule:</strong> (fg)' = f'g + fg'.</p><p>See the <a href=\"/courses/210/files/8801\">Unit 3 notes</a>.</p>"}],
        "files": [{"id": "8801", "name": "unit3-notes.txt", "content_type": "text/plain", "size": len(CALC_NOTES),
                   "updated_at": _iso(-12), "download_url": f"{HOST}/files/8801/download", "locked": False,
                   "sources": ["files_tab", "module"]}],
        "discussions": [], "quizzes": [],
        "announcements": [{"id": "an1", "title": "Midterm 2 review session", "posted_at": _iso(-1, 14),
                           "author": "Prof. Rivera", "html_url": None,
                           "message_html": "<p>Review session Thursday 6pm in Kerchof 317. Bring questions on the chain rule.</p>"}],
    }
    hist = {
        "id": hist_id, "name": "U.S. History to 1877", "course_code": "HIST 2150-001", "class_key": "fa::u.s. history to 1877",
        "on_dashboard": True, "term": {"id": "fa", "name": "Fall"}, "html_url": f"{HOST}/courses/{hist_id}",
        "grade": {"current_score": 93.0, "current_grade": "A"},
        "syllabus_html": "<p>Weekly reading responses (40%), paper (30%), final exam (30%).</p>",
        "assignment_groups": [{"id": "h1", "name": "Assignments", "weight": 0, "position": 1}],
        "assignments": [
            _assignment(3201, "Reading response: Reconstruction", "h1", -5, 10, "graded", 10),
            _assignment(3202, "Reading response: The Gilded Age", "h1", 3, 10, kind="online_text_entry"),
            _assignment(3203, "Research paper proposal", "h1", 11, 25,
                        desc="<p>One page: your question, three sources, and why it matters.</p>"),
        ],
        "modules": [], "pages": [], "discussions": [], "quizzes": [], "files_tab_hidden": False,
        "files": [{"id": "8802", "name": "reconstruction-notes.txt", "content_type": "text/plain", "size": len(HISTORY_NOTES),
                   "updated_at": _iso(-8), "download_url": f"{HOST}/files/8802/download", "locked": False, "sources": ["files_tab"]}],
        "announcements": [],
    }
    disc = dict(hist, id=hist_disc, course_code="HIST 2150-104", grade={}, assignments=[], files=[],
                html_url=f"{HOST}/courses/{hist_disc}", syllabus_html=None)
    cs = {
        "id": cs_id, "name": "Introduction to Programming", "course_code": "CS 1110-002", "class_key": "fa::introduction to programming",
        "on_dashboard": True, "term": {"id": "fa", "name": "Fall"}, "html_url": f"{HOST}/courses/{cs_id}",
        "grade": {"current_score": 100.0, "current_grade": "A"},
        "assignment_groups": [{"id": "c1", "name": "Programming Assignments", "weight": 60, "position": 1},
                              {"id": "c2", "name": "Labs", "weight": 40, "position": 2}],
        "assignments": [
            _assignment(4301, "PA-01: Hello, Python", "c1", -14, 15, "graded", 15, kind="external_tool"),
            _assignment(4302, "PA-02: Loops", "c1", 1, 20, "submitted", kind="external_tool"),
            _assignment(4303, "Lab 05: Functions", "c2", 4, 5, kind="external_tool"),
        ],
        "modules": [], "pages": [], "files": [], "discussions": [], "quizzes": [], "announcements": [],
        "files_tab_hidden": True, "syllabus_html": None,
    }
    return {"schema_version": 1, "synced_at": utcnow().isoformat() + "Z", "base_url": HOST,
            "user": {"id": "90001", "name": "Demo Student"}, "courses": [calc, hist, disc, cs],
            "calendar_events": [{"id": "ev1", "title": "Midterm 2 review session", "start_at": _iso(3, 22, 0),
                                 "end_at": _iso(3, 23, 30), "course_id": calc_id, "location": "Kerchof 317"}],
            "planner": [], "missing": [], "restricted": [], "errors": []}


def seed(user) -> None:
    from .services import ingest
    from .extensions import db
    from .models import SyncRun

    snap = snapshot()
    manifest = [{"id": f["id"], "updated_at": f["updated_at"], "name": f["name"], "content_type": f["content_type"],
                 "course_id": c["id"], "path": f"{c['name']}/{f['name']}"}
                for c in snap["courses"] for f in c.get("files") or []]
    run, needed = ingest.ingest_snapshot(user, snap, manifest)
    run = db.session.get(SyncRun, run.id)
    texts = {"8801": CALC_NOTES, "8802": HISTORY_NOTES}
    by_id = {m["id"]: m for m in manifest}
    for fid in needed:
        ingest.store_file(user, run, fid, by_id[fid]["updated_at"], io.BytesIO(texts[fid].encode()), "text/plain")
    ingest.complete_run(run, {"uploaded": len(needed), "failed": []})
