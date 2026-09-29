from __future__ import annotations

from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required

from ..services import citations

bp = Blueprint("tools", __name__, url_prefix="/tools")


def _int(value, low, high):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if low <= v <= high else None


@bp.route("/citations", methods=["GET", "POST"])
@login_required
def citation_generator():
    form = request.form if request.method == "POST" else {}
    results = None
    if request.method == "POST":
        accessed = None
        if form.get("accessed"):
            try:
                accessed = date.fromisoformat(form["accessed"])
            except ValueError:
                accessed = None
        src = citations.Source(
            kind=form.get("kind") if form.get("kind") in {"website", "book", "article"} else "website",
            authors=[a.strip() for a in (form.get("authors") or "").splitlines() if a.strip()],
            title=form.get("title", ""), container=form.get("container", ""), publisher=form.get("publisher", ""),
            year=_int(form.get("year"), 1000, 2100), month=_int(form.get("month"), 1, 12),
            day=_int(form.get("day"), 1, 31), url=form.get("url", ""), accessed=accessed,
            volume=form.get("volume", ""), issue=form.get("issue", ""), pages=form.get("pages", ""),
            doi=form.get("doi", ""), edition=form.get("edition", ""), city=form.get("city", ""))
        results = [(label, fn(src)) for label, fn in citations.STYLES.values()]
    return render_template("tools/citations.html", form=form, results=results, today=date.today().isoformat())
