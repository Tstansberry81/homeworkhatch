"""Plans and Stripe subscriptions.

Plans mirror the original pricing (Normal $10 / Premium $20 / Pro $25) plus a free tier.
They differ in monthly AI actions. Without Stripe keys, billing is simply switched off.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from sqlalchemy import select

from ..extensions import db
from ..models import User


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price: int  # USD per month
    ai_monthly: int
    blurb: str
    price_config: str | None = None

    @property
    def stripe_price(self) -> str:
        return current_app.config.get(self.price_config or "", "") if self.price_config else ""


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", 0, 25, "Canvas sync, planner, flashcards, chat and games, plus 25 AI actions a month."),
    "normal": Plan("normal", "Normal", 10, 200, "200 AI actions a month for tutoring, summaries and generated study sets.",
                   "STRIPE_PRICE_NORMAL"),
    "premium": Plan("premium", "Premium", 20, 600, "600 AI actions a month for heavy study weeks.", "STRIPE_PRICE_PREMIUM"),
    "pro": Plan("pro", "Pro", 25, 2000, "2,000 AI actions a month. Effectively unlimited for one student.",
                "STRIPE_PRICE_PRO"),
}

ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def plan_for(user: User) -> Plan:
    plan = PLANS.get(user.plan or "free", PLANS["free"])
    if plan.key != "free" and not user.plan_comped and user.plan_status not in ACTIVE_STATUSES:
        return PLANS["free"]
    return plan


def enabled() -> bool:
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


def _stripe():
    import stripe

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


def checkout_url(user: User, plan_key: str, success_url: str, cancel_url: str) -> str:
    plan = PLANS[plan_key]
    if not plan.stripe_price:
        raise ValueError(f"No Stripe price configured for {plan.name}")
    stripe = _stripe()
    params = dict(mode="subscription", line_items=[{"price": plan.stripe_price, "quantity": 1}],
                  client_reference_id=str(user.id), success_url=success_url, cancel_url=cancel_url,
                  metadata={"user_id": str(user.id), "plan": plan_key},
                  subscription_data={"metadata": {"user_id": str(user.id), "plan": plan_key}})
    if user.stripe_customer_id:
        params["customer"] = user.stripe_customer_id
    else:
        params["customer_email"] = user.email
    return stripe.checkout.Session.create(**params).url


def portal_url(user: User, return_url: str) -> str:
    stripe = _stripe()
    return stripe.billing_portal.Session.create(customer=user.stripe_customer_id, return_url=return_url).url


def _plan_from_price(price_id: str | None) -> str | None:
    for plan in PLANS.values():
        if plan.stripe_price and plan.stripe_price == price_id:
            return plan.key
    return None


def handle_event(event: dict) -> str:
    """Apply a verified Stripe webhook event. Returns a short description for logs."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        ref = str(obj.get("client_reference_id") or "")
        user = db.session.get(User, int(ref)) if ref.isdigit() else None
        if user is None:
            return "checkout for unknown user"
        user.stripe_customer_id = obj.get("customer") or user.stripe_customer_id
        user.stripe_subscription_id = obj.get("subscription") or user.stripe_subscription_id
        plan = (obj.get("metadata") or {}).get("plan")
        if plan in PLANS:
            user.plan = plan
            user.plan_status = "active"
        db.session.commit()
        return f"user {user.id} subscribed to {plan}"
    if kind in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        user = db.session.scalar(select(User).where(User.stripe_customer_id == obj.get("customer")))
        if user is None:
            meta_user = (obj.get("metadata") or {}).get("user_id")
            user = db.session.get(User, int(meta_user)) if meta_user and str(meta_user).isdigit() else None
        if user is None:
            return "subscription for unknown customer"
        tracked = user.stripe_subscription_id
        if tracked and obj.get("id") != tracked and user.plan_status in ACTIVE_STATUSES:
            # Another subscription on the same customer (e.g. an old or duplicate one): it must
            # not change the plan of the one the user is actually paying for.
            return f"ignored {kind} for untracked subscription {obj.get('id')}"
        status = "canceled" if kind.endswith("deleted") else obj.get("status")
        items = ((obj.get("items") or {}).get("data") or [])
        price_id = ((items[0].get("price") or {}).get("id")) if items else None
        plan = _plan_from_price(price_id) or (obj.get("metadata") or {}).get("plan")
        user.stripe_subscription_id = obj.get("id")
        user.plan_status = status
        if status in ACTIVE_STATUSES and plan in PLANS:
            user.plan = plan
        elif status not in ACTIVE_STATUSES and not user.plan_comped:
            user.plan = "free"
        db.session.commit()
        return f"user {user.id} subscription {status}"
    return f"ignored {kind}"


def has_active_subscription(user: User) -> bool:
    return bool(user.stripe_subscription_id) and user.plan_status in ACTIVE_STATUSES and not user.plan_comped


def cancel_subscription(user: User) -> None:
    """Used when an account is deleted, so the card isn't charged for a deleted account."""
    if not enabled() or not user.stripe_subscription_id:
        return
    _stripe().Subscription.cancel(user.stripe_subscription_id)
