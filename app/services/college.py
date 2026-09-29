"""College acceptance estimates.

School data comes from the U.S. Department of Education's College Scorecard API (free
key) or can be typed in by hand. The estimate starts from the school's admit rate (the
out-of-state rate for public schools when the student is out of state and that rate is
known), then shifts it by how the student's SAT/ACT compares with the school's middle-50%
range and by GPA. It is a rough, transparent heuristic, shown with its reasoning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import requests
from flask import current_app

SCORECARD_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
FIELDS = [
    "id", "school.name", "school.city", "school.state", "school.ownership",
    "latest.admissions.admission_rate.overall",
    "latest.admissions.sat_scores.25th_percentile.critical_reading",
    "latest.admissions.sat_scores.75th_percentile.critical_reading",
    "latest.admissions.sat_scores.25th_percentile.math",
    "latest.admissions.sat_scores.75th_percentile.math",
    "latest.admissions.act_scores.25th_percentile.cumulative",
    "latest.admissions.act_scores.75th_percentile.cumulative",
]


class CollegeDataError(Exception):
    pass


def scorecard_enabled() -> bool:
    return bool(current_app.config.get("COLLEGE_SCORECARD_API_KEY"))


def _sum(a, b):
    return int(a + b) if a is not None and b is not None else None


def search(name: str, limit: int = 12) -> list[dict]:
    if not scorecard_enabled():
        raise CollegeDataError("School search needs a College Scorecard API key on the server.")
    params = {
        "api_key": current_app.config["COLLEGE_SCORECARD_API_KEY"],
        "school.name": name,
        "school.operating": 1,
        "fields": ",".join(FIELDS),
        "per_page": limit,
        "sort": "latest.student.size:desc",
    }
    try:
        resp = requests.get(SCORECARD_URL, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CollegeDataError("College Scorecard didn't respond. Try again, or enter the school by hand.") from exc
    out = []
    for r in resp.json().get("results", []):
        out.append({
            "scorecard_id": str(r.get("id")),
            "name": r.get("school.name"),
            "city": r.get("school.city"),
            "state": r.get("school.state"),
            "public": r.get("school.ownership") == 1,
            "admit_rate": r.get("latest.admissions.admission_rate.overall"),
            "sat25": _sum(r.get("latest.admissions.sat_scores.25th_percentile.critical_reading"),
                          r.get("latest.admissions.sat_scores.25th_percentile.math")),
            "sat75": _sum(r.get("latest.admissions.sat_scores.75th_percentile.critical_reading"),
                          r.get("latest.admissions.sat_scores.75th_percentile.math")),
            "act25": r.get("latest.admissions.act_scores.25th_percentile.cumulative"),
            "act75": r.get("latest.admissions.act_scores.75th_percentile.cumulative"),
        })
    return out


@dataclass
class Estimate:
    chance: float | None
    label: str
    reasons: list[str]


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def estimate(profile, school) -> Estimate:
    """profile: gpa, gpa_scale, sat, act, home_state. school: admit_rate, oos_admit_rate, public, state, sat/act ranges."""
    reasons: list[str] = []
    base = school.admit_rate
    out_of_state = bool(school.public and profile.home_state and school.state
                        and profile.home_state.upper() != school.state.upper())
    if out_of_state and school.oos_admit_rate:
        base = school.oos_admit_rate
        reasons.append(f"Using the out-of-state admit rate ({base:.0%}) because you're applying from out of state.")
    elif out_of_state:
        reasons.append("Public school, applying out of state: the real rate is often lower than the overall rate.")
    if not base or base <= 0:
        return Estimate(None, "Unknown", ["No admit rate available for this school."])
    base = min(max(base, 0.005), 0.995)
    reasons.insert(0, f"Starting point: the school admits {base:.0%} of applicants.")
    logit = _logit(base)

    z_scores = []
    if profile.sat and school.sat25 and school.sat75 and school.sat75 > school.sat25:
        mid, sd = (school.sat25 + school.sat75) / 2, (school.sat75 - school.sat25) / 1.349
        z_scores.append((profile.sat - mid) / sd)
        reasons.append(f"SAT {profile.sat} vs. middle 50% {school.sat25}–{school.sat75}.")
    if profile.act and school.act25 and school.act75 and school.act75 > school.act25:
        mid, sd = (school.act25 + school.act75) / 2, (school.act75 - school.act25) / 1.349
        z_scores.append((profile.act - mid) / sd)
        reasons.append(f"ACT {profile.act} vs. middle 50% {school.act25}–{school.act75}.")
    if z_scores:
        z = max(-3.0, min(3.0, max(z_scores)))  # the better test counts, as with superscoring/test choice
        logit += 1.1 * z
    else:
        reasons.append("No comparable test scores, so the estimate leans on the admit rate.")

    if profile.gpa and profile.gpa_scale:
        gpa4 = 4.0 * min(profile.gpa / profile.gpa_scale, 1.1)
        g = max(-2.0, min(1.5, (gpa4 - 3.6) / 0.3))
        logit += 0.5 * g
        reasons.append(f"GPA {profile.gpa:g}/{profile.gpa_scale:g} (≈{gpa4:.2f} on a 4.0 scale).")

    chance = 1 / (1 + math.exp(-logit))
    if base < 0.25:
        # Holistic admissions: great numbers can't make a very selective school a sure thing.
        chance = min(chance, base * 3)
    chance = min(max(chance, 0.01), 0.95)

    if base < 0.15:
        label = "Reach"
        reasons.append("Admits under 15%: a reach for every applicant, whatever their numbers.")
    elif chance >= 0.7:
        label = "Safety"
    elif chance >= 0.35:
        label = "Target"
    else:
        label = "Reach"
    return Estimate(round(chance, 3), label, reasons)
