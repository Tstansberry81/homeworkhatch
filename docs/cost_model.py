"""Homework Hatch cost model. Run: python docs/cost_model.py

Every number is either measured from the live app (Sept 30 2026, one real student with 9 UVA
classes) or a vendor list price checked the same day. Assumptions are marked ASSUME and are the
knobs to change as real usage data comes in.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------- vendor prices (USD)

# Anthropic, per million tokens. Cache writes are 1.25x input (5-minute TTL); batch is 50% off.
MODELS = {
    "Opus 5.5 (current)": dict(inp=4.00, out=20.00, cache_read=0.20),
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
SYNCS_PER_DAY = 12                # ASSUME: hourly while Chrome is open ~12 h/day
DOWNLOAD_GB_PER_MONTH = 0.05      # ASSUME: student opens ~50 MB of their files a month
PAGE_GB_PER_MONTH = 0.03          # ASSUME: HTML/JSON served by Render per student
COMPOSIO_CALLS_PER_MONTH = 100    # ASSUME: ~50 calendar create/patch + Drive search/imports
CALENDAR_SHARE = 0.4              # ASSUME: share of students who turn on Google

# ---------------------------------------------------------------- AI actions (measured tokens)

@dataclass
class Action:
    name: str
    share: float        # ASSUME: share of all AI actions
    inp: int            # uncached input tokens
    out: int            # output tokens (includes thinking)
    cached: int = 0     # cache-write tokens (first turn) for attached files

ACTIONS = [
    # Tutor: measured 3,697 in / 2,018 out with a 13k-token attached reading (cache write).
    Action("Tutor answer", 0.60, inp=4_000, out=1_500, cached=0),
    Action("Tutor answer, first turn with a file attached", 0.05, inp=4_000, out=2_000, cached=13_000),
    # Flashcards: measured 8,889 in / 1,412 out (3 lab sheets) and 43,762 / 1,711 (big reading).
    Action("Flashcards (15 cards)", 0.15, inp=25_000, out=1_600),
    Action("Practice quiz (10 questions)", 0.10, inp=25_000, out=3_500),
    Action("Summary", 0.08, inp=20_000, out=1_200),
    # Scanned PDF read by Claude: ~2,000 tokens/page, 20 pages, ~10k tokens of transcript.
    Action("Read a scanned PDF", 0.02, inp=40_000, out=10_000),
]

def action_cost(a: Action, m: dict) -> float:
    return (a.inp * m["inp"] + a.out * m["out"] + a.cached * m["inp"] * 1.25) / 1e6

def blended(m: dict) -> float:
    return sum(a.share * action_cost(a, m) for a in ACTIONS)

# ---------------------------------------------------------------- scenarios

PLANS_NOW = {"free": (0, 25), "normal": (10, 200), "premium": (20, 600), "pro": (25, 2000)}

def infra_month(students: int, semesters_kept: float = 1.0, snapshot_skip: bool = False) -> dict:
    storage = students * FILES_GB_PER_SEMESTER * semesters_kept
    disk = students * DB_GB_PER_STUDENT
    sync_egress = students * SYNC_READ_MB * SYNCS_PER_DAY * 30 / 1024 * (0.1 if snapshot_skip else 1.0)
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
    return {"supabase": supa, "render": rend, "composio": comp, "egress_gb": egress, "storage_gb": storage,
            "compute": compute, "render_plan": f"{render_n}x {render}"}

def main():
    print("== Cost per AI action ==")
    for mname, m in MODELS.items():
        print(f"\n{mname}: blended ${blended(m):.3f} per action")
        for a in ACTIONS:
            print(f"   {a.name:48} ${action_cost(a, m):.3f}")

    opus = MODELS["Opus 5.5 (current)"]
    print("\n== Worst case: a user who uses the whole monthly AI quota (current plans, Opus 5.5) ==")
    for plan, (price, quota) in PLANS_NOW.items():
        cost = quota * blended(opus)
        fee = stripe_fee(price, True) if price else 0
        print(f"   {plan:8} ${price:>3}/mo, {quota:>4} actions -> AI ${cost:7.2f}, Stripe ${fee:.2f}, margin ${price - cost - fee:8.2f}")

    print("\n== Monthly infrastructure (Supabase Pro + Render + Composio), excluding AI ==")
    for n in (100, 1_000, 5_000, 10_000):
        for skip in (False, True):
            i = infra_month(n, semesters_kept=1.0, snapshot_skip=skip)
            total = i["supabase"] + i["render"] + i["composio"]
            label = "skip unchanged syncs" if skip else "as built"
            print(f"   {n:>6} students ({label:20}): ${total:8.2f}/mo = ${total / n:.3f}/student"
                  f"   [supabase ${i['supabase']:.0f} ({i['compute']}), render ${i['render']:.0f} ({i['render_plan']}),"
                  f" composio ${i['composio']:.0f}, egress {i['egress_gb']:.0f} GB, storage {i['storage_gb']:.0f} GB]")

    print("\n== Whole-business month (4% paid, typical use) ==")
    for n in (100, 1_000, 10_000):
        paid = round(n * 0.04)
        free = n - paid
        for mname in ("Opus 5.5 (current)", "Sonnet 5.5"):
            per = blended(MODELS[mname])
            ai = free * 6 * per + paid * 60 * per          # ASSUME: free users 6 actions, paid 60
            infra = infra_month(n, snapshot_skip=True)
            infra_total = infra["supabase"] + infra["render"] + infra["composio"]
            rev_now = paid * 10                              # everyone on the $10 plan
            rev_pass = paid * 20 / 4                         # $20/semester pass ~ $5/month
            fees_now = paid * stripe_fee(10, True)
            fees_pass = paid * stripe_fee(20, False) / 4
            print(f"   {n:>6} students, {mname:18}: AI ${ai:8.0f}  infra ${infra_total:6.0f}  |"
                  f"  $10/mo plan: revenue ${rev_now:6.0f} net ${rev_now - fees_now - ai - infra_total:8.0f}"
                  f"  |  $20/semester pass: revenue ${rev_pass:6.0f} net ${rev_pass - fees_pass - ai - infra_total:8.0f}")

# ---------------------------------------------------------------- pricing / lever scenarios

GENERATION = {"Flashcards (15 cards)", "Practice quiz (10 questions)", "Summary"}


def blended_with(m: dict, gen_model: dict | None = None, shared: float = 0.0) -> float:
    """Blended action cost when generation can use another model and a share of generations
    reuse a deck a classmate already generated from the same file (cost ~0)."""
    total = 0.0
    for a in ACTIONS:
        if a.name in GENERATION:
            total += a.share * action_cost(a, gen_model or m) * (1 - shared)
        else:
            total += a.share * action_cost(a, m)
    return total


@dataclass
class Scenario:
    name: str
    tutor_model: str
    gen_model: str
    shared: float           # share of generations served from a shared class deck
    free_actions: float     # AI actions an average free user runs a month
    paid_actions: float     # AI actions an average paid user runs a month
    price_month: float      # what a paid user pays per month (semester pass / 4)
    subscription: bool


SCENARIOS = [
    Scenario("As built: Opus, free 25/mo (avg 6), $10/mo", "Opus 5.5 (current)", "Opus 5.5 (current)", 0.0, 6, 60, 10, True),
    Scenario("Sonnet everywhere, free 10/mo (avg 4), $10/mo", "Sonnet 5.5", "Sonnet 5.5", 0.0, 4, 60, 10, True),
    Scenario("Sonnet + shared decks, free 10/mo (avg 4), $20/semester", "Sonnet 5.5", "Sonnet 5.5", 0.5, 4, 50, 5, False),
    Scenario("Free = no AI after a 5-action trial (avg 0.5), Sonnet, $20/semester", "Sonnet 5.5", "Sonnet 5.5", 0.5, 0.5, 50, 5, False),
    Scenario("Same, $6/mo subscription", "Sonnet 5.5", "Sonnet 5.5", 0.5, 0.5, 50, 6, True),
    Scenario("Same, Haiku tutor + Sonnet generation, $20/semester", "Haiku 4.5", "Sonnet 5.5", 0.5, 0.5, 50, 5, False),
]


def scenarios():
    infra_per_student = {n: (lambda i: (i["supabase"] + i["render"] + i["composio"]) / n)(infra_month(n, snapshot_skip=True))
                         for n in (1_000, 10_000)}
    print("\n== Scenarios: per-user economics and the paid share needed to break even ==")
    print(f"   (infra per student with unchanged syncs skipped: ${infra_per_student[1_000]:.3f} at 1k, ${infra_per_student[10_000]:.3f} at 10k)")
    for s in SCENARIOS:
        per = blended_with(MODELS[s.tutor_model], MODELS[s.gen_model], s.shared)
        fee = stripe_fee(s.price_month, True) if s.subscription else stripe_fee(s.price_month * 4, False) / 4
        free_cost = s.free_actions * per + infra_per_student[1_000]
        paid_margin = s.price_month - fee - s.paid_actions * per - infra_per_student[1_000]
        breakeven = free_cost / (paid_margin + free_cost) if paid_margin > 0 else float("inf")
        nets = []
        for n in (1_000, 10_000):
            paid = n * 0.04
            nets.append(paid * paid_margin - (n - paid) * (s.free_actions * per + infra_per_student[n]))
        print(f"   {s.name}")
        print(f"      ${per:.3f}/action | free user costs ${free_cost:.2f}/mo | paid user nets ${paid_margin:.2f}/mo"
              f" | break-even paid share {breakeven * 100:.1f}% | at 4% paid: 1k students ${nets[0]:,.0f}/mo, 10k ${nets[1]:,.0f}/mo")


if __name__ == "__main__":
    main()
    scenarios()
