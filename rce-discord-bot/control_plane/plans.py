"""Packages (monthly subscriptions) for the shop: what a customer gets is decided by their package.

  plans(id, slug, name, tagline, price_cents, currency, slots, ai, custom_bot, perks, highlight, sort, active,
        stripe_price_id)

* The owner creates and edits packages in the owner panel (Packages). Saving a package with Stripe set up creates its
  Stripe price by itself (product + monthly price) – no copying of price ids. Changing the price makes a new Stripe
  price; existing subscribers keep theirs until they switch.
* A customer's package sets their entitlements (apply()): the number of server slots (base_slots) and the add-ons
  AI and own bot. Add-ons the OWNER switched on by hand stay; only package-given ones (granted_by "plan") are taken
  away when the package changes. Slot grants the owner made stay on top.
* Packages are never deleted (old subscriptions point at them) – they're switched off (active = 0) instead.
"""
from __future__ import annotations

import re
import time

import aiohttp

from control_plane import features, slots
from control_plane.db import DB

PLAN_OWNER = "plan"                 # granted_by of package-given add-ons
CURRENCIES = {"eur": "€", "usd": "$", "gbp": "£"}
SLUG = re.compile(r"[^a-z0-9]+")
DEFAULTS = (   # a starting point for a new shop – the owner edits the prices and texts
    {"name": "Starter", "tagline": "Everything a community server needs", "price_cents": 799, "slots": 1, "ai": 0,
     "custom_bot": 0, "highlight": 0,
     "perks": "1 Rust Console server\nKits, shop, points, tickets & stats\nFree player website with your own "
              "pictures\nYour own web shop with PayPal\nDiscord bot (ours)"},
    {"name": "Pro", "tagline": "With the AI that does the admin work", "price_cents": 1499, "slots": 2, "ai": 1,
     "custom_bot": 0, "highlight": 1,
     "perks": "2 Rust Console servers\nEverything in Starter\nAI tickets, AI chat & AI moderation\nPriority support"},
    {"name": "Network", "tagline": "For server networks with their own brand", "price_cents": 2999, "slots": 5,
     "ai": 1, "custom_bot": 1, "highlight": 0, "custom_domain": 1,
     "perks": "5 Rust Console servers\nEverything in Pro\nYour own bot (name & avatar)\nYour own domain for the "
              "website\nDirect support"},
)


class PlanError(ValueError):
    pass


def seed(db: DB) -> None:
    """A new shop starts with three example packages (the owner edits them)."""
    if db.one("SELECT 1 FROM plans"):
        return
    for i, p in enumerate(DEFAULTS):
        db.run("INSERT INTO plans (slug, name, tagline, price_cents, slots, ai, custom_bot, custom_domain, perks, "
               "highlight, sort, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (p["name"].lower(), p["name"], p["tagline"], p["price_cents"], p["slots"], p["ai"], p["custom_bot"],
                p.get("custom_domain", 0), p["perks"], p["highlight"], i, int(time.time())))


def listed(db: DB, *, all_: bool = False) -> list[dict]:
    rows = db.q("SELECT * FROM plans" + ("" if all_ else " WHERE active = 1") + " ORDER BY sort, price_cents, id")
    return [dict(r) for r in rows]


def get(db: DB, plan_id=None, *, slug: str | None = None, stripe_price: str | None = None) -> dict | None:
    if slug is not None:
        row = db.one("SELECT * FROM plans WHERE slug = ?", (slug,))
    elif stripe_price is not None:
        row = db.one("SELECT * FROM plans WHERE stripe_price_id = ?", (stripe_price,))
    else:
        row = db.one("SELECT * FROM plans WHERE id = ?", (int(plan_id or 0),))
    return dict(row) if row else None


def price_text(plan: dict) -> str:
    sym = CURRENCIES.get(plan["currency"], plan["currency"].upper() + " ")
    return f"{sym}{plan['price_cents'] / 100:.2f}"


