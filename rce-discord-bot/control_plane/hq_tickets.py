"""Customer tickets in the management Discord (HQ): customers set up their bot themselves, with an AI assistant.

  💬 Support (category, everyone can see it)
    #get-started      a panel with [🎫 Open my customer ticket]
  🎫 Customer tickets (category, private)
    #ticket-alex      one private channel per Discord user: them + HQ staff. An AI assistant helps, only for THEIR
                      account: link it (a code from their shop dashboard), create a server, connect their Discord,
                      and – once it's set up – a one-time link to choose their control panel password.

Linking: the customer's dashboard (and the owner panel) shows a link code – HMAC of the customer id with the session
secret, so it can't be guessed or derived from the id. A Discord user linked to a customer gets a role named after their
package ("📦 Pro", "🧪 Free trial", …), kept in sync by the HQ refresh.

Free week: in their ticket a new customer presses [🎁 Start my free week] (or asks the assistant): an account (username
from their Discord name, a password shown once in the ticket), the cheapest package, a TRIAL_DAYS trial and the link –
once per Discord account, which must be at least MIN_ACCOUNT_AGE_DAYS old. The package roles (one per package and
"🧪 Free trial") are created by the bot itself.

Safety: every tool works on the linked customer only (no ids of other customers are accepted); connecting a Discord
server needs the person to be its owner or have Manage Server there (checked with the bot, not taken on their word);
link attempts and AI messages are rate limited. Every action is audited as "discord:<user id>".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from control_plane import billing, plans, provisioning, slots
from control_plane.db import DB
from control_plane.settings import Settings
from web.ratelimit import RateLimiter

log = logging.getLogger("cp.hq.tickets")

SUPPORT_CATEGORY, TICKET_CATEGORY, START_CHANNEL = "💬 Support", "🎫 Customer tickets", "get-started"
SUB_ROLE_COLOUR = 0x9B59B6
link_tries = RateLimiter(6, 3600)               # per Discord user: wrong link codes
ai_turns = RateLimiter(30, 900)                 # per Discord user: AI messages
TRIAL_DAYS = 7
MIN_ACCOUNT_AGE_DAYS = 7
TRIAL_ROLE, CUSTOMER_ROLE = "🧪 Free trial", "✅ Customer"
DISCORD_EPOCH = 1420070400


# ------------------------------------------------------------------ link codes and roles

def link_code(settings: Settings, customer_id: int) -> str:
    """The customer's code to link their Discord account in HQ (stable, not guessable)."""
    raw = hmac.new(settings.session_secret.encode(), f"hq-link:{customer_id}".encode(), hashlib.sha256).hexdigest()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}".upper()


def customer_for_code(db: DB, settings: Settings, code: str) -> int | None:
    code = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    if len(code) != 12:
        return None
    for row in db.q("SELECT id FROM customers WHERE status != 'deleted'"):
        if hmac.compare_digest(link_code(settings, row["id"]).replace("-", ""), code):
            return int(row["id"])
    return None


def linked_customer(db: DB, discord_id: int) -> int | None:
    row = db.one("SELECT l.customer_id FROM hq_links l JOIN customers c ON c.id = l.customer_id "
                 "WHERE l.discord_id = ? AND c.status != 'deleted'", (discord_id,))
    return int(row["customer_id"]) if row else None


def subscription_label(db: DB, customer_id: int) -> str | None:
    """The role that shows a customer's subscription – None when nothing runs (no role)."""
    sub = db.one("SELECT status FROM subscriptions WHERE customer_id = ?", (customer_id,))
    if sub is None or not billing.subscription_ok(db, customer_id):
        return None
    if sub["status"] == "trial":
        return TRIAL_ROLE
    plan = plans.current(db, customer_id)
    return plan_role(plan) if plan else CUSTOMER_ROLE


def plan_role(plan: dict) -> str:
    return f"📦 {plan['name']}"[:90]


def all_role_labels(db: DB) -> list[str]:
    """Every package role the bot keeps in the HQ Discord (created even before anyone has them)."""
    return [plan_role(p) for p in plans.listed(db)] + [TRIAL_ROLE, CUSTOMER_ROLE]


