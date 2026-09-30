"""Re-test quizzes after a prompt change: new-prompt candidate models against the Opus quizzes saved by
model_eval.py (the old prompt, i.e. what production served). Blind and pairwise, both orders.

    DATABASE_URL=sqlite:///path/hh.db python docs/evals/quiz_prompt_eval.py model_eval.json out.json
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from statistics import mean

from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models import User
from app.services import study

sys.path.insert(0, "docs/evals")
from model_eval import REFERENCE, SETS, checks, generate, pairwise, render  # noqa: E402

CANDIDATES = ["claude-sonnet-5-5", "claude-opus-5-5"]


def main(saved_path: str, out_path: str):
    saved = {g["set"]: g for g in json.load(open(saved_path))["generation"] if g["kind"] == "quiz"}
    app = create_app("development")
    rows = []
    with app.test_request_context():
        user = db.session.scalar(select(User).where(User.username == "traveler"))
        for name, refs in SETS.items():
            material = study.gather_sources(user, refs).text
            outs = {m: generate(user, "quiz", refs, m) for m in CANDIDATES}
            print(f"{name}: " + ", ".join(f"{m} ${o['usage']['cost']:.3f}" for m, o in outs.items()), flush=True)
            rows.append({"set": name, "material": material, "reference": saved[name]["outputs"][REFERENCE]["data"],
                         "outputs": outs})
    what = "a 10-question multiple-choice practice quiz from the source"
    jobs = [(r, m) for r in rows for m in CANDIDATES]
    with ThreadPoolExecutor(6) as pool:
        verdicts = list(pool.map(lambda j: pairwise(app, j[0]["material"], what, render("quiz", j[0]["reference"]),
                                                    render("quiz", j[0]["outputs"][j[1]]["data"])), jobs))
    for (r, m), v in zip(jobs, verdicts):
        r.setdefault("judged", {})[m] = v
    for r in rows:
        r.pop("material")
        r["checks"] = {m: checks("quiz", o["data"]) for m, o in r["outputs"].items()}
    json.dump(rows, open(out_path, "w"), indent=1)
    print("\n== New quiz prompt vs production Opus quizzes (old prompt) ==")
    for m in CANDIDATES:
        js = [r["judged"][m] for r in rows]
        outs = [o for j in js for o in j["outcomes"]]
        cand = [s for j in js for s in j["candidate"]]
        ref = [s for j in js for s in j["reference"]]
        avg = lambda ss, k: mean(s[k] for s in ss)  # noqa: E731
        print(f"   {m:18} ${mean(r['outputs'][m]['usage']['cost'] for r in rows):.3f}/quiz | W/T/L {outs.count('win')}/"
              f"{outs.count('tie')}/{outs.count('loss')} | accuracy {avg(cand, 'accuracy'):.1f} (old {avg(ref, 'accuracy'):.1f})"
              f" quality {avg(cand, 'quality'):.1f} (old {avg(ref, 'quality'):.1f}) coverage {avg(cand, 'coverage'):.1f}"
              f" (old {avg(ref, 'coverage'):.1f}) | items {[r['checks'][m]['items'] for r in rows]}")
    print(f"   judging ${sum(v['judge_cost'] for v in verdicts):.2f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
