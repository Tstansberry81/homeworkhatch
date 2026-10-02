"""Synthetic test cases (no real course material). expected = kind[/confidence]."""
from datetime import datetime, timedelta

T0 = datetime(2026, 10, 14, 14, 0)


def A(name, types=("online_upload",), quiz=False, pts=10, group=None, gcount=3, share=None, grading="points",
      omit=False, desc=None, rubric=None, unlock=None, lock=None, user_kind=None):
    return dict(name=name, submission_types=list(types), is_quiz=quiz, points_possible=pts, group_name=group,
                group_count=gcount, share=share, grading_type=grading, omit_from_final_grade=omit,
                description_html=desc, rubric=rubric, due_at=T0,
                unlock_at=(T0 - timedelta(hours=unlock)) if unlock else None,
                lock_at=(T0 + timedelta(hours=lock)) if lock is not None else None, user_kind=user_kind)


def E(title, minutes=75):
    return dict(name=title, is_event=True, start_at=T0, end_at=T0 + timedelta(minutes=minutes))


R = [{"criterion": "Thesis", "points": 10}]

CASES = [
    # ---- clear assessments
    ("Midterm Exam 2", A("Midterm Exam 2", ["on_paper"], pts=100, group="Exams", share=0.25), "midterm/high"),
    ("Final Exam", A("Final Exam", ["none"], pts=200, group="Final Exam", share=0.30), "final/high"),
    ("Final (no noun)", A("Final", ["none"], pts=150, group="Exams", share=0.20), "final/high"),
    ("Exam 3 (Final)", A("Exam 3 (Final)", ["none"], pts=100, group="Exams", share=0.20), "final/high"),
    ("Quiz 4: Chain rule (Classic)", A("Quiz 4: Chain rule", ["online_quiz"], quiz=True, pts=10, group="Quizzes", share=0.02), "quiz/high"),
    ("Quiz 3 New Quizzes, quiz group", A("Quiz 3", ["external_tool"], pts=20, group="Quizzes", share=0.02), "quiz/high"),
    ("Quiz 3 New Quizzes, default group only", A("Quiz 3", ["external_tool"], pts=20, group="Assignments", gcount=1, share=0.04), "quiz/high"),
    ("Quiz with LockDown suffix", A("Quiz 5 - Requires Respondus LockDown Browser", ["online_quiz"], quiz=True, pts=20, group="Quizzes", share=0.03), "quiz/high"),
    ("Take-home midterm via upload", A("Take-Home Midterm", ["online_upload"], pts=100, group="Exams", share=0.125), "midterm/high"),
    ("Gradescope midterm", A("Midterm 1 (Gradescope)", ["external_tool"], pts=100, group="Midterms", share=0.15), "midterm/high"),
    ("Prelim", A("Prelim 1", ["none"], pts=100, group="Prelims", share=0.20), "midterm/high"),
    ("Lab practical", A("Lab Practical 1", ["on_paper"], pts=50, group="Lab", share=0.06), "test/medium"),
    ("Unit test HS summative", A("Unit 2 Test", ["none"], pts=100, group="Summative Assessments", share=0.12), "test/high"),
    ("Benchmark HS", A("Benchmark 2", ["none"], pts=50, group="Summative", share=0.10), "test/high"),
    ("Retake HS", A("Retake: Unit 1 Test", ["none"], pts=100, group="Summative", share=0.10), "test/high"),
    ("Make-up exam", A("Make-up Exam 1", ["none"], pts=100, group="Exams", share=0.20), "test/high"),
    ("Vocab quiz in formative group", A("Vocab Quiz 8", ["on_paper"], pts=20, group="Formative", share=0.02), "quiz/high"),
    ("Pop quiz in class", A("Pop Quiz", ["none"], pts=10, group="Quizzes", share=0.01), "quiz/high"),
    ("Online-course assessment", A("Unit 2 Assessment", ["external_tool"], pts=50, group="Assessments", share=0.10), "test/high"),
    ("Knowledge check LTI", A("Module 3 Knowledge Check", ["external_tool"], pts=5, group="Knowledge Checks", share=0.01), "quiz/high"),
    ("Reading quiz (micro stakes)", A("Reading Quiz 7", ["online_quiz"], quiz=True, pts=5, group="Reading Quizzes", share=0.008), "quiz/high"),
    ("Checkpoint in quiz group", A("Checkpoint 3", ["on_paper"], pts=10, group="Quizzes", share=0.02), "quiz/high"),
    ("Statistical Tests Quiz (head noun)", A("Statistical Tests Quiz", ["online_quiz"], quiz=True, pts=10, group="Quizzes", share=0.02), "quiz/high"),
    ("Structural only: 'Unit 4' in Tests", A("Unit 4", ["on_paper"], pts=100, group="Tests", share=0.125), "test/high"),
    ("Structural only: 'Chapter 7' Classic quiz in Quizzes", A("Chapter 7", ["online_quiz"], quiz=True, pts=15, group="Quizzes", share=0.02), "quiz/high"),
    ("Exam named, default group, upload", A("Exam 2", ["online_upload"], pts=100, group="Assignments", gcount=1, share=0.10), "test/medium"),
    ("Exam with proctoring words in description", A("Exam 1", ["none"], pts=100, group="Assignments", gcount=1, share=0.20,
                                                   desc="<p>Closed book. You will have 75 minutes. Bring a calculator and a pencil.</p>"), "test/high"),
    ("Short window, Classic quiz, no keyword", A("Week 6", ["online_quiz"], quiz=True, pts=10, group="Assignments", gcount=1, share=0.03, unlock=2, lock=0), "quiz/medium"),
    ("Oral exam by recording", A("Oral Exam", ["media_recording"], pts=50, group="Exams", share=0.10), "test/high"),
    ("Final exam essay part", A("Final Exam Part 2: Essay", ["online_upload"], pts=100, group="Final Exam", share=0.15), "final/medium"),
    ("Extra credit quiz", A("Extra Credit Quiz", ["online_quiz"], quiz=True, pts=5, group="Extra Credit", share=0.0), "quiz/medium"),
    ("Final exam info marker", A("Final Exam Info", ["none"], pts=0, grading="not_graded", group="Assignments", gcount=1), "final/medium"),
    # ---- lookalikes that must NOT count
    ("Final Project", A("Final Project", ["online_upload"], pts=100, group="Projects", share=0.25, rubric=R), "none"),
    ("Final Paper", A("Final Paper", ["online_upload"], pts=100, group="Writing", share=0.20, rubric=R,
                      desc="<p>2500 words, double-spaced, MLA.</p>"), "none"),
    ("Final presentation", A("Final Presentation", ["media_recording"], pts=50, group="Presentations", share=0.15), "none"),
    ("Midterm paper", A("Midterm Paper", ["online_upload"], pts=100, group="Papers", share=0.20), "none"),
    ("Midterm project proposal", A("Midterm Project Proposal", ["online_upload"], pts=20, group="Projects", share=0.04), "none"),
    ("Exam corrections", A("Exam 1 Corrections", ["online_upload"], pts=10, group="Homework", share=0.01), "none"),
    ("Quiz corrections", A("Quiz Corrections - Quiz 3", ["online_upload"], pts=5, group="Homework", share=0.005), "none"),
    ("Exam wrapper", A("Exam Wrapper: Exam 2", ["online_quiz"], quiz=True, pts=2, group="Participation", share=0.002), "none"),
    ("Midterm reflection", A("Midterm Reflection", ["online_text_entry"], pts=5, group="Homework", share=0.01), "none"),
    ("Study guide", A("Midterm Study Guide", ["online_upload"], pts=5, group="Homework", share=0.01), "none"),
    ("Test review HW", A("Unit 3 Test Review", ["none"], pts=10, group="Classwork", share=0.01), "none"),
    ("Practice quiz graded 0", A("Practice Quiz 2", ["online_quiz"], quiz=True, pts=0, omit=True), "none"),
    ("Practice exam in quiz group", A("Practice Midterm", ["online_quiz"], quiz=True, pts=5, group="Quizzes", share=0.01), "none"),
    ("Self-test", A("Chapter 5 Self-Test", ["online_quiz"], quiz=True, pts=0), "none"),
    ("Ungraded quiz by description", A("Quiz 6", ["online_quiz"], quiz=True, pts=0, omit=True,
                                       desc="<p>This quiz is for practice and is not graded. Unlimited attempts.</p>"), "none"),
    ("Pre-lab quiz", A("Pre-lab Quiz 6", ["online_quiz"], quiz=True, pts=2, group="Lab", share=0.004), "none"),
    ("Survey via graded survey", A("Mid-semester Feedback Survey", ["online_quiz"], quiz=True, pts=1), "none"),
    ("Syllabus quiz", A("Syllabus Quiz", ["online_quiz"], quiz=True, pts=5, group="Quizzes", share=0.01), "none"),
    ("Academic integrity quiz", A("Academic Integrity Quiz", ["online_quiz"], quiz=True, pts=5), "none"),
    ("LockDown practice quiz", A("LockDown Browser Practice Quiz", ["online_quiz"], quiz=True, pts=0), "none"),
    ("Attendance quiz", A("Attendance Quiz 3/14", ["online_quiz"], quiz=True, pts=1, group="Participation"), "none"),
    ("Quizlet set homework", A("Create a Quizlet set for Unit 4", ["online_url"], pts=10, group="Homework", share=0.01), "none"),
    ("Student writes a quiz", A("Write a 10-question quiz for your classmates", ["online_upload"], pts=10, group="Homework"), "none"),
    ("Homework done as Classic quiz", A("Chapter 7", ["online_quiz"], quiz=True, pts=15, group="Homework", share=0.02), "none"),
    ("Homework Quiz in HW group", A("Homework Quiz 4", ["online_quiz"], quiz=True, pts=10, group="Homework", share=0.02), "none"),
    ("CS unit tests", A("Unit Tests for Linked List", ["external_tool"], pts=20, group="Programming Assignments", share=0.03), "none"),
    ("CS test cases", A("Test Cases: Project 2", ["online_upload"], pts=10, group="Projects", share=0.02), "none"),
    ("Hypothesis test worksheet", A("Hypothesis Test Worksheet", ["online_upload"], pts=10, group="Homework", share=0.02), "none"),
    ("CS project checkpoint", A("Checkpoint 2", ["external_tool"], pts=20, group="Projects", share=0.04), "none"),
    ("Discussion about exam", A("Week 5 Discussion: Exam prep", ["discussion_topic"], pts=5, group="Discussions"), "none"),
    ("Exam conflict form", A("Final Exam Conflict Form", ["online_text_entry"], pts=0), "none"),
    ("Exam solutions posted as item", A("Exam 2 Solutions", ["none"], pts=0, grading="not_graded"), "none"),
    ("Course evaluation", A("Midterm Course Evaluation", ["online_quiz"], quiz=True, pts=1), "none"),
    ("Lab report", A("Lab 4: Titration", ["online_upload"], pts=20, group="Labs", share=0.03, rubric=R), "none"),
    ("HW", A("HW 5: Implicit differentiation", ["online_upload"], pts=20, group="Homework", share=0.03), "none"),
    ("Participation points", A("Participation Week 3", ["none"], pts=5, group="Participation"), "none"),
    ("Pretest", A("Unit 5 Pre-Test", ["online_quiz"], quiz=True, pts=0), "none"),
    ("Final grade adjustment column", A("Final Grade Adjustment", ["none"], pts=0), "none"),
    ("In-class essay (kept none, override-able)", A("In-class Essay 2", ["on_paper"], pts=50, group="Essays", share=0.08), "none"),
    # ---- calendar events
    ("EV Midterm 2", E("Midterm 2", 75), "midterm/high"),
    ("EV Exam 1 with room", E("Exam 1 - Room 120", 50), "test/high"),
    ("EV Final exam w/ time", E("FINAL EXAM 9am-12pm", 180), "final/high"),
    ("EV Quiz in lab (30 min)", E("Quiz 4 (in lab)", 30), "quiz/medium"),
    ("EV review session", E("Final Exam Review Session", 90), "none"),
    ("EV exam review", E("Exam 2 Review", 60), "none"),
    ("EV office hours", E("Office Hours", 60), "none"),
    ("EV finals week banner", E("Finals Week", 0), "none"),
    ("EV no lecture notice", E("No lecture - Exam 2 in evening", 75), "none"),
    ("EV exam schedule", E("Final Exam Schedule", 24 * 60), "none"),
    # ---- user override and inheritance
    ("Override promotes", A("In-class Essay 2", ["on_paper"], pts=50, group="Essays", share=0.08, user_kind="test"), "test/user"),
    ("Override demotes", A("Quiz 3", ["online_quiz"], quiz=True, pts=10, group="Quizzes", share=0.02, user_kind="none"), "none/user"),
]
