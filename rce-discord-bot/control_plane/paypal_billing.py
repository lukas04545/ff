"""PayPal for the bot itself: a customer pays 1, 3, 6 or 12 months of a package in advance.

  dashboard → "Pay with PayPal" (package + months) → PayPal → back to /billing/paypal/return → the payment is
  captured (the amount PayPal reports is checked) → subscriptions.status = "paid" until current_period_end.

Paying again extends from the current end (nothing is lost by paying early). When the paid time is over, the servers
stop like after a free trial (data kept) and run again with the next payment. Buyers who paid but closed the page
before coming back are completed on their next dashboard visit (and by check_all). Unpaid orders expire after 3 h.
A customer with a running Stripe subscription can't also pay with PayPal (no double billing).
"""
from __future__ import annotations

import secrets
import time

from control_plane import plans
from control_plane.db import DB

MONTHS = (1, 3, 6, 12)
MONTH = 30 * 86400
EXPIRE = 3 * 3600
_client = None


class PayPalBillingError(Exception):
    pass


def configured(settings) -> bool:
    return bool(settings.paypal_client_id and settings.paypal_secret)


def client(settings):
    """The PayPal API client (support/paypal.py, the same the web store uses) – rebuilt when the keys change."""
    global _client
    from support.paypal import PayPalClient
    key = (settings.paypal_client_id, settings.paypal_secret, settings.paypal_mode)
    if _client is None or _client[0] != key:
        _client = (key, PayPalClient(*key))
    return _client[1]


def amount(plan: dict, months: int) -> float:
    return round(plan["price_cents"] * int(months) / 100, 2)


def _has_stripe(db: DB, customer_id: int) -> bool:
    row = db.one("SELECT status, stripe_subscription_id FROM subscriptions WHERE customer_id = ?", (customer_id,))
    return bool(row and row["stripe_subscription_id"] and row["status"] in ("active", "trialing", "past_due"))


def paid_until(db: DB, customer_id: int) -> int | None:
    row = db.one("SELECT status, current_period_end FROM subscriptions WHERE customer_id = ?", (customer_id,))
    return int(row["current_period_end"]) if row and row["status"] == "paid" and row["current_period_end"] else None


async def start(db: DB, settings, customer: dict, plan_id: int, months: int) -> str:
    """Creates the PayPal order and returns the link where the customer approves the payment."""
    if not configured(settings):
        raise PayPalBillingError("PayPal isn't set up yet – please use another way to pay or contact us.")
    if int(months) not in MONTHS:
        raise PayPalBillingError("Pick 1, 3, 6 or 12 months.")
    plan = plans.get(db, int(plan_id))
    if plan is None or not plan["active"]:
        raise PayPalBillingError("That package isn't available.")
    if _has_stripe(db, customer["id"]):
        raise PayPalBillingError("You already pay by card (Stripe) – manage it under Invoices & payment.")
    total = amount(plan, months)
    token = secrets.token_urlsafe(18)
    cur = db.run("INSERT INTO paypal_payments (token, customer_id, plan_id, months, amount, currency, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (token, customer["id"], plan["id"], int(months), f"{total:.2f}",
                                                 plan["currency"].upper(), int(time.time())))
    pid = cur.lastrowid
    base = settings.portal_url
    from support.paypal import PayPalError
    try:
        order_id, url = await client(settings).create_order(
            amount=total, currency=plan["currency"].upper(),
            description=f"{plan['name']} – {months} month{'s' if int(months) != 1 else ''}"[:120],
            reference=f"bot-{pid}", brand=settings.shop_name or "RCE Bot",
            return_url=f"{base}/billing/paypal/return?o={token}", cancel_url=f"{base}/billing/paypal/cancel?o={token}")
    except PayPalError as exc:
        db.run("UPDATE paypal_payments SET status = 'cancelled' WHERE id = ?", (pid,))
        raise PayPalBillingError(f"PayPal isn't available right now ({exc}) – try again in a few minutes.") from None
    db.run("UPDATE paypal_payments SET paypal_order = ? WHERE id = ?", (order_id, pid))
    db.audit(f"customer:{customer['id']}", "billing.paypal_started", customer_id=customer["id"],
             detail=f"{plan['slug']} × {months} = {total:.2f} {plan['currency'].upper()}")
    return url


