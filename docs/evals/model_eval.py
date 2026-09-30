"""Does a cheaper model make study sets and tutor answers as good as Opus? Blind, pairwise.

Runs the app's real generation path (study.generate_flashcards / generate_quiz with per-feature
models) and the tutor prompt on files in a local database, then asks Claude Opus 5.5 to judge each
cheaper model against Opus, in both orders to cancel position bias. Needs ANTHROPIC_API_KEY and
spends real money (about $6-8 for four sets and six questions).

Use only material you have the right to process for this purpose: synthetic course files or
openly licensed ones (e.g. OpenStax). Don't run it on real course materials; instructors' files
aren't ours to use to develop the product (UVA PROV-005, copyright). The sets live in a local JSON
file, not in the repo: {"sets": {"name": ["file:1", ...]}, "tutor": [["course name contains", "question"]]}.

    DATABASE_URL=sqlite:///path/hh.db EVAL_SETS=sets.json python docs/evals/model_eval.py out.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from statistics import mean

from flask import current_app
from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models import Course, User
from app.services import ai, retrieval, study
from app.blueprints.tutor import SYSTEM as TUTOR_SYSTEM

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
REFERENCE, JUDGE = "claude-opus-5-5", "claude-opus-5-5"



def load_sets(path: str | None = None) -> tuple[dict, list]:
    """The eval's file sets and tutor questions, from the local JSON file named by EVAL_SETS."""
    cfg = json.load(open(path or os.environ["EVAL_SETS"]))
    return cfg["sets"], [tuple(t) for t in cfg["tutor"]]


SCORE = {"type": "object", "properties": {
    "accuracy": {"type": "integer"}, "coverage": {"type": "integer"}, "quality": {"type": "integer"},
    "clarity": {"type": "integer"}, "errors": {"type": "array", "items": {"type": "string"}}},
    "required": ["accuracy", "coverage", "quality", "clarity", "errors"], "additionalProperties": False}
VERDICT = {"type": "object", "properties": {"a": SCORE, "b": SCORE, "winner": {"type": "string", "enum": ["a", "b", "tie"]},
                                            "reason": {"type": "string"}},
           "required": ["a", "b", "winner", "reason"], "additionalProperties": False}
JUDGE_SYSTEM = (
    "You grade study materials made for a college student from their own course files. Score each candidate "
    "1-10 on: accuracy (faithful to the source, no wrong facts or wrong answer keys), coverage (the most important "
    "ideas, across every source), quality (for flashcards: prompts that test understanding, not trivia; for quizzes: "
    "one clearly correct answer and plausible distractors; for tutor answers: actually teaches, grounded in the "
    "materials), and clarity. List concrete errors. Then pick the better candidate, or 'tie' if a student would not "
    "notice a difference. Judge content only, not length for its own sake.")


def generate(user, kind: str, refs: list[str], model: str) -> dict:
    current_app.config["AI_MODELS"] = {kind if kind != "deck" else "flashcards": model}
    material = study.gather_sources(user, refs)
    t0 = time.time()
    data = study.generate_flashcards(user, material, 15) if kind == "deck" else study.generate_quiz(user, material, 10)
    row = db.session.scalars(select(ai.AIUsage).order_by(ai.AIUsage.id.desc()).limit(1)).first()
    return {"model": model, "kind": kind, "seconds": round(time.time() - t0, 1), "data": data, "material": material.text,
            "usage": {"in": row.input_tokens, "out": row.output_tokens, "cost": row.cost_usd}}


def checks(kind: str, data: dict) -> dict:
    if kind == "deck":
        fronts = [c["front"].strip().lower() for c in data["cards"]]
        return {"items": len(fronts), "duplicates": len(fronts) - len(set(fronts))}
    answers = [q["answer"] for q in data["questions"]]
    return {"items": len(answers), "answer_positions": {i: answers.count(i) for i in sorted(set(answers))},
            "four_choices": sum(len(q["choices"]) == 4 for q in data["questions"]),
            "explained": sum(bool(q["explanation"]) for q in data["questions"])}


def tutor_answer(user, course_hint: str, question: str, model: str) -> dict:
    matches = [c for c in db.session.scalars(select(Course).where(Course.user_id == user.id)) if course_hint in c.name]
    course = max(matches, key=lambda c: len(c.files) + len(c.pages))  # the section that has the materials
    chunks = retrieval.search(user.id, question, [course.id], k=8)
    materials = "\n\n".join(f"[S{i}] {c.title} ({c.source_type})\n{c.text}" for i, c in enumerate(chunks, 1))
    prompt = f"<materials scope=\"{course.name}\">\n{materials}\n</materials>\n\n{question}"
    t0 = time.time()
    r = ai.provider().complete(system=TUTOR_SYSTEM, messages=[{"role": "user", "content": prompt}], max_tokens=12000,
                               effort="medium", model=model)
    return {"model": model, "question": question, "seconds": round(time.time() - t0, 1), "answer": r.text, "materials": materials,
            "usage": {"in": r.input_tokens, "out": r.output_tokens,
                      "cost": ai.cost_usd(r.model, r.input_tokens, r.output_tokens, r.cache_write_tokens, r.cache_read_tokens)}}


def judge(app, source: str, what: str, a: str, b: str) -> dict:
    with app.app_context():
        system = [{"type": "text", "text": JUDGE_SYSTEM},
                  {"type": "text", "text": f"<source>\n{source}\n</source>", "cache_control": {"type": "ephemeral"}}]
        prompt = f"Task: {what}\n\n<candidate_a>\n{a}\n</candidate_a>\n\n<candidate_b>\n{b}\n</candidate_b>"
        r = ai.provider().complete(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=16000,
                                   effort="medium", schema=VERDICT, model=JUDGE)
        v = json.loads(r.text)
        v["cost"] = ai.cost_usd(r.model, r.input_tokens, r.output_tokens, r.cache_write_tokens, r.cache_read_tokens)
        return v


