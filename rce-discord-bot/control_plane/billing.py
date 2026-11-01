"""Stripe billing: Checkout for the subscription, webhooks keep subscriptions.status in sync.

Run / suspend rules (applied by the supervisor's reconcile loop – nothing is ever deleted by billing):
  active, trialing                      -> servers run
  past_due                              -> keep running until grace_until (CP_GRACE_DAYS after it went past due)
  canceled, unpaid, incomplete_expired  -> suspended (processes stopped, data kept)
  paid again / new subscription         -> runs again automatically (reactivation restores everything)
Webhooks are verified with the endpoint secret (Stripe-Signature, HMAC-SHA256, 5-minute tolerance) and processed
once per event id. No Stripe SDK needed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import aiohttp

from control_plane.db import DB

RUNNING = {"active", "trialing"}
TOLERANCE = 300


class BillingError(Exception):
    pass


def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> dict:
    """Checks a Stripe-Signature header; returns the parsed event or raises BillingError."""
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set.")
    parts: dict[str, list[str]] = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise BillingError("Bad signature header.") from None
    if abs((now or time.time()) - ts) > TOLERANCE:
        raise BillingError("Signature too old.")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError("Signature doesn't match.")
    return json.loads(payload)


def subscription_ok(db: DB, customer_id: int, now: float | None = None) -> bool:
    row = db.one("SELECT status, grace_until FROM subscriptions WHERE customer_id = ?", (customer_id,))
    if row is None:
        return False
    if row["status"] in RUNNING or row["status"] == "comped":         # comped: granted by the owner, no Stripe
        return True
    return row["status"] == "past_due" and (row["grace_until"] or 0) > (now or time.time())


def _set(db: DB, customer_id: int, status: str, *, sub_id: str | None, period_end: int | None,
         grace_days: int) -> None:
    old = db.one("SELECT status, grace_until FROM subscriptions WHERE customer_id = ?", (customer_id,))
    grace = old["grace_until"] if old is not None and old["status"] == "past_due" else None
    if status == "past_due" and grace is None:
        grace = int(time.time()) + grace_days * 86400
    db.run("INSERT INTO subscriptions (customer_id, stripe_subscription_id, status, current_period_end, grace_until, "
           "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(customer_id) DO UPDATE SET "
           "stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, stripe_subscription_id), "
           "status = excluded.status, current_period_end = COALESCE(excluded.current_period_end, current_period_end), "
           "grace_until = excluded.grace_until, updated_at = excluded.updated_at",
           (customer_id, sub_id, status, period_end, grace if status == "past_due" else None, int(time.time())))
    db.audit("stripe", "subscription." + status, customer_id=customer_id, detail=sub_id or "")


def _event_plan(db: DB, kind: str, obj: dict) -> int | None:
    """Which package an event is about: checkout carries metadata.plan_id, subscriptions their price."""
    from control_plane import plans
    if kind == "checkout.session.completed":
        pid = str((obj.get("metadata") or {}).get("plan_id") or "")
        return int(pid) if pid.isdigit() and plans.get(db, int(pid)) else None
    if kind in ("customer.subscription.created", "customer.subscription.updated"):
        for item in ((obj.get("items") or {}).get("data") or []):     # the package item (add-ons are others)
            price = (item.get("price") or {}).get("id")
            plan = plans.get(db, stripe_price=price) if price else None
            if plan:
                return plan["id"]
    return None


def handle_event(db: DB, event: dict, *, grace_days: int = 3) -> str:
    """Applies one verified Stripe event. Returns what happened (for logs/tests)."""
    from control_plane import plans
    eid, kind = event.get("id", ""), event.get("type", "")
    if db.one("SELECT 1 FROM stripe_events WHERE id = ?", (eid,)):
        return "duplicate"
    obj = (event.get("data") or {}).get("object") or {}
    result = _handle(db, eid, kind, obj, grace_days)
    plan_id = _event_plan(db, kind, obj)
    if plan_id is not None:                            # the package decides slots and add-ons
        cid = (int(obj.get("client_reference_id") or 0) if kind == "checkout.session.completed" else
               (db.one("SELECT id FROM customers WHERE stripe_customer_id = ?", (obj.get("customer"),)) or {"id": 0})["id"])
        if cid and db.one("SELECT 1 FROM customers WHERE id = ?", (cid,)):
            plans.apply(db, cid, plan_id, actor="stripe")
    if kind.startswith("customer.subscription."):         # the own-domain add-on item in the subscription
        row = db.one("SELECT id FROM customers WHERE stripe_customer_id = ?", (obj.get("customer"),))
        if row is not None:
            prices = {(i.get("price") or {}).get("id") for i in ((obj.get("items") or {}).get("data") or [])}
            alive = not kind.endswith(".deleted") and obj.get("status") in ("active", "trialing", "past_due")
            plans.sync_addon(db, row["id"], alive and bool(prices & plans.addon_price_ids(db)), actor="stripe")
    return result


def _handle(db: DB, eid: str, kind: str, obj: dict, grace_days: int) -> str:
    result = "ignored"
    with db.transaction():
        db.run("INSERT INTO stripe_events (id, received_at) VALUES (?, ?)", (eid, int(time.time())))
        if kind == "checkout.session.completed":
            cid = int((obj.get("client_reference_id") or "0"))
            if db.one("SELECT 1 FROM customers WHERE id = ?", (cid,)):
                db.run("UPDATE customers SET stripe_customer_id = ? WHERE id = ?", (obj.get("customer"), cid))
                _set(db, cid, "active", sub_id=obj.get("subscription"), period_end=None, grace_days=grace_days)
                result = "linked"
        elif kind.startswith("customer.subscription."):
            row = db.one("SELECT id FROM customers WHERE stripe_customer_id = ?", (obj.get("customer"),))
            if row is not None:
                status = "canceled" if kind.endswith(".deleted") else str(obj.get("status") or "")
                _set(db, row["id"], status, sub_id=obj.get("id"), period_end=obj.get("current_period_end"),
                     grace_days=grace_days)
                result = status
        elif kind in ("invoice.paid", "invoice.payment_failed"):
            row = db.one("SELECT id FROM customers WHERE stripe_customer_id = ?", (obj.get("customer"),))
            if row is not None:
                _set(db, row["id"], "active" if kind == "invoice.paid" else "past_due",
                     sub_id=obj.get("subscription"), period_end=None, grace_days=grace_days)
                result = "paid" if kind == "invoice.paid" else "past_due"
    return result


async def checkout_url(settings, customer: dict, success_url: str, cancel_url: str, *, price_id: str = "",
                       plan_id: int | None = None) -> str:
    """A Stripe Checkout page for the subscription (client_reference_id ties it to the customer, metadata to the
    package)."""
    price_id = price_id or settings.stripe_price_id
    if not settings.stripe_secret_key or not price_id:
        raise BillingError("Payments aren't set up yet – please try again later.")
    form = {"mode": "subscription", "line_items[0][price]": price_id, "line_items[0][quantity]": "1",
            "client_reference_id": str(customer["id"]), "success_url": success_url, "cancel_url": cancel_url,
            "allow_promotion_codes": "true"}
    if plan_id is not None:
        form["metadata[plan_id]"] = str(plan_id)
        form["subscription_data[metadata][plan_id]"] = str(plan_id)
    if customer.get("stripe_customer_id"):
        form["customer"] = customer["stripe_customer_id"]
    else:
        form["customer_email"] = customer["email"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post("https://api.stripe.com/v1/checkout/sessions", data=form,
                                auth=aiohttp.BasicAuth(settings.stripe_secret_key, "")) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise BillingError((data.get("error") or {}).get("message", f"Stripe HTTP {resp.status}"))
            return data["url"]


async def portal_url(settings, stripe_customer_id: str, return_url: str) -> str:
    """Stripe's customer portal (update card, cancel) – Stripe handles it, the webhooks bring the result back."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post("https://api.stripe.com/v1/billing_portal/sessions",
                                data={"customer": stripe_customer_id, "return_url": return_url},
                                auth=aiohttp.BasicAuth(settings.stripe_secret_key, "")) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise BillingError((data.get("error") or {}).get("message", f"Stripe HTTP {resp.status}"))
            return data["url"]
