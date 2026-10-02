from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..services import ai, billing

bp = Blueprint("billing", __name__, url_prefix="/billing")


def _external(endpoint: str, **values) -> str:
    """An absolute link for Stripe to send the student back to: the address they're using when it's
    one of ours (login cookies are per address), else PUBLIC_URL."""
    path = url_for(endpoint, **values)
    cfg = current_app.config
    ours = {urlsplit(u).netloc for u in (cfg.get("PUBLIC_URL"), cfg.get("RENDER_URL")) if u}
    if request.host in ours or not cfg.get("PUBLIC_URL"):
        return request.host_url.rstrip("/") + path
    return cfg["PUBLIC_URL"].rstrip("/") + path


@bp.route("/")
@login_required
def plans():
    status = request.args.get("status")
    if status == "success":
        flash("Thanks! Your plan will update as soon as Stripe confirms the payment.", "success")
    elif status == "cancel":
        flash("Checkout canceled. Nothing was charged.", "info")
    return render_template("billing/plans.html", plans=billing.PLANS.values(), current=billing.plan_for(current_user),
                           enabled=billing.enabled(), used=ai.used(current_user), remaining=ai.remaining(current_user),
                           subscribed=billing.has_active_subscription(current_user))


@bp.route("/checkout/<plan>", methods=["POST"])
@login_required
def checkout(plan: str):
    if not billing.enabled() or plan not in billing.PLANS or plan == "free":
        abort(404)
    if billing.has_active_subscription(current_user) and current_user.stripe_customer_id:
        if billing.PLANS[plan].period == "once":
            flash("You already have Plus. Cancel it in the billing portal first if you'd rather have a Semester Pass.",
                  "info")
            return redirect(url_for("billing.plans"))
        # Changing plans happens in Stripe's portal; a second checkout would add a second subscription.
        return redirect(url_for("billing.portal"))
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