def save(db: DB, form: dict, *, actor: str, plan_id: int | None = None) -> int:
    """Creates or updates a package from the owner's form. Returns its id."""
    name = " ".join(str(form.get("name", "")).split())[:40]
    if len(name) < 2:
        raise PlanError("Give the package a name.")
    try:
        price = round(float(str(form.get("price", "")).replace(",", ".")) * 100)
        n_slots = int(form.get("slots") or 1)
        sort = int(form.get("sort") or 0)
    except ValueError:
        raise PlanError("Price, servers and order must be numbers.") from None
    if not 50 <= price <= 100_000:
        raise PlanError("The price must be between 0.50 and 1000 a month.")
    if not 1 <= n_slots <= 100:
        raise PlanError("1 to 100 servers per package.")
    currency = str(form.get("currency") or "eur").lower()
    if currency not in CURRENCIES:
        raise PlanError("Currency: EUR, USD or GBP.")
    values = {"name": name, "tagline": str(form.get("tagline", "")).strip()[:120], "price_cents": price,
              "currency": currency, "slots": n_slots, "ai": int(bool(form.get("ai"))),
              "custom_bot": int(bool(form.get("custom_bot"))), "highlight": int(bool(form.get("highlight"))),
              "custom_domain": int(bool(form.get("custom_domain"))),
              "perks": "\n".join(line.strip()[:80] for line in str(form.get("perks", "")).splitlines()
                                 if line.strip())[:1000], "sort": sort, "active": int(bool(form.get("active")))}
    if plan_id is None:
        slug = SLUG.sub("-", name.lower()).strip("-")[:30] or "plan"
        base, n = slug, 2
        while db.one("SELECT 1 FROM plans WHERE slug = ?", (slug,)):
            slug, n = f"{base}-{n}", n + 1
        cur = db.run("INSERT INTO plans (slug, created_at, " + ", ".join(values) + ") VALUES (?, ?, "
                     + ", ".join("?" * len(values)) + ")", (slug, int(time.time()), *values.values()))
        plan_id = int(cur.lastrowid)
        db.audit(actor, "plan.create", detail=f"{slug} {price_text(get(db, plan_id))}")
    else:
        if get(db, plan_id) is None:
            raise PlanError("No such package.")
        db.run("UPDATE plans SET " + ", ".join(f"{k} = ?" for k in values) + " WHERE id = ?",
               (*values.values(), plan_id))
        db.audit(actor, "plan.update", detail=f"#{plan_id} {name} {price_text(get(db, plan_id))}")
    return plan_id


async def ensure_stripe_price(db: DB, settings, plan_id: int) -> str | None:
    """Creates the package's monthly Stripe price (and product) when it has none or the price changed."""
    plan = get(db, plan_id)
    if plan is None or not settings.stripe_secret_key:
        return None
    if plan["stripe_price_id"] and plan["stripe_price_cents"] == plan["price_cents"]:
        return plan["stripe_price_id"]
    form = {"currency": plan["currency"], "unit_amount": str(plan["price_cents"]), "recurring[interval]": "month",
            "product_data[name]": f"{plan['name']} package", "metadata[plan_id]": str(plan["id"])}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post("https://api.stripe.com/v1/prices", data=form,
                                auth=aiohttp.BasicAuth(settings.stripe_secret_key, "")) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise PlanError("Stripe: " + (data.get("error") or {}).get("message", f"HTTP {resp.status}"))
    db.run("UPDATE plans SET stripe_price_id = ?, stripe_price_cents = ? WHERE id = ?",
           (data["id"], plan["price_cents"], plan_id))
    return data["id"]


def current(db: DB, customer_id: int) -> dict | None:
    row = db.one("SELECT plan_id FROM subscriptions WHERE customer_id = ?", (customer_id,))
    return get(db, row["plan_id"]) if row and row["plan_id"] else None


def apply(db: DB, customer_id: int, plan_id: int, *, actor: str) -> None:
    """The customer's package decides their server slots and the AI / own-bot add-ons."""
    plan = get(db, plan_id)
    if plan is None:
        raise PlanError("No such package.")
    db.run("INSERT INTO subscriptions (customer_id, status, updated_at, plan_id) VALUES (?, 'none', ?, ?) "
           "ON CONFLICT(customer_id) DO UPDATE SET plan_id = excluded.plan_id", (customer_id, int(time.time()), plan_id))
    db.run("UPDATE customers SET base_slots = ? WHERE id = ?", (plan["slots"], customer_id))
    for feat in ("ai", "custom_bot", "custom_domain"):
        row = db.one("SELECT granted_by FROM features WHERE customer_id = ? AND feature = ? AND revoked_at IS NULL",
                     (customer_id, feat))
        if plan[feat] and row is None:
            features.grant(db, customer_id, feat, owner=PLAN_OWNER)
        elif not plan[feat] and row is not None and row["granted_by"] == PLAN_OWNER:
            features.revoke(db, customer_id, feat, owner=PLAN_OWNER)     # only what the package gave
    slots.release_over_limit(db, customer_id, actor=actor)
    slots.enforce(db, customer_id, actor=actor)
    db.audit(actor, "plan.apply", customer_id=customer_id, detail=f"{plan['slug']} ({plan['slots']} slots)")