def _apply(db: DB, pay) -> int:
    """The paid months: the package (slots, add-ons) and the time – from the current end if still running."""
    plans.apply(db, pay["customer_id"], pay["plan_id"], actor="paypal")
    now = int(time.time())
    row = db.one("SELECT status, current_period_end FROM subscriptions WHERE customer_id = ?", (pay["customer_id"],))
    running_end = int(row["current_period_end"] or 0) if row and row["status"] in ("paid", "trial") else 0
    until = max(now, running_end) + int(pay["months"]) * MONTH
    db.run("UPDATE subscriptions SET status = 'paid', current_period_end = ?, grace_until = NULL, updated_at = ? "
           "WHERE customer_id = ?", (until, now, pay["customer_id"]))
    db.audit("paypal", "subscription.paid", customer_id=pay["customer_id"],
             detail=f"{pay['months']} month(s), {pay['amount']} {pay['currency']}, until "
                    f"{time.strftime('%Y-%m-%d', time.gmtime(until))}")
    return until


async def complete(db: DB, settings, token: str):
    """Back from PayPal: capture and apply (once). Returns the payment row (or None)."""
    pay = db.one("SELECT * FROM paypal_payments WHERE token = ?", (str(token or "")[:64],))
    if pay is None or pay["status"] != "pending" or not pay["paypal_order"] or not configured(settings):
        return pay
    from support.paypal import PayPalError
    try:
        cap = await client(settings).capture(pay["paypal_order"], amount=float(pay["amount"]), currency=pay["currency"])
    except PayPalError:
        return pay
    if cap.ok:
        # claim it first (only one request can): then the months are added exactly once
        if db.run("UPDATE paypal_payments SET status = 'paid', capture_id = ?, paid_at = ? WHERE id = ? AND "
                  "status = 'pending'", (cap.capture_id, int(time.time()), pay["id"])).rowcount:
            until = _apply(db, pay)
            db.run("UPDATE paypal_payments SET until = ? WHERE id = ?", (until, pay["id"]))
    elif cap.status != "ORDER_NOT_APPROVED":                  # refused or waiting: the owner looks at it
        if db.run("UPDATE paypal_payments SET status = 'review', capture_id = ? WHERE id = ? AND status = 'pending'",
                  (cap.capture_id, pay["id"])).rowcount:
            db.audit("paypal", "billing.paypal_review", customer_id=pay["customer_id"],
                     detail=f"{cap.problem or cap.status} – PayPal order {pay['paypal_order']}")
    return db.one("SELECT * FROM paypal_payments WHERE id = ?", (pay["id"],))


def cancel(db: DB, token: str) -> None:
    db.run("UPDATE paypal_payments SET status = 'cancelled' WHERE token = ? AND status = 'pending'",
           (str(token or "")[:64],))


async def check_pending(db: DB, settings, customer_id: int | None = None, now: float | None = None) -> int:
    """Paid on PayPal but never came back: completes it. Never paid for 3 h: cancelled. Returns how many completed."""
    now = now or time.time()
    done = 0
    rows = db.q("SELECT * FROM paypal_payments WHERE status = 'pending'"
                + (" AND customer_id = ?" if customer_id is not None else ""),
                (customer_id,) if customer_id is not None else ())
    from support.paypal import PayPalError
    for pay in rows:
        if pay["paypal_order"] and configured(settings) and now - pay["created_at"] > 30:
            try:
                state = (await client(settings).get_order(pay["paypal_order"])).get("status")
            except PayPalError:
                state = None
            if state in ("APPROVED", "COMPLETED"):
                done += (await complete(db, settings, pay["token"]))["status"] == "paid"
                continue
        if now - pay["created_at"] > EXPIRE:
            db.run("UPDATE paypal_payments SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (pay["id"],))
    return done