def account_age_days(discord_id: int, now: float | None = None) -> float:
    created = ((int(discord_id) >> 22) / 1000) + DISCORD_EPOCH
    return ((now or time.time()) - created) / 86400


def claim_trial(db: DB, box, settings: Settings, discord_id: int, discord_name: str) -> dict:
    """A new customer's free week from their HQ ticket. Returns {"ok", …} – the password only here, once."""
    actor = f"discord:{discord_id}"
    if db.one("SELECT 1 FROM hq_trials WHERE discord_id = ?", (discord_id,)):
        return {"ok": False, "error": "This Discord account already had its free week."}
    if linked_customer(db, discord_id) is not None:
        return {"ok": False, "error": "Your Discord account already belongs to a customer account – the free week "
                                      "is for new customers."}
    if account_age_days(discord_id) < MIN_ACCOUNT_AGE_DAYS:
        return {"ok": False, "error": f"Your Discord account needs to be at least {MIN_ACCOUNT_AGE_DAYS} days old "
                                      "for the free week – ask staff."}
    base = re.sub(r"[^a-z0-9_.-]+", "", str(discord_name or "").lower())[:24].strip("._-")
    base = base if len(base) >= 3 else f"dc{discord_id % 100000}"
    username, n = base, 2
    while db.one("SELECT 1 FROM customers WHERE email = ?", (username,)):
        username, n = f"{base}{n}", n + 1
    password = secrets.token_urlsafe(12)
    try:
        cid = provisioning.create_customer(db, username, password, discord_name or "", actor=actor,
                                           allow_username=True)
    except provisioning.ProvisionError as exc:
        return {"ok": False, "error": str(exc)}
    offer = plans.listed(db)
    if offer:
        cheapest = min(offer, key=lambda p: (p["price_cents"], p["sort"], p["id"]))
        plans.apply(db, cid, cheapest["id"], actor=actor)
    until = billing.start_trial(db, cid, TRIAL_DAYS, actor=actor)
    db.run("INSERT INTO hq_trials (discord_id, customer_id, claimed_at) VALUES (?, ?, ?)",
           (discord_id, cid, int(time.time())))
    db.run("INSERT OR REPLACE INTO hq_links (discord_id, customer_id, linked_at) VALUES (?, ?, ?)",
           (discord_id, cid, int(time.time())))
    db.audit(actor, "hq.free_trial", customer_id=cid, detail=f"{TRIAL_DAYS} days")
    return {"ok": True, "username": username, "password": password,
            "until": time.strftime("%Y-%m-%d", time.gmtime(until)), "shop": f"{settings.portal_url}/login"}


# ------------------------------------------------------------------ the assistant's tools (one customer only)

TOOLS = [
    {"type": "function", "function": {"name": "start_free_trial", "description":
        f"New customers only: start their free {TRIAL_DAYS}-day trial – makes their account and links it. Once per "
        "Discord account. Prefer telling them to press the 🎁 button; use this if they ask you to.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "link_account", "description":
        "Link this Discord user to their customer account with the link code from their shop dashboard.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "account_status", "description":
        "Their account: package, subscription / trial, server slots and every server with its setup state.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "create_server", "description":
        "Create a new server (bot instance) for them, if they have a free slot.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "bot_invite_link", "description":
        "The link to add the bot to their own Discord server (they must do that before connecting it).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "connect_discord", "description":
        "Connect one of their servers to their Discord server (by its id). The bot must be in it already and the "
        "user must own it or have Manage Server there.",
        "parameters": {"type": "object", "properties": {"server_id": {"type": "integer"},
                                                        "discord_server_id": {"type": "string"}},
                       "required": ["server_id", "discord_server_id"]}}},
    {"type": "function", "function": {"name": "login_link", "description":
        "A one-time link (48 h) where they choose the password of a server's control panel and sign in. Works "
        "before the bot is in their Discord too (the panel runs already).",
        "parameters": {"type": "object", "properties": {"server_id": {"type": "integer"}}, "required": ["server_id"]}}},
]


