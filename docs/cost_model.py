"""Homework Hatch cost model. Run: python docs/cost_model.py

Every number is either measured (Sept 30 2026: the live app with one real student's 9 UVA classes,
and docs/evals/model_eval.py on those classes' files) or a vendor list price checked the same day.
Assumptions are marked ASSUME and are the knobs to change as real usage data comes in; the admin
"AI spend" page now records what each action really costs.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------- vendor prices (USD)

# Anthropic, per million tokens. Cache writes are 1.25x input (5-minute TTL); batch is 50% off.
MODELS = {
    "Opus 5.5": dict(inp=4.00, out=20.00, cache_read=0.20),
    "Sonnet 5.5": dict(inp=2.00, out=10.00, cache_read=0.20),
    "Haiku 4.5": dict(inp=1.00, out=5.00, cache_read=0.10),
}
# Supabase Pro: $25/mo incl. 8 GB disk, 100 GB storage, 250 GB egress, $10 compute credit (Micro).
SUPA_BASE, SUPA_DISK_GB, SUPA_STORAGE_GB, SUPA_EGRESS_GB = 25.0, 8, 100, 250
SUPA_DISK_OVER, SUPA_STORAGE_OVER, SUPA_EGRESS_OVER = 0.125, 0.0213, 0.09
SUPA_COMPUTE = {"Micro": 0, "Small": 5, "Medium": 50}  # add-on minus the $10 credit
# Render web service instances; bandwidth overage per GB.
RENDER = {"Starter (0.5 CPU, 512 MB)": 7, "Standard (1 CPU, 2 GB)": 25, "Pro (2 CPU, 4 GB)": 85}
RENDER_BW_OVER = 0.15
# Composio: 100k tool calls free; Pro $29 with $29 credit, then $0.0003/call.
COMPOSIO_FREE_CALLS, COMPOSIO_PRO, COMPOSIO_PER_CALL = 100_000, 29.0, 0.0003
# Stripe: 2.9% + $0.30 per charge, plus 0.7% Billing fee on subscriptions.
def stripe_fee(amount: float, subscription: bool) -> float:
    return amount * 0.029 + 0.30 + (amount * 0.007 if subscription else 0)

# ---------------------------------------------------------------- measured per-student footprint

FILES_GB_PER_SEMESTER = 0.137     # measured: 134 files, 137 MB (9 classes, reading-heavy)
DB_GB_PER_STUDENT = 0.007         # measured: public tables for one student
SYNC_READ_MB = 0.6                # measured ~300 KB compressed per sync; ~2x on the wire
UNCHANGED_SYNC_READ = 58 / 740    # measured: an unchanged sync loads 58 KB of rows, not 740 KB (5 SQL statements, not 91)
UNCHANGED_SYNC_SHARE = 0.9        # ASSUME: 9 of 10 hourly syncs find nothing new in Canvas
SYNCS_PER_DAY = 12                # ASSUME: hourly while Chrome is open ~12 h/day
DOWNLOAD_GB_PER_MONTH = 0.05      # ASSUME: student opens ~50 MB of their files a month
PAGE_GB_PER_MONTH = 0.03          # ASSUME: HTML/JSON served by Render per student
COMPOSIO_CALLS_PER_MONTH = 100    # ASSUME: ~50 calendar create/patch + Drive search/imports
CALENDAR_SHARE = 0.4              # ASSUME: share of students who turn on Google

def infra_month(students: int, semesters_kept: float = 1.0, snapshot_skip: bool = True) -> dict:
    storage = students * FILES_GB_PER_SEMESTER * semesters_kept
    disk = students * DB_GB_PER_STUDENT
    skip = (1 - UNCHANGED_SYNC_SHARE) + UNCHANGED_SYNC_SHARE * UNCHANGED_SYNC_READ if snapshot_skip else 1.0
    sync_egress = students * SYNC_READ_MB * SYNCS_PER_DAY * 30 / 1024 * skip
    egress = sync_egress + students * DOWNLOAD_GB_PER_MONTH
    compute = "Micro" if students <= 1_000 else "Small" if students <= 5_000 else "Medium"
    render = "Starter (0.5 CPU, 512 MB)" if students <= 200 else "Standard (1 CPU, 2 GB)" if students <= 2_000 else "Pro (2 CPU, 4 GB)"
    render_n = max(1, students // 8_000 + 1) if students > 2_000 else 1
    calls = students * CALENDAR_SHARE * COMPOSIO_CALLS_PER_MONTH
    comp = 0.0 if calls <= COMPOSIO_FREE_CALLS else COMPOSIO_PRO + max(0, (calls - COMPOSIO_FREE_CALLS) * COMPOSIO_PER_CALL - COMPOSIO_PRO)
    supa = (SUPA_BASE + SUPA_COMPUTE[compute] + max(0, disk - SUPA_DISK_GB) * SUPA_DISK_OVER
            + max(0, storage - SUPA_STORAGE_GB) * SUPA_STORAGE_OVER + max(0, egress - SUPA_EGRESS_GB) * SUPA_EGRESS_OVER)
    bw = students * PAGE_GB_PER_MONTH
    rend = RENDER[render] * render_n + max(0, bw - 100) * RENDER_BW_OVER
    return {"supabase": supa, "render": rend, "composio": comp, "total": supa + rend + comp, "egress_gb": egress,
            "storage_gb": storage, "compute": compute, "render_plan": f"{render_n}x {render}"}


# ---------------------------------------------------------------- AI actions

# Tokens per action, per model: (uncached input, output incl. thinking, cache-write). Haiku 4.5's tokenizer
# counts the same text as ~23% fewer tokens than the 5.x models.
# Measured by docs/evals/model_eval.py (Sept 30 2026): flashcards and quizzes from 4 real file sets (lab keys,
# lecture keys, a 50k-character reading, a 210k-character reading), tutor answers to 6 real questions.
# Quizzes: docs/evals/quiz_prompt_eval.py, with the prompt tightened after the first run.
TOKENS = {
    "Tutor answer":                     {"Opus 5.5": (4_000, 1_180, 0), "Sonnet 5.5": (4_000, 770, 0), "Haiku 4.5": (3_080, 360, 0)},
    "Tutor, first turn with a file":    {"Opus 5.5": (4_000, 1_180, 13_000), "Sonnet 5.5": (4_000, 770, 13_000), "Haiku 4.5": (3_080, 360, 10_000)},
    "Flashcards (15 cards)":            {"Opus 5.5": (23_158, 1_476, 0), "Sonnet 5.5": (23_158, 1_530, 0), "Haiku 4.5": (17_957, 1_098, 0)},
    "Practice quiz (10 questions)":     {"Opus 5.5": (23_342, 3_402, 0), "Sonnet 5.5": (23_342, 2_284, 0), "Haiku 4.5": (18_029, 2_055, 0)},
    "Summary":                          {"Opus 5.5": (20_000, 1_200, 0), "Sonnet 5.5": (20_000, 1_200, 0), "Haiku 4.5": (15_400, 1_000, 0)},  # ASSUME
    "Read a scanned PDF (20 pages)":    {"Opus 5.5": (40_000, 10_000, 0), "Sonnet 5.5": (40_000, 10_000, 0), "Haiku 4.5": (40_000, 10_000, 0)},  # ASSUME
}
SHARE_OF_ACTIONS = {  # ASSUME: what students use AI for
    "Tutor answer": 0.60, "Tutor, first turn with a file": 0.05, "Flashcards (15 cards)": 0.15,
    "Practice quiz (10 questions)": 0.10, "Summary": 0.08, "Read a scanned PDF (20 pages)": 0.02,
}
SHAREABLE = {"Flashcards (15 cards)", "Practice quiz (10 questions)", "Summary", "Read a scanned PDF (20 pages)"}

# Blind pairwise judging by Opus 5.5, both orders (wins/ties/losses against Opus 5.5).
QUALITY = {
    "Flashcards (15 cards)": {"Sonnet 5.5": "6/0/2, accuracy 9.2 vs 9.2", "Haiku 4.5": "0/0/8, accuracy 7.6 vs 9.4"},
    "Practice quiz (10 questions)": {"Sonnet 5.5": "3/3/2 with the new prompt, accuracy 9.6 vs 9.6", "Haiku 4.5": "0/0/8, accuracy 5.5 vs 9.5"},
    "Tutor answer": {"Sonnet 5.5": "4/1/7, accuracy 9.2 vs 9.2", "Haiku 4.5": "0/0/12, accuracy 8.4 vs 9.4"},
}


def action_cost(action: str, model: str) -> float:
    m, (inp, out, cached) = MODELS[model], TOKENS[action][model]
    return (inp * m["inp"] + out * m["out"] + cached * m["inp"] * 1.25) / 1e6


@dataclass
class Setup:
    name: str
    models: dict            # action -> model (default for missing actions: "default")
    shared: float = 0.0     # share of shareable actions served from a result someone already made (cost 0)

    def model(self, action: str) -> str:
        return self.models.get(action, self.models["default"])

    def per_action(self, shared: float | None = None) -> float:
        hit = self.shared if shared is None else shared
        return sum(w * action_cost(a, self.model(a)) * ((1 - hit) if a in SHAREABLE else 1)
                   for a, w in SHARE_OF_ACTIONS.items())


ON_SONNET = {a: "Sonnet 5.5" for a in ("Tutor answer", "Tutor, first turn with a file", "Flashcards (15 cards)",
                                       "Practice quiz (10 questions)")}
SETUPS = {
    "before": Setup("Before: Opus 5.5 for everything, nothing shared", {"default": "Opus 5.5"}),
    "now": Setup("Now: Sonnet for tutor, flashcards, quizzes; Opus for summaries/PDFs", {"default": "Opus 5.5", **ON_SONNET}),
    "sonnet": Setup("Option: summaries on Sonnet too (untested)",
                    {"default": "Sonnet 5.5", "Read a scanned PDF (20 pages)": "Opus 5.5"}),
}
SHARED_HIT = 0.0  # cross-account reuse of generated material was removed (legal review, Sept 30 2026)

# ---------------------------------------------------------------- plans

# (price per month, AI actions a free user runs per month on average, a paid user's typical month, cap)
BEFORE_PLANS = dict(price=10.0, subscription=True, free_actions=6, paid_actions=60, cap=200)   # $10 Normal, free 25/mo
PASS = dict(price=20 / 4, subscription=False, charge=20.0)   # $20 once, ~4 months of use
PLUS = dict(price=6.0, subscription=True, charge=6.0)
FREE_TRIAL_ACTIONS_PER_MONTH = 0.5   # ASSUME: 5 trial actions, most used in the first month, averaged over a semester
PAID_TYPICAL, PAID_CAP = 50, 100     # ASSUME typical; the cap is what the plans promise
PASS_SHARE = 0.6                     # ASSUME: 6 in 10 paying students pick the one-time pass
PAID_SHARE = 0.04                    # ASSUME: 4% of students pay (2-5% is typical for free apps)


def fee_per_month(plan: dict) -> float:
    return stripe_fee(plan["charge"], plan["subscription"]) * plan["price"] / plan["charge"]


def paid_month() -> tuple[float, float]:
    """Revenue and Stripe fees per paying student per month, blended over pass and Plus."""
    rev = PASS_SHARE * PASS["price"] + (1 - PASS_SHARE) * PLUS["price"]
    fee = PASS_SHARE * fee_per_month(PASS) + (1 - PASS_SHARE) * fee_per_month(PLUS)
    return rev, fee


# ---------------------------------------------------------------- report

def money(v: float) -> str:
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def main():
    print("== Cost per AI action (measured tokens, list prices) ==")
    print(f"   {'':34}{'Opus 5.5':>10}{'Sonnet 5.5':>12}{'Haiku 4.5':>11}   quality vs Opus (W/T/L)")
    for a in TOKENS:
        q = QUALITY.get(a, {})
        print(f"   {a:34}" + "".join(f"{'$%.3f' % action_cost(a, m):>{w}}" for m, w in (("Opus 5.5", 10), ("Sonnet 5.5", 12), ("Haiku 4.5", 11)))
              + (f"   Sonnet {q['Sonnet 5.5']}; Haiku {q['Haiku 4.5']}" if q else ""))

    print("\n== Blended cost per AI action ==")
    for key, s in SETUPS.items():
        print(f"   {s.name:70} ${s.per_action(0.0):.4f}")

    print("\n== Hosting per month (Supabase Pro + Render + Composio), excluding AI ==")
    for n in (100, 1_000, 5_000, 10_000):
        a, b = infra_month(n, snapshot_skip=False), infra_month(n, snapshot_skip=True)
        print(f"   {n:>6} students: every sync read in full ${a['total']:7.0f} (egress {a['egress_gb']:5.0f} GB)"
              f" | unchanged syncs skipped ${b['total']:7.0f} (egress {b['egress_gb']:5.0f} GB) = ${b['total'] / n:.3f}/student")

    rev, fee = paid_month()
    infra_1k = infra_month(1_000)["total"] / 1_000
    print("\n== One paying student per month (new plans) ==")
    print(f"   revenue ${rev:.2f} (Pass $5.00, Plus $6.00, {int(PASS_SHARE * 100)}/{100 - int(PASS_SHARE * 100)} mix),"
          f" Stripe ${fee:.2f}, hosting ${infra_1k:.3f}")
    for key in ("before", "now", "sonnet"):
        s = SETUPS[key]
        for label, n, hit in (("typical", PAID_TYPICAL, SHARED_HIT), ("uses the whole cap", PAID_CAP, 0.0)):
            ai = n * s.per_action(hit)
            print(f"   {s.name[:52]:52} {label:18} {n:>3} actions: AI ${ai:5.2f} -> margin ${rev - fee - infra_1k - ai:5.2f}")
        cap_even = (rev - fee - infra_1k) / s.per_action(0.0)
        print(f"   {'':52} break-even cap with nothing shared: {cap_even:.0f} actions/month")

    print("\n== Whole business per month ==")
    print(f"   ({int(PAID_SHARE * 100)}% pay; free users {FREE_TRIAL_ACTIONS_PER_MONTH} AI actions/mo after the trial change,"
          f" paid {PAID_TYPICAL})")
    b = BEFORE_PLANS
    before_per = SETUPS["before"].per_action(0.0)
    for n in (100, 1_000, 10_000):
        infra = infra_month(n, snapshot_skip=False)["total"]
        paid = n * PAID_SHARE
        ai = (n - paid) * b["free_actions"] * before_per + paid * b["paid_actions"] * before_per
        revenue = paid * b["price"]
        net_before = revenue - paid * stripe_fee(b["price"], True) - ai - infra
        rows = [f"before (Opus, free 25/mo, $10 plan) {money(net_before):>8}"]
        for key in ("now", "sonnet"):
            s, infra = SETUPS[key], infra_month(n)["total"]
            ai = (n - paid) * FREE_TRIAL_ACTIONS_PER_MONTH * s.per_action() + paid * PAID_TYPICAL * s.per_action()
            rows.append(f"{key} {money(paid * (rev - fee) - ai - infra):>8}")
        print(f"   {n:>6} students: " + "  |  ".join(rows))

    print("\n== Paid share needed to break even ==")
    for key in ("now", "sonnet"):
        s = SETUPS[key]
        free_cost = FREE_TRIAL_ACTIONS_PER_MONTH * s.per_action() + infra_1k
        margin = rev - fee - PAID_TYPICAL * s.per_action() - infra_1k
        print(f"   {s.name:70} {free_cost / (margin + free_cost) * 100:4.1f}%  (free user ${free_cost:.3f}/mo, paid user nets ${margin:.2f}/mo)")
    free_before = b["free_actions"] * before_per + infra_1k
    margin_before = b["price"] - stripe_fee(b["price"], True) - b["paid_actions"] * before_per - infra_1k
    print(f"   {'Before (Opus, free 25/mo, $10/mo)':70} {free_before / (margin_before + free_before) * 100:4.1f}%")


if __name__ == "__main__":
    main()