async def switch_subscription(settings, subscription_id: str, price_id: str) -> None:
    """A running Stripe subscription moves to another package's price (prorated); the webhook applies it."""
    auth = aiohttp.BasicAuth(settings.stripe_secret_key, "")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(f"https://api.stripe.com/v1/subscriptions/{subscription_id}", auth=auth) as resp:
            sub = await resp.json(content_type=None)
            if resp.status != 200:
                raise PlanError("Stripe: " + (sub.get("error") or {}).get("message", f"HTTP {resp.status}"))
        item = sub["items"]["data"][0]["id"]
        async with session.post(f"https://api.stripe.com/v1/subscriptions/{subscription_id}", auth=auth,
                                data={"items[0][id]": item, "items[0][price]": price_id,
                                      "proration_behavior": "create_prorations"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise PlanError("Stripe: " + (data.get("error") or {}).get("message", f"HTTP {resp.status}"))


# ---------------------------------------------------------------- the premium add-on: own domain

ADDON_OWNER = "addon"               # granted_by of a bought add-on (a package change never takes it away)


def addon_price(db: DB, settings) -> dict | None:
    """The own-domain add-on's price: {"cents", "currency", "text"} or None if it isn't for sale."""
    raw = str(settings.domain_price or "").replace(",", ".").strip()
    try:
        cents = round(float(raw) * 100) if raw else 0
    except ValueError:
        return None
    if not 50 <= cents <= 100_000:
        return None
    first = db.one("SELECT currency FROM plans WHERE active = 1 ORDER BY sort, id")
    currency = first["currency"] if first else "eur"
    return {"cents": cents, "currency": currency,
            "text": f"{CURRENCIES.get(currency, currency.upper() + ' ')}{cents / 100:.2f}"}


async def ensure_addon_stripe_price(db: DB, settings) -> str | None:
    import json
    price = addon_price(db, settings)
    if price is None or not settings.stripe_secret_key:
        return None
    row = db.one("SELECT value FROM settings_kv WHERE key = 'addon_domain_price'")
    saved = json.loads(row["value"]) if row else {}
    if saved.get("cents") == price["cents"] and saved.get("currency") == price["currency"] and saved.get("id"):
        return saved["id"]
    if saved.get("id"):                              # old price: its subscribers keep the add-on
        db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
               (f"addon_domain_old:{saved['id']}", json.dumps(saved)))
    form = {"currency": price["currency"], "unit_amount": str(price["cents"]), "recurring[interval]": "month",
            "product_data[name]": "Own domain add-on", "metadata[addon]": "custom_domain"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post("https://api.stripe.com/v1/prices", data=form,
                                auth=aiohttp.BasicAuth(settings.stripe_secret_key, "")) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise PlanError("Stripe: " + (data.get("error") or {}).get("message", f"HTTP {resp.status}"))
    db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('addon_domain_price', ?)",
           (json.dumps({"id": data["id"], "cents": price["cents"], "currency": price["currency"]}),))
    return data["id"]


def addon_price_ids(db: DB) -> set[str]:
    """Every Stripe price the own-domain add-on ever had (an old price still counts for old subscribers)."""
    import json
    ids = set()
    for row in db.q("SELECT value FROM settings_kv WHERE key = 'addon_domain_price' OR key LIKE 'addon_domain_old:%'"):
        try:
            ids.add(json.loads(row["value"])["id"])
        except (ValueError, KeyError):
            pass
    return ids


def sync_addon(db: DB, customer_id: int, has_addon: bool, *, actor: str) -> None:
    """The subscription has (or doesn't have) the own-domain add-on item: grant or take back what it gave."""
    row = db.one("SELECT granted_by FROM features WHERE customer_id = ? AND feature = 'custom_domain' AND "
                 "revoked_at IS NULL", (customer_id,))
    if has_addon and row is None:
        features.grant(db, customer_id, "custom_domain", owner=ADDON_OWNER)
        db.audit(actor, "addon.domain.on", customer_id=customer_id)
    elif not has_addon and row is not None and row["granted_by"] == ADDON_OWNER:
        features.revoke(db, customer_id, "custom_domain", owner=ADDON_OWNER)
        db.audit(actor, "addon.domain.off", customer_id=customer_id)


async def change_addon(settings, subscription_id: str, price_id: str, add: bool) -> None:
    """Adds the add-on to a running subscription (prorated) or removes it; the webhook applies the change."""
    auth = aiohttp.BasicAuth(settings.stripe_secret_key, "")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        if add:
            async with session.post("https://api.stripe.com/v1/subscription_items", auth=auth,
                                    data={"subscription": subscription_id, "price": price_id, "quantity": "1",
                                          "proration_behavior": "create_prorations"}) as resp:
                data = await resp.json(content_type=None)
        else:
            async with session.get(f"https://api.stripe.com/v1/subscriptions/{subscription_id}", auth=auth) as resp:
                sub = await resp.json(content_type=None)
            item = next((i["id"] for i in (sub.get("items") or {}).get("data", [])
                         if (i.get("price") or {}).get("id") == price_id), None)
            if item is None:
                return
            async with session.delete(f"https://api.stripe.com/v1/subscription_items/{item}", auth=auth,
                                      params={"proration_behavior": "create_prorations"}) as resp:
                data = await resp.json(content_type=None)
        if resp.status != 200:
            raise PlanError("Stripe: " + (data.get("error") or {}).get("message", f"HTTP {resp.status}"))
