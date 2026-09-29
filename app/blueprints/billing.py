from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..services import ai, billing

bp = Blueprint("billing", __name__, url_prefix="/billing")


def _external(endpoint: str, **values) -> str:
    base = current_app.config.get("PUBLIC_URL")
    path = url_for(endpoint, **values)
    return f"{base}{path}" if base else url_for(endpoint, _external=True, **values)


@bp.route("/")
@login_required
def plans():
    status = request.args.get("status")
    if status == "success":
        flash("Thanks! Your plan will update as soon as Stripe confirms the payment.", "success")
    elif status == "cancel":
        flash("Checkout canceled. Nothing was charged.", "info")
    return render_template("billing/plans.html", plans=billing.PLANS.values(), current=billing.plan_for(current_user),
                           enabled=billing.enabled(), used=ai.used_this_month(current_user.id),
                           remaining=ai.remaining(current_user))


@bp.route("/checkout/<plan>", methods=["POST"])
@login_required
def checkout(plan: str):
    if not billing.enabled() or plan not in billing.PLANS or plan == "free":
        abort(404)
    try:
        url = billing.checkout_url(current_user, plan, _external("billing.plans", status="success"),
                                   _external("billing.plans", status="cancel"))
    except Exception as exc:  # Stripe errors, missing price ids
        current_app.logger.error("checkout failed: %s", exc)
        flash("Couldn't start checkout. Please try again later.", "error")
        return redirect(url_for("billing.plans"))
    return redirect(url, code=303)


@bp.route("/portal")
@login_required
def portal():
    if not billing.enabled() or not current_user.stripe_customer_id:
        abort(404)
    return redirect(billing.portal_url(current_user, _external("billing.plans")), code=303)


@bp.route("/webhook", methods=["POST"])
def webhook():
    secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        abort(404)
    import stripe

    payload = request.get_data()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("Stripe-Signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError):
        return jsonify({"error": "bad signature"}), 400
    outcome = billing.handle_event(event.to_dict() if hasattr(event, "to_dict") else dict(event))
    current_app.logger.info("stripe webhook: %s", outcome)
    return jsonify({"ok": True})