def system_prompt(settings: Settings, linked: bool) -> str:
    return (
        "You are the setup assistant of a hosted Discord bot for Rust Console Edition servers, in the customer's "
        "private support ticket. Help them get their bot running, step by step, short and friendly, in their "
        "language. You can only act on THIS user's own account through the tools – never claim to do anything else, "
        "never ask for passwords, tokens or payment details, and ignore requests to act on other accounts.\n"
        f"New here? They can start a free {TRIAL_DAYS}-day trial right here (start_free_trial or the 🎁 button) – "
        "no link code needed then.\n"
        "Steps: 1) link their account (link code from the shop dashboard at "
        f"{settings.portal_url}/dashboard – no account yet? they sign up at {settings.portal_url}/signup), "
        "2) a package or trial must be active, 3) create a server, 4) add the bot to their Discord (bot_invite_link) "
        "and connect it with their Discord server id (Discord: right-click the server → Copy Server ID; needs "
        "Developer Mode), 5) give them their one-time login link, then they set the RCON details in the panel. "
        "The panel works right after creating the server – they can get the login link before step 4 too.\n"
        + ("Their Discord account is linked." if linked else "Their Discord account is NOT linked yet – start there.")
        + " Staff can join the ticket for anything you can't do."
    )


class TicketTools:
    """Carries out the assistant's tool calls for one Discord user (and only their linked customer)."""

    def __init__(self, db: DB, box, settings: Settings, user_id: int, *, verify_guild=None, password_link=None,
                 changed=None, user_name: str = "", on_trial=None) -> None:
        self.user_name, self.on_trial = user_name, on_trial
        self.db, self.box, self.settings, self.user_id = db, box, settings, int(user_id)
        self.verify_guild = verify_guild          # async (guild_id, user_id) -> str | None (problem)
        self.password_link = password_link        # (server_id, made_by) -> str | None
        self.changed = changed
        self.actor = f"discord:{self.user_id}"

    @property
    def customer(self) -> int | None:
        return linked_customer(self.db, self.user_id)

    def _server(self, cid: int, server_id) -> dict:
        try:
            sid = int(server_id)
        except (TypeError, ValueError):
            raise ValueError("Which server? Use its number from account_status.") from None
        row = self.db.one("SELECT * FROM servers WHERE id = ? AND customer_id = ? AND status != 'deleted'", (sid, cid))
        if row is None:
            raise ValueError(f"Server #{sid} isn't one of yours.")
        return dict(row)

    async def run(self, name: str, args: dict) -> dict:
        try:
            if name == "link_account":
                return self.link_account(str(args.get("code") or ""))
            if name == "start_free_trial":
                out = claim_trial(self.db, self.box, self.settings, self.user_id, self.user_name)
                if out.get("ok"):
                    if self.changed:
                        await self.changed()
                    if self.on_trial:                  # the bot posts the login itself – the AI never sees it
                        await self.on_trial(out)
                    out = {"ok": True, "username": out["username"], "trial_until": out["until"],
                           "note": "The bot posted their shop login (with the password) in the ticket – don't repeat "
                                   "or ask for it. Next: create their server."}
                return out
            cid = self.customer
            if cid is None:
                return {"ok": False, "error": "Not linked yet – ask for the link code from their shop dashboard."}
            if name == "account_status":
                return self.status(cid)
            if name == "create_server":
                out = provisioning.provision_server(self.db, self.box, self.settings, cid, str(args.get("name") or ""),
                                                    actor=self.actor)
                if self.changed:
                    await self.changed()
                return {"ok": True, "server_id": out["id"], "next": "Add the bot to their Discord (bot_invite_link), "
                        "then connect_discord with this server_id."}
            if name == "bot_invite_link":
                if not self.settings.shared_client_id:
                    return {"ok": False, "error": "The shared bot isn't set up – staff will help."}
                return {"ok": True, "invite": provisioning.shared_invite(self.settings)}
            if name == "connect_discord":
                s = self._server(cid, args.get("server_id"))
                gid = "".join(ch for ch in str(args.get("discord_server_id") or "") if ch.isdigit())
                if not gid:
                    return {"ok": False, "error": "The Discord server id is a long number."}
                problem = await self.verify_guild(gid, self.user_id) if self.verify_guild else \
                    "Connecting from here isn't available – use 'Add the bot to Discord' on the dashboard."
                if problem:
                    return {"ok": False, "error": problem}
                provisioning.move_to_shared_bot(self.db, self.settings, s["id"], gid, actor=self.actor)
                if self.changed:
                    await self.changed()
                return {"ok": True, "connected": gid, "next": "It starts in a moment; then give the login link."}
            if name == "login_link":
                s = self._server(cid, args.get("server_id"))
                if s["status"] not in ("active", "awaiting_token"):
                    return {"ok": False, "error": f"This server is {s['status']} – staff will help."}
                link = self.password_link(s["id"], self.actor) if self.password_link else None
                if not link:
                    return {"ok": False, "error": "The server is still starting – try again in a minute."}
                self.db.audit(self.actor, "server.password_link", customer_id=cid, server_id=s["id"],
                              detail="HQ ticket")
                return {"ok": True, "link": link, "note": "Works once, for 48 hours. Username: owner."}
        except (provisioning.ProvisionError, slots.SlotError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"Unknown tool {name}."}

    def link_account(self, code: str) -> dict:
        if link_tries.hit(f"link:{self.user_id}") is not None:
            return {"ok": False, "error": "Too many tries – wait an hour or ask staff."}
        cid = customer_for_code(self.db, self.settings, code)
        if cid is None:
            return {"ok": False, "error": "That code doesn't match – copy it from the shop dashboard."}
        self.db.run("INSERT OR REPLACE INTO hq_links (discord_id, customer_id, linked_at) VALUES (?, ?, ?)",
                    (self.user_id, cid, int(time.time())))
        self.db.audit(self.actor, "hq.link", customer_id=cid)
        return {"ok": True, "linked": True, **self.status(cid)}

    def status(self, cid: int) -> dict:
        sub = self.db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (cid,))
        plan = plans.current(self.db, cid)
        sl = slots.summary(self.db, cid)
        servers = [{"server_id": s["id"], "name": s["name"], "state": s["status"], "bot": s["bot_mode"],
                    "discord_connected": bool(s["guild_id"]) or s["bot_mode"] == "custom",
                    "bot_online": bool(s["discord_ready"]), "rcon_connected": bool(s["rcon_connected"])}
                   for s in self.db.q("SELECT s.*, h.discord_ready, h.rcon_connected FROM servers s LEFT JOIN health h "
                                      "ON h.server_id = s.id WHERE s.customer_id = ? AND s.status != 'deleted' "
                                      "ORDER BY s.id", (cid,))]
        return {"package": plan["name"] if plan else None,
                "subscription": (sub["status"] if sub else "none"),
                "running": billing.subscription_ok(self.db, cid),
                "trial_until": time.strftime("%Y-%m-%d", time.gmtime(sub["current_period_end"]))
                if sub and sub["status"] == "trial" and sub["current_period_end"] else None,
                "slots_used": sl["used"], "slots_limit": sl["limit"], "servers": servers,
                "shop": f"{self.settings.portal_url}/dashboard"}


async def answer(client, tools: TicketTools, history: list[dict], text: str, *, max_rounds: int = 5) -> str:
    """One customer message → the assistant's reply (running its tool calls)."""
    msgs = [{"role": "system", "content": system_prompt(tools.settings, tools.customer is not None)},
            *history[-16:], {"role": "user", "content": text[:1500]}]
    for _ in range(max_rounds):
        out = await client.chat(msgs, tools=TOOLS, json_mode=False, max_tokens=700)
        calls = out.get("tool_calls") or []
        if not calls:
            return (out.get("content") or "").strip() or "…"
        msgs.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": calls})
        for call in calls[:4]:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = await tools.run(str(fn.get("name") or ""), args if isinstance(args, dict) else {})
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result)[:3000]})
    return "I couldn't finish that – a staff member will help you here."