def render(kind: str, data: dict) -> str:
    if kind == "deck":
        return "\n".join(f"- {c['front']} :: {c['back']}" for c in data["cards"])
    return "\n".join(f"Q: {q['question']}\n" + "\n".join(f"  {'*' if i == q['answer'] else '-'} {c}" for i, c in enumerate(q["choices"]))
                     + f"\n  E: {q['explanation']}" for q in data["questions"])


def pairwise(app, source, what, ref_text, cand_text):
    """Two judgments, candidate as B then as A; returns the candidate's scores and outcome."""
    one = judge(app, source, what, ref_text, cand_text)
    two = judge(app, source, what, cand_text, ref_text)
    outcome = {"b": "win", "a": "loss", "tie": "tie"}[one["winner"]], {"a": "win", "b": "loss", "tie": "tie"}[two["winner"]]
    return {"candidate": [one["b"], two["a"]], "reference": [one["a"], two["b"]], "outcomes": outcome,
            "reasons": [one["reason"], two["reason"]], "judge_cost": one["cost"] + two["cost"]}


def main(out_path: str):
    app = create_app("development")
    SETS, TUTOR = load_sets()
    results = {"generation": [], "tutor": []}
    with app.test_request_context():  # sources link back to file pages
        user = db.session.scalar(select(User).where(User.username == "traveler"))
        user.is_admin = True  # no quota for the eval
        db.session.commit()
        for name, refs in SETS.items():
            for kind in ("deck", "quiz"):
                outs = {m: generate(user, kind, refs, m) for m in MODELS}
                print(f"generated {kind:4} {name}: " + ", ".join(f"{m.split('-')[1]} ${o['usage']['cost']:.3f} {o['seconds']}s" for m, o in outs.items()), flush=True)
                results["generation"].append({"set": name, "kind": kind, "outputs": outs})
        for hint, q in TUTOR:
            outs = {m: tutor_answer(user, hint, q, m) for m in MODELS}
            print(f"tutor {hint}: " + ", ".join(f"{m.split('-')[1]} ${o['usage']['cost']:.3f}" for m, o in outs.items()), flush=True)
            results["tutor"].append({"question": q, "outputs": outs})

    jobs = []
    for g in results["generation"]:
        ref = g["outputs"][REFERENCE]
        what = "15 flashcards from the source" if g["kind"] == "deck" else "a 10-question multiple-choice practice quiz from the source"
        for m in MODELS[1:]:
            jobs.append((g, m, ref["material"], what, render(g["kind"], ref["data"]), render(g["kind"], g["outputs"][m]["data"])))
    for t in results["tutor"]:
        ref = t["outputs"][REFERENCE]
        what = f"a tutor answer to the student's question: {t['question']}"
        for m in MODELS[1:]:
            jobs.append((t, m, ref["materials"], what, ref["answer"], t["outputs"][m]["answer"]))
    with ThreadPoolExecutor(6) as pool:
        verdicts = list(pool.map(lambda j: pairwise(app, j[2], j[3], j[4], j[5]), jobs))
    for (item, m, *_), v in zip(jobs, verdicts):
        item.setdefault("judged", {})[m] = v

    for g in results["generation"]:
        g["checks"] = {m: checks(g["kind"], o["data"]) for m, o in g["outputs"].items()}
        for o in g["outputs"].values():
            o.pop("material", None)
    for t in results["tutor"]:
        for o in t["outputs"].values():
            o.pop("materials", None)
    json.dump(results, open(out_path, "w"), indent=1)

    print("\n== Summary (candidate vs Opus 5.5; scores are means of 1-10 judge scores, both orders) ==")
    for section, kinds in (("generation", ("deck", "quiz")), ("tutor", (None,))):
        for kind in kinds:
            items = [x for x in results[section] if kind is None or x["kind"] == kind]
            label = {"deck": "Flashcards", "quiz": "Quizzes", None: "Tutor"}[kind]
            for m in MODELS:
                cost = mean(x["outputs"][m]["usage"]["cost"] for x in items)
                secs = mean(x["outputs"][m]["seconds"] for x in items)
                line = f"   {label:10} {m:18} ${cost:.3f}/action {secs:5.1f}s"
                if m != REFERENCE:
                    js = [x["judged"][m] for x in items]
                    outs = [o for j in js for o in j["outcomes"]]
                    cand = [s for j in js for s in j["candidate"]]
                    ref = [s for j in js for s in j["reference"]]
                    avg = lambda ss, k: mean(s[k] for s in ss)
                    line += (f" | W/T/L vs Opus {outs.count('win')}/{outs.count('tie')}/{outs.count('loss')}"
                             f" | accuracy {avg(cand, 'accuracy'):.1f} (Opus {avg(ref, 'accuracy'):.1f})"
                             f" quality {avg(cand, 'quality'):.1f} (Opus {avg(ref, 'quality'):.1f})"
                             f" coverage {avg(cand, 'coverage'):.1f} (Opus {avg(ref, 'coverage'):.1f})")
                print(line)
    judge_cost = sum(v["judge_cost"] for v in verdicts)
    gen_cost = sum(o["usage"]["cost"] for x in results["generation"] + results["tutor"] for o in x["outputs"].values())
    print(f"\n   spent: generation ${gen_cost:.2f}, judging ${judge_cost:.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "model_eval.json")
