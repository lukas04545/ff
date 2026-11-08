"""The management Discord ("HQ"): connect YOUR own Discord server to the control plane.

The shared bot of every server (or, with CP_HQ_BOT_TOKEN, a bot of its own) builds and keeps up to date, in CP_HQ_GUILD_ID:

  roles             HQ Admin (everything), HQ Support (management AI without risky actions, restart/reactivate
                    servers), HQ Viewer (reads everything, can't change anything) – created by the bot
  🛠 RCE HQ          (private category)
    #hq-overview     numbers, update status, [Check for updates] [Install update for ALL servers]
    #hq-ai           the management AI for any server: "#12 list the emote binds"
    #hq-alerts       new customers/servers, crashes, bots offline, RCON lost, late payments
    #hq-audit        every action from the portal, the owner panel and here (HQ Admin only)
    #hq-staff        give or take the HQ roles with a user picker (HQ Admin only)
  💬 Support         (everyone) #get-started: [🎫 Open my customer ticket] – customers set up their bot there with an
                    AI assistant, link their account and get a one-time login link (control_plane/hq_tickets.py);
                    linked customers get a role named after their package
                    #customer-chat (CP_HQ_IDEAS_CHANNEL): an AI collects customers' suggestions, answers questions
                    about the bot and keeps a pinned board with the development status of every idea (hq_ideas.py)
  👥 Customers       (private category; "Customers 2", … after 50 channels)
    #12-alex         one channel per customer who bought the bot: a customer panel (account, subscription, slots,
                     add-ons + buttons) and one panel per server (health + Restart / Suspend / Discord AI on/off).
                     Typing here talks to the management AI of that customer's server ("#<id> …" if they have more).

Who can use it: the HQ roles above; CP_HQ_ADMIN_IDS (user ids), CP_HQ_ADMIN_ROLE_ID and the HQ server's owner always
count as HQ Admin. Everyone else is ignored (and every channel is hidden from @everyone). The bot creates and repairs
the roles, categories, channels and their permissions by itself at every start. The bot leaves any other Discord server it's added
to. Every action is in the audit log as "discord:<user id>". Destructive ones (remove, purge) stay in the owner panel.
Risky management-AI actions (console commands, deleting …) only run after an extra "Allow and run again" click.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Awaitable, Callable

import discord

from control_plane import actions, features, hq_ideas, hq_tickets, manage_ai, provisioning, slots, updates
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

log = logging.getLogger("cp.hq")

HQ_CATEGORY, CUSTOMER_CATEGORY = "🛠 RCE HQ", "👥 Customers"
OVERVIEW_CHANNEL, AI_CHANNEL = "hq-overview", "hq-ai"
ALERTS_CHANNEL, AUDIT_CHANNEL, STAFF_CHANNEL = "hq-alerts", "hq-audit", "hq-staff"
CHANNELS_PER_CATEGORY = 50
REFRESH_EVERY = 60
NONE, VIEWER, SUPPORT, ADMIN = 0, 1, 2, 3
# staff roles the bot creates: key -> (name, colour, level)
ROLES = {"admin": ("HQ Admin", 0xE74C3C, ADMIN), "support": ("HQ Support", 0x3498DB, SUPPORT),
         "viewer": ("HQ Viewer", 0x95A5A6, VIEWER)}
# who may press which button: (kind, action) or kind -> level
NEEDS = {("s", "restart"): SUPPORT, ("s", "reactivate"): SUPPORT}
# channel -> (topic, {role key: "read" | "write"}) – roles not listed don't see it
CHANNELS = {
    OVERVIEW_CHANNEL: ("Numbers, version and updates.", {"viewer": "read", "support": "read", "admin": "read"}),
    AI_CHANNEL: ("The management AI for any server: '#12 list the emote binds'.",
                 {"viewer": "read", "support": "write", "admin": "write"}),
    ALERTS_CHANNEL: ("New customers and servers, crashes, bots offline, RCON lost, late payments.",
                     {"viewer": "read", "support": "read", "admin": "read"}),
    AUDIT_CHANNEL: ("Every action from the portal, the owner panel and this Discord.", {"admin": "read"}),
    STAFF_CHANNEL: ("Give or take the HQ roles.", {"admin": "read"}),
}
CUSTOMER_ACCESS = {"viewer": "read", "support": "write", "admin": "write"}
SERVER_PREFIX = re.compile(r"^\s*#(\d+)[\s:,-]+(.*)$", re.S)
COLORS = {"ok": 0x2ECC71, "warn": 0xF39C12, "bad": 0xE74C3C, "info": 0xF7931E}


class HQError(Exception):
    pass


def channel_name(cid: int, email: str, name: str = "") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or email.split("@")[0]).lower()).strip("-") or "customer"
    return f"{cid}-{base}"[:90]


def _chunks(text: str, size: int = 1900) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class HQ:
    def __init__(self, db: DB, box: SecretBox, settings: Settings, changed: Callable[[], Awaitable[None]],
                 supervisor=None, client: discord.Client | None = None, restart: Callable[[], None] | None = None) -> None:
        self.restart = restart                       # restarts the whole control plane (after an update)
        if not settings.hq_guild_id:
            raise HQError("Set CP_HQ_GUILD_ID (your management Discord server's id).")
        # its own bot (CP_HQ_BOT_TOKEN), or – empty / the same token – the ONE shared bot of every server
        self.token = settings.hq_bot_token or settings.shared_bot_token
        if not self.token:
            raise HQError("No bot for the management Discord: set the shared bot (or CP_HQ_BOT_TOKEN).")
        self.on_shared = self.token == settings.shared_bot_token
        self.db, self.box, self.settings, self.changed, self.supervisor = db, box, settings, changed, supervisor
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = intents.members = True
        # on the shared bot it's in every customer's Discord: only the HQ server's members are loaded (on_ready)
        self.client = client or discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                                               chunk_guilds_at_startup=False)
        self.history: dict[tuple[int, int], list[dict]] = {}       # (channel, server) -> AI conversation
        self.ticket_history: dict[int, list[dict]] = {}            # ticket channel -> the assistant's conversation
        self.idea_history: dict[int, list[dict]] = {}              # suggestions channel -> recent chat with the AI
        self._ai = None                                            # DeepSeek client for the ticket assistant
        self.retries: dict[str, tuple[int, int, str]] = {}         # token -> (channel, server, text)
        self._last: dict[tuple[str, int], str] = {}                # panel -> hash of what's shown (no needless edits)
        self._lock = asyncio.Lock()
        self._snap: dict | None = None                             # last state seen, for #hq-alerts
        row = db.one("SELECT value FROM settings_kv WHERE key = 'hq_roles'")
        self.role_ids: dict[str, int] = json.loads(row["value"]) if row else {}
        if client is None:
            self.client.event(self.on_ready)
            self.client.event(self.on_guild_join)
            self.client.event(self.on_message)
            self.client.event(self.on_interaction)

    # ------------------------------------------------------------------ access

    @property
    def guild(self):
        return self.client.get_guild(self.settings.hq_guild_id)

    def level(self, user) -> int:
        guild = getattr(user, "guild", None)
        if guild is None or guild.id != self.settings.hq_guild_id or getattr(user, "bot", False):
            return NONE
        if user.id in self.settings.hq_admin_ids or user.id == guild.owner_id:
            return ADMIN
        mine = {r.id for r in getattr(user, "roles", [])}
        if self.settings.hq_admin_role_id and self.settings.hq_admin_role_id in mine:
            return ADMIN
        return max([lvl for key, (_, _, lvl) in ROLES.items() if self.role_ids.get(key) in mine], default=NONE)

    def is_admin(self, user) -> bool:
        return self.level(user) >= ADMIN

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.client.start(self.token)

    async def on_ready(self) -> None:
        if not self.on_shared:                         # its own bot only belongs in the management Discord
            for g in list(self.client.guilds):
                if g.id != self.settings.hq_guild_id:
                    log.warning("HQ bot: leaving %s (%s) – it only works in the management Discord", g.name, g.id)
                    await g.leave()
        if self.guild is None:
            log.error("The bot isn't in the management Discord %s – invite it there.", self.settings.hq_guild_id)
            return
        if not self.guild.chunked:
            await self.guild.chunk()                   # the staff panel lists who has which HQ role
        await self.sync()

    async def on_guild_join(self, guild) -> None:
        if guild.id != self.settings.hq_guild_id and not self.on_shared:     # the shared bot joins customers
            await guild.leave()

    async def refresh_forever(self, every: float = REFRESH_EVERY) -> None:
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            await asyncio.sleep(every)
            try:
                await self.sync()
            except Exception:
                log.exception("HQ refresh failed")

    # ------------------------------------------------------------------ channels

    async def ensure_roles(self, guild) -> None:
        """Creates the staff roles (no Discord permissions – they only open the HQ channels) if they're missing."""
        changed = False
        for key, (name, colour, _) in ROLES.items():
            role = guild.get_role(self.role_ids.get(key, 0)) or discord.utils.get(guild.roles, name=name)
            if role is None:
                role = await guild.create_role(name=name, colour=discord.Colour(colour), mentionable=False,
                                               reason="RCE HQ: staff role")
            if self.role_ids.get(key) != role.id:
                self.role_ids[key], changed = role.id, True
        if changed:
            self.db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('hq_roles', ?)",
                        (json.dumps(self.role_ids),))

    def _overwrites(self, guild, access: dict[str, str]) -> dict:
        """@everyone can't see it; the bot can do everything; each HQ role reads or writes as given."""
        def perm(mode: str):
            return discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                               send_messages=(mode == "write"))
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    read_message_history=True, manage_channels=True)}
        for key, mode in access.items():
            role = guild.get_role(self.role_ids.get(key, 0))
            if role is not None:
                ow[role] = perm(mode)
        if "admin" in access:                          # the configured admins always get in
            role = guild.get_role(self.settings.hq_admin_role_id) if self.settings.hq_admin_role_id else None
            if role is not None:
                ow[role] = perm(access["admin"])
            for uid in self.settings.hq_admin_ids:
                member = guild.get_member(uid)
                if member is not None:
                    ow[member] = perm(access["admin"])
        return ow

    async def _category(self, guild, name: str, access: dict[str, str]):
        ow = self._overwrites(guild, access)
        for cat in guild.categories:
            if cat.name == name:
                if cat.overwrites != ow:               # repair permissions someone changed
                    await cat.edit(overwrites=ow, reason="RCE HQ: repair permissions")
                return cat
        return await guild.create_category(name, overwrites=ow, reason="RCE HQ")

    async def _text(self, guild, category, name: str):
        topic, access = CHANNELS[name]
        ow = self._overwrites(guild, access)
        for ch in category.text_channels:
            if ch.name == name:
                if ch.overwrites != ow:
                    await ch.edit(overwrites=ow, reason="RCE HQ: repair permissions")
                return ch
        return await category.create_text_channel(name, topic=topic, overwrites=ow, reason="RCE HQ")

    async def _customer_category(self, guild):
        n = 1
        while True:
            cat = await self._category(guild, CUSTOMER_CATEGORY if n == 1 else f"{CUSTOMER_CATEGORY} {n}",
                                       CUSTOMER_ACCESS)
            if len(cat.channels) < CHANNELS_PER_CATEGORY:
                return cat
            n += 1

    async def customer_channel(self, guild, cid: int):
        row = self.db.one("SELECT channel_id FROM hq_channels WHERE customer_id = ?", (cid,))
        ch = guild.get_channel(row["channel_id"]) if row else None
        if ch is not None:
            return ch
        c = self.db.one("SELECT email, name FROM customers WHERE id = ?", (cid,))
        ch = await (await self._customer_category(guild)).create_text_channel(
            channel_name(cid, c["email"], c["name"]), topic=f"Customer #{cid} · {c['email']} – panels and the "
            "management AI for their servers (type here; '#<server id> …' if they have more than one)",
            reason="RCE HQ: new customer")
        self.db.run("INSERT OR REPLACE INTO hq_channels (customer_id, channel_id) VALUES (?, ?)", (cid, ch.id))
        return ch

    def channel_customer(self, channel_id: int) -> int | None:
        row = self.db.one("SELECT customer_id FROM hq_channels WHERE channel_id = ?", (channel_id,))
        return row["customer_id"] if row else None

    # ------------------------------------------------------------------ panels (content)

    def overview(self) -> tuple[discord.Embed, discord.ui.View]:
        n = self.db.one("SELECT COUNT(*) AS n, SUM(status = 'active') AS a FROM customers WHERE status != 'deleted'")
        s = self.db.one("SELECT COUNT(*) AS n, SUM(status = 'active') AS a FROM servers WHERE status != 'deleted'")
        subs = self.db.one("SELECT SUM(status IN ('active','trialing','comped','trial')) AS ok, "
                           "SUM(status IN ('past_due','unpaid')) AS late FROM subscriptions")
        online = self.db.one("SELECT SUM(discord_ready) AS d, SUM(rcon_connected) AS r, SUM(players_online) AS p "
                             "FROM health h JOIN servers s ON s.id = h.server_id WHERE s.status = 'active'")
        st = updates.status(self.db)
        e = discord.Embed(title="🛠 RCE HQ – overview", color=COLORS["info"])
        e.add_field(name="Customers", value=f"{n['n'] or 0} ({n['a'] or 0} active)")
        e.add_field(name="Subscriptions", value=f"{subs['ok'] or 0} paying/comped · {subs['late'] or 0} late")
        e.add_field(name="Servers", value=f"{s['n'] or 0} ({s['a'] or 0} active)")
        e.add_field(name="Live", value=f"🟢 {online['d'] or 0} bots · RCON {online['r'] or 0} · "
                                       f"{online['p'] or 0} players")
        cur, latest = st.get("current") or {}, st.get("latest") or {}
        e.add_field(name="Version", inline=False,
                    value=f"installed `{(cur.get('sha') or '?')[:10]}`"
                          + (f" · newest `{latest['sha'][:10]}` {'⬆️ update available' if st.get('available') else '✅'}"
                             if latest.get("sha") else "")
                          + ("\n⚠️ restart the control plane service" if st.get("control_plane_restart_needed") else ""))
        e.set_footer(text=f"Owner panel: {self.settings.portal_url}{self.settings.owner_path or ''}")
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Check for updates", custom_id="hq:u:check", emoji="🔎"))
        v.add_item(discord.ui.Button(label="Install update for ALL servers", custom_id="hq:u:install",
                                     style=discord.ButtonStyle.danger, emoji="⬆️"))
        return e, v

    def customer_panel(self, cid: int) -> tuple[discord.Embed, discord.ui.View]:
        c = self.db.one("SELECT * FROM customers WHERE id = ?", (cid,))
        sub = self.db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (cid,))
        on = features.active(self.db, cid)
        sl = slots.summary(self.db, cid)
        tone = "bad" if c["status"] != "active" else ("ok" if sub and sub["status"] in ("active", "trialing", "trial",
                                                                                          "comped") else "warn")
        e = discord.Embed(title=f"👤 #{cid} {c['email']}", description=c["name"] or None, color=COLORS[tone])
        e.add_field(name="Account", value=c["status"] + (f" – {c['suspend_reason']}" if c["suspend_reason"] else ""))
        e.add_field(name="Subscription", value=(sub["status"] if sub else "none")
                    + (f"\ngrace until <t:{sub['grace_until']}:f>" if sub and sub["grace_until"] else ""))
        e.add_field(name="Slots", value=f"{sl['used']} used of {sl['limit']} ({c['base_slots']} in the plan)")
        e.add_field(name="Add-ons", inline=False, value="\n".join(
            f"{'✅' if f in on else '▫️'} {label}" for f, label in features.FEATURES.items()))
        v = discord.ui.View(timeout=None)
        if c["status"] == "suspended":
            v.add_item(discord.ui.Button(label="Reactivate account", custom_id=f"hq:c:reactivate:{cid}",
                                         style=discord.ButtonStyle.success))
        elif c["status"] == "active":
            v.add_item(discord.ui.Button(label="Suspend account", custom_id=f"hq:c:suspend:{cid}",
                                         style=discord.ButtonStyle.danger))
        v.add_item(discord.ui.Button(label="+1 slot", custom_id=f"hq:c:grant:{cid}", emoji="➕"))
        v.add_item(discord.ui.Button(label="Remove comp" if sub and sub["status"] == "comped" else "Comp (free)",
                                     custom_id=f"hq:c:comp:{cid}"))
        for f, label in features.FEATURES.items():
            v.add_item(discord.ui.Button(label=f"{label}: {'on → off' if f in on else 'off → on'}",
                                         custom_id=f"hq:f:{f}:{'off' if f in on else 'on'}:{cid}", row=1,
                                         style=discord.ButtonStyle.secondary))
        return e, v

    def server_panel(self, sid: int) -> tuple[discord.Embed, discord.ui.View]:
        s = self.db.one("SELECT s.*, h.process, h.restarts, h.discord_ready, h.bot_user, h.guild_name, "
                        "h.rcon_connected, h.players_online, h.setup_mode, h.error, h.checked_at FROM servers s "
                        "LEFT JOIN health h ON h.server_id = s.id WHERE s.id = ?", (sid,))
        good = s["status"] == "active" and s["discord_ready"] and s["rcon_connected"]
        tone = "ok" if good else ("bad" if s["status"] in ("suspended", "deleted", "over_limit") or
                                  s["process"] in ("crashloop", "crashed") else "warn")
        e = discord.Embed(title=f"🖥️ #{sid} {s['name']}", url=provisioning.panel_url(self.settings, s),
                          color=COLORS[tone])
        e.add_field(name="State", value=f"{s['status']} · {s['process'] or 'not started'}"
                    + (f" · ↻{s['restarts']}" if s["restarts"] else ""))
        e.add_field(name="Discord", value=(f"🟢 {s['bot_user'] or ''}\n{s['guild_name'] or ''}" if s["discord_ready"]
                                           else "🔴 offline") + (" · own bot" if s["bot_mode"] == "custom" else ""))
        e.add_field(name="Game", value=f"RCON {'✅' if s['rcon_connected'] else '❌'} · "
                                       f"{s['players_online'] or 0} online" + (" · setup mode" if s["setup_mode"] else ""))
        e.add_field(name="Management AI in their Discord", value="on" if s["manager_ai"] else "off")
        if s["error"]:
            e.add_field(name="Error", value=str(s["error"])[:300], inline=False)
        e.set_footer(text=f"{self.settings.url_for(s['slug'])} · type here to use its management AI")
        v = discord.ui.View(timeout=None)
        if s["status"] == "deleted":
            return e, v
        if s["status"] == "active":
            v.add_item(discord.ui.Button(label="Restart", custom_id=f"hq:s:restart:{sid}", emoji="🔄"))
            v.add_item(discord.ui.Button(label="Suspend", custom_id=f"hq:s:suspend:{sid}",
                                         style=discord.ButtonStyle.danger))
        elif s["status"] in ("suspended", "stopped"):
            v.add_item(discord.ui.Button(label="Reactivate", custom_id=f"hq:s:reactivate:{sid}",
                                         style=discord.ButtonStyle.success))
        v.add_item(discord.ui.Button(label=f"Discord AI: {'on → off' if s['manager_ai'] else 'off → on'}",
                                     custom_id=f"hq:m:{'off' if s['manager_ai'] else 'on'}:{sid}", emoji="🧠"))
        return e, v

    def staff_panel(self, guild) -> tuple[discord.Embed, discord.ui.View]:
        e = discord.Embed(title="👥 HQ staff", color=COLORS["info"],
                          description="**HQ Admin** – everything (buttons, add-ons, slots, updates, risky AI actions)\n"
                                      "**HQ Support** – the management AI (no risky actions), restart/reactivate "
                                      "servers, sees every customer\n"
                                      "**HQ Viewer** – reads every panel and alert, can't change anything\n\n"
                                      "Pick people below – each person has one HQ role.")
        for key, (name, _, _) in ROLES.items():
            role = guild.get_role(self.role_ids.get(key, 0))
            members = sorted(m.display_name for m in getattr(role, "members", [])) if role else []
            e.add_field(name=f"{name} ({len(members)})", value=", ".join(members)[:1000] or "–", inline=False)
        v = discord.ui.View(timeout=None)
        for row, (key, label) in enumerate((("admin", "Make HQ Admin …"), ("support", "Make HQ Support …"),
                                            ("viewer", "Make HQ Viewer …"), ("none", "Remove from HQ staff …"))):
            v.add_item(discord.ui.UserSelect(custom_id=f"hq:r:{key}", placeholder=label, min_values=1,
                                             max_values=10, row=row))
        return e, v

    async def set_staff(self, guild, user_ids: list[int], key: str, *, actor: str) -> list[str]:
        """Gives each person the HQ role `key` (and takes their other HQ roles), or takes all of them ("none")."""
        roles = {k: guild.get_role(self.role_ids.get(k, 0)) for k in ROLES}
        done = []
        for uid in user_ids:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue
            if member.bot:
                continue
            drop = [r for k, r in roles.items() if r is not None and k != key and r in member.roles]
            if drop:
                await member.remove_roles(*drop, reason=f"RCE HQ staff change by {actor}")
            if key in roles and roles[key] is not None and roles[key] not in member.roles:
                await member.add_roles(roles[key], reason=f"RCE HQ staff change by {actor}")
            self.db.audit(actor, f"hq.staff.{key}", detail=f"{member} ({member.id})")
            done.append(member.display_name)
        return done

    def alerts(self) -> list[str]:
        """What changed since the last look: new customers/servers, crashes, offline bots, RCON, late payments."""
        servers = {r["id"]: dict(r) for r in self.db.q(
            "SELECT s.id, s.name, s.status, s.customer_id, h.process, h.discord_ready, h.rcon_connected, h.error "
            "FROM servers s LEFT JOIN health h ON h.server_id = s.id WHERE s.status != 'deleted'")}
        custs = {r["id"]: dict(r) for r in self.db.q(
            "SELECT c.id, c.email, c.status, s.status AS sub FROM customers c LEFT JOIN subscriptions s "
            "ON s.customer_id = c.id WHERE c.status != 'deleted'")}
        old, self._snap = self._snap, {"servers": servers, "customers": custs}
        if old is None:                                # first look after a start: nothing to compare with
            return []
        out = []
        for cid, c in custs.items():
            before = old["customers"].get(cid)
            if before is None:
                out.append(f"🆕 New customer **#{cid}** {c['email']}")
                continue
            if c["sub"] != before["sub"] and c["sub"] in ("past_due", "unpaid", "canceled"):
                out.append(f"💳 #{cid} {c['email']}: subscription **{c['sub']}**")
            if c["status"] != before["status"]:
                out.append(f"👤 #{cid} {c['email']}: account {before['status']} → **{c['status']}**")
        for sid, s in servers.items():
            before, tag = old["servers"].get(sid), f"#{sid} {s['name']}"
            if before is None:
                out.append(f"🖥️ New server **{tag}** (customer #{s['customer_id']})")
                continue
            if s["status"] != before["status"]:
                out.append(f"🖥️ {tag}: {before['status']} → **{s['status']}**")
            if s["process"] != before["process"] and s["process"] in ("crashed", "crashloop"):
                out.append(f"💥 {tag}: process **{s['process']}**")
            if s["status"] == "active" and before["discord_ready"] and not s["discord_ready"]:
                out.append(f"🔴 {tag}: bot went offline in Discord")
            elif s["status"] == "active" and not before["discord_ready"] and s["discord_ready"]:
                out.append(f"🟢 {tag}: bot is back online")
            if s["status"] == "active" and before["rcon_connected"] and not s["rcon_connected"]:
                out.append(f"🔌 {tag}: RCON switched off by the customer" if "switched off" in (s["error"] or "")
                           else f"❌ {tag}: RCON connection lost")
            elif s["status"] == "active" and not before["rcon_connected"] and s["rcon_connected"]:
                out.append(f"✅ {tag}: RCON back")
        return out

    def audit_lines(self, limit: int = 30) -> list[str]:
        """New audit log rows since the last post (the first start only remembers where it is)."""
        row = self.db.one("SELECT value FROM settings_kv WHERE key = 'hq_audit_last'")
        top = self.db.one("SELECT MAX(id) AS m FROM audit_log")["m"] or 0
        last = int(row["value"]) if row else top
        rows = self.db.q("SELECT * FROM audit_log WHERE id > ? ORDER BY id LIMIT ?", (last, limit))
        new_last = rows[-1]["id"] if rows else last
        if new_last != (int(row["value"]) if row else None):
            self.db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('hq_audit_last', ?)", (str(new_last),))
        return [f"<t:{r['at']}:t> `{r['actor']}` **{r['action']}**"
                + (f" c#{r['customer_id']}" if r["customer_id"] else "") + (f" s#{r['server_id']}" if r["server_id"] else "")
                + (f" – {discord.utils.escape_markdown(str(r['detail']))[:200]}" if r["detail"] else "") for r in rows]

    async def _post(self, channel, lines: list[str]) -> None:
        text = ""
        for line in lines:
            if len(text) + len(line) > 1900:
                await channel.send(text)
                text = ""
            text += line + "\n"
        if text:
            await channel.send(text)

    # ------------------------------------------------------------------ panels (posting)

    async def _show(self, channel, kind: str, ref: int, content: tuple) -> None:
        embed, view = content
        view.stop()                                    # buttons are handled in on_interaction, not by the view
        digest = hashlib.sha256(json.dumps([embed.to_dict(), view.to_components()], sort_keys=True,
                                           default=str).encode()).hexdigest()
        row = self.db.one("SELECT * FROM hq_messages WHERE kind = ? AND ref_id = ?", (kind, ref))
        if row and row["channel_id"] == channel.id:
            if self._last.get((kind, ref)) == digest:
                return
            try:
                msg = await channel.fetch_message(row["message_id"])
                await msg.edit(embed=embed, view=view)
                self._last[(kind, ref)] = digest
                return
            except discord.NotFound:
                pass
        msg = await channel.send(embed=embed, view=view)
        self.db.run("INSERT OR REPLACE INTO hq_messages (kind, ref_id, channel_id, message_id) VALUES (?, ?, ?, ?)",
                    (kind, ref, channel.id, msg.id))
        self._last[(kind, ref)] = digest

    async def sync(self) -> None:
        """Creates missing channels and panels, updates the ones that changed."""
        guild = self.guild
        if guild is None:
            return
        async with self._lock:
            await self.ensure_roles(guild)
            hq = await self._category(guild, HQ_CATEGORY, {"viewer": "read", "support": "read", "admin": "read"})
            chans = {name: await self._text(guild, hq, name) for name in CHANNELS}
            await self._show(chans[OVERVIEW_CHANNEL], "overview", 0, self.overview())
            await self._show(chans[STAFF_CHANNEL], "staff", 0, self.staff_panel(guild))
            for c in self.db.q("SELECT id FROM customers WHERE status != 'deleted' ORDER BY id"):
                await self.refresh_customer(c["id"], guild)
            try:
                await self.sync_tickets(guild)
            except discord.HTTPException as exc:
                log.warning("HQ customer tickets: %s", exc)
            try:
                await self.sync_ideas(guild)
            except discord.HTTPException as exc:
                log.warning("HQ suggestions: %s", exc)
            await self._post(chans[ALERTS_CHANNEL], self.alerts())
            await self._post(chans[AUDIT_CHANNEL], self.audit_lines())

    async def refresh_customer(self, cid: int, guild=None) -> None:
        guild = guild or self.guild
        if guild is None:
            return
        ch = await self.customer_channel(guild, cid)
        await self._show(ch, "customer", cid, self.customer_panel(cid))
        for s in self.db.q("SELECT id FROM servers WHERE customer_id = ? AND status != 'deleted' ORDER BY id", (cid,)):
            await self._show(ch, "server", s["id"], self.server_panel(s["id"]))

    # ------------------------------------------------------------------ buttons

    async def on_interaction(self, interaction) -> None:
        cid_ = (getattr(interaction, "data", None) or {}).get("custom_id", "")
        if not cid_.startswith("hq:") or getattr(interaction, "guild_id", None) != self.settings.hq_guild_id:
            return                                     # never answers in a customer's Discord (shared bot)
        parts = cid_.split(":")
        if parts[1] == "t":                            # customer tickets: for everyone in the HQ Discord
            await self.ticket_button(interaction, parts)
            return
        need = NEEDS.get(tuple(parts[1:3]), ADMIN)
        have = self.level(interaction.user)
        if have < need:
            await interaction.response.send_message(
                "Not for you." if have == NONE else "Your HQ role can't do that – ask an HQ Admin.", ephemeral=True)
            return
        actor = f"discord:{interaction.user.id}"
        owner = f"discord:{getattr(interaction.user, 'name', None) or interaction.user.id}"
        try:
            if parts[1] == "r":
                guild = self.guild
                values = [int(x) for x in (interaction.data.get("values") or []) if str(x).isdigit()]
                await interaction.response.defer(ephemeral=True, thinking=True)
                done = await self.set_staff(guild, values, parts[2], actor=actor)
                async with self._lock:
                    row = self.db.one("SELECT channel_id FROM hq_messages WHERE kind = 'staff' AND ref_id = 0")
                    ch = guild.get_channel(row["channel_id"]) if row else None
                    if ch is not None:
                        await self._show(ch, "staff", 0, self.staff_panel(guild))
                label = ROLES[parts[2]][0] if parts[2] in ROLES else "no HQ role"
                await interaction.followup.send(f"✅ {', '.join(done) or 'nobody'} → {label}", ephemeral=True)
                return
            if parts[1] == "u":
                if parts[2] == "check":
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    st = await updates.check(self.db, self.settings)
                    text = "⬆️ An update is available." if st.get("available") else "✅ Up to date."
                elif parts[2] == "install" and parts[-1] != "yes":
                    v = discord.ui.View(timeout=None)
                    v.add_item(discord.ui.Button(label="Yes – install and restart every server one by one",
                                                 custom_id="hq:u:install:yes", style=discord.ButtonStyle.danger))
                    v.stop()
                    await interaction.response.send_message("Install the newest version for ALL servers?", view=v,
                                                            ephemeral=True)
                    return
                else:
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    # with a restart coming, the control plane's restart restarts every server anyway
                    r = await updates.install(self.db, self.settings, None if self.restart else self.supervisor,
                                              owner=owner)
                    if r["last_install"]["status"] == "updated" and self.restart is not None:
                        text = (f"⬆️ Installed: {r['last_install']['message']} – the control plane and every server "
                                "restart now with it (about a minute).")
                        await interaction.followup.send(text, ephemeral=True)
                        self.db.audit(actor, "control_plane.restart", detail="after an update")
                        asyncio.get_running_loop().call_later(2.0, self.restart)
                        return
                    text = f"{r['last_install']['status']}: {r['last_install']['message']} – " \
                           f"{r['last_install']['restarted']} server(s) restarted"
                async with self._lock:
                    await self._show_overview()
                await interaction.followup.send(text, ephemeral=True)
                return
            if parts[1] == "ai":
                await self._retry_ai(interaction, parts[2])
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if parts[1] == "s":
                sid = int(parts[3])
                text = await actions.server(self.db, self.settings, self.supervisor, sid, parts[2], actor=actor)
                cid = self._server_customer(sid)
            elif parts[1] == "m":
                sid = int(parts[3])
                text = actions.manager_ai(self.db, sid, parts[2] == "on", actor=actor)
                cid = self._server_customer(sid)
            elif parts[1] == "c":
                cid = int(parts[3])
                text = actions.customer(self.db, cid, parts[2], actor=actor, owner=owner, reason="via HQ Discord")
            elif parts[1] == "f":
                cid = int(parts[4])
                text = actions.feature(self.db, cid, parts[2], parts[3] == "on", owner=owner)
            else:
                raise ValueError("Unknown button.")
        except (slots.SlotError, provisioning.ProvisionError, features.FeatureError, ValueError, IndexError,
                discord.Forbidden) as exc:
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            text = ("The bot isn't allowed to do that – give it **Manage Roles** and drag its role above the HQ "
                    "roles in Server Settings → Roles." if isinstance(exc, discord.Forbidden) else str(exc))
            await send(f"❌ {text}", ephemeral=True)
            return
        await self.changed()
        async with self._lock:                         # never two refreshes creating the same channel
            await self.refresh_customer(cid)
            await self._show_overview()
        await interaction.followup.send(f"✅ #{cid}: {text}", ephemeral=True)

    def _server_customer(self, sid: int) -> int:
        return self.db.one("SELECT customer_id FROM servers WHERE id = ?", (sid,))["customer_id"]

    async def _show_overview(self) -> None:
        row = self.db.one("SELECT channel_id FROM hq_messages WHERE kind = 'overview' AND ref_id = 0")
        ch = self.guild.get_channel(row["channel_id"]) if row and self.guild else None
        if ch is not None:
            await self._show(ch, "overview", 0, self.overview())

    # ------------------------------------------------------------------ management AI

    def _pick_server(self, channel_id: int, text: str) -> tuple[int | None, str, str]:
        """(server id, message, problem). In a customer channel: their only server or '#<id> …'; in #hq-ai: '#<id> …'."""
        m = SERVER_PREFIX.match(text)
        sid, text = (int(m[1]), m[2].strip()) if m else (None, text.strip())
        cid = self.channel_customer(channel_id)
        if cid is None:
            if sid is None:
                return None, text, "Start with the server: `#12 list the emote binds` (ids are in the panels)."
            ok = self.db.one("SELECT 1 FROM servers WHERE id = ? AND status != 'deleted'", (sid,))
            return (sid, text, "") if ok else (None, text, f"There's no server #{sid}.")
        mine = [r["id"] for r in self.db.q("SELECT id FROM servers WHERE customer_id = ? AND status != 'deleted' "
                                           "ORDER BY id", (cid,))]
        if sid is not None:
            return (sid, text, "") if sid in mine else (None, text, f"#{sid} isn't one of this customer's servers.")
        if len(mine) == 1:
            return mine[0], text, ""
        if not mine:
            return None, text, "This customer has no server yet."
        return None, text, "Which server? Start with " + ", ".join(f"`#{i}`" for i in mine) + "."

    def _is_ai_channel(self, channel) -> bool:
        return getattr(channel, "name", "") == AI_CHANNEL and getattr(getattr(channel, "category", None), "name",
                                                                         "") == HQ_CATEGORY

    async def on_message(self, message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != self.settings.hq_guild_id:
            return
        if self._is_ideas_channel(message.channel):          # the customers' chat: the suggestions AI
            await self.ideas_message(message)
            return
        ticket = self.db.one("SELECT user_id FROM hq_tickets WHERE channel_id = ?", (message.channel.id,))
        if ticket is not None:                                # a customer ticket: the assistant answers its owner
            if message.author.id == ticket["user_id"]:
                await self.ticket_message(message)
            return
        if self.level(message.author) < SUPPORT:              # viewers read, they don't ask
            return
        if not (self._is_ai_channel(message.channel) or self.channel_customer(message.channel.id) is not None):
            return
        sid, text, problem = self._pick_server(message.channel.id, message.content or "")
        if not text:
            return
        if problem:
            await message.reply(problem, mention_author=False)
            return
        await self._ask(message.channel, message.author, sid, text, allow_risky=False, reply_to=message)

    async def _ask(self, channel, user, sid: int, text: str, *, allow_risky: bool, reply_to=None) -> None:
        hist = self.history.setdefault((channel.id, sid), [])
        async with channel.typing():
            out = await manage_ai.ask(self.db, self.box, sid, text, list(hist), allow_risky=allow_risky)
        answer = out.get("answer") or f"⚠️ {out.get('error', 'no answer')}"
        hist += [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
        del hist[:-24]
        self.db.audit(f"discord:{user.id}", "manager_ai.request", server_id=sid,
                      detail=f"{'[risky allowed] ' if allow_risky else ''}{text[:200]} → "
                             f"{', '.join(out.get('done') or []) or 'no changes'}")
        extra = []
        if out.get("done"):
            extra.append("✅ " + " · ".join(out["done"]))
        refused = [a for a in out.get("asked") or [] if str(a).startswith("✖️")]
        if refused:
            extra.append("Not done (risky): " + " · ".join(refused))
        body = f"**#{sid}** {answer}" + ("\n" + "\n".join(extra) if extra else "")
        view = None
        if refused and not allow_risky:
            token = secrets.token_hex(6)
            self.retries[token] = (channel.id, sid, text)
            if len(self.retries) > 200:
                self.retries.pop(next(iter(self.retries)))
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Allow risky actions and run again", custom_id=f"hq:ai:{token}",
                                            style=discord.ButtonStyle.danger, emoji="⚠️"))
            view.stop()
        parts = _chunks(body)
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if i == 0 and reply_to is not None:
                await reply_to.reply(part, mention_author=False, **({"view": view} if last and view else {}))
            else:
                await channel.send(part, **({"view": view} if last and view else {}))

    async def _retry_ai(self, interaction, token: str) -> None:
        job = self.retries.pop(token, None)
        if job is None or job[0] != interaction.channel.id:
            await interaction.response.send_message("That request is too old – ask again.", ephemeral=True)
            return
        await interaction.response.send_message(f"⚠️ Running #{job[1]} again with risky actions allowed …")
        hist = self.history.get((job[0], job[1]), [])
        if len(hist) >= 2 and hist[-2].get("content") == job[2]:
            del hist[-2:]                                          # replace the refused round
        await self._ask(interaction.channel, interaction.user, job[1], job[2], allow_risky=True)

    # ------------------------------------------------------------------ customer tickets (hq_tickets.py)

    def _public_overwrites(self, guild) -> dict:
        return {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                                read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      read_message_history=True, manage_channels=True)}

    def ticket_panel(self) -> tuple[discord.Embed, discord.ui.View]:
        e = discord.Embed(title="🎫 Get your bot running", color=COLORS["info"], description=(
            "Bought the bot, on a free trial, or want to? Open your own private ticket – an assistant sets "
            "everything up with you: link your account, create your server, connect your Discord and get your "
            f"login link. Staff can join any time.\n\nShop: {self.settings.portal_url}"))
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Open my customer ticket", custom_id="hq:t:open", emoji="🎫",
                                     style=discord.ButtonStyle.success))
        return e, v

    async def sync_tickets(self, guild) -> None:
        """#get-started with its button, the private ticket category and the package roles of linked customers."""
        ow = self._public_overwrites(guild)
        cat = next((c for c in guild.categories if c.name == hq_tickets.SUPPORT_CATEGORY), None)
        if cat is None:
            cat = await guild.create_category(hq_tickets.SUPPORT_CATEGORY, overwrites=ow, reason="RCE HQ")
        ch = next((c for c in cat.text_channels if c.name == hq_tickets.START_CHANNEL), None)
        if ch is None:
            ch = await cat.create_text_channel(hq_tickets.START_CHANNEL, overwrites=ow, reason="RCE HQ",
                                               topic="Open your own customer ticket – set up your bot there.")
        elif ch.overwrites != ow:
            await ch.edit(overwrites=ow, reason="RCE HQ: repair permissions")
        await self._show(ch, "tickets", 0, self.ticket_panel())
        await self.sync_sub_roles(guild)

    async def _ticket_category(self, guild):
        return await self._category(guild, hq_tickets.TICKET_CATEGORY, CUSTOMER_ACCESS)

    async def sync_sub_roles(self, guild) -> None:
        """Every linked customer's Discord account gets the role of their package (and loses the old one)."""
        row = self.db.one("SELECT value FROM settings_kv WHERE key = 'hq_sub_roles'")
        known: dict[str, int] = json.loads(row["value"]) if row else {}
        mine = {}
        for link in self.db.q("SELECT discord_id, customer_id FROM hq_links"):
            mine[link["discord_id"]] = hq_tickets.subscription_label(self.db, link["customer_id"])
        changed = False
        for label in dict.fromkeys(hq_tickets.all_role_labels(self.db) + [lbl for lbl in mine.values() if lbl]):
            role = guild.get_role(known.get(label, 0)) or discord.utils.get(guild.roles, name=label)
            if role is None:
                role = await guild.create_role(name=label, colour=discord.Colour(hq_tickets.SUB_ROLE_COLOUR),
                                               mentionable=False, hoist=True, reason="RCE HQ: customer package")
            if known.get(label) != role.id:
                known[label], changed = role.id, True
        if changed:
            self.db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('hq_sub_roles', ?)",
                        (json.dumps(known),))
        all_roles = {rid for rid in known.values()}
        for uid, label in mine.items():
            member = guild.get_member(uid)
            if member is None:
                continue
            want = guild.get_role(known.get(label, 0)) if label else None
            drop = [r for r in member.roles if r.id in all_roles and r != want]
            if drop:
                await member.remove_roles(*drop, reason="RCE HQ: package changed")
            if want is not None and want not in member.roles:
                await member.add_roles(want, reason="RCE HQ: customer package")

    async def ticket_button(self, interaction, parts: list[str]) -> None:
        guild, user = self.guild, interaction.user
        if parts[2] == "open":
            row = self.db.one("SELECT channel_id FROM hq_tickets WHERE user_id = ?", (user.id,))
            ch = guild.get_channel(row["channel_id"]) if row else None
            if ch is not None:
                await interaction.response.send_message(f"Your ticket is here: {ch.mention}", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self._lock:
                cat = await self._ticket_category(guild)
                ow = dict(cat.overwrites)
                ow[user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                       attach_files=True)
                name = "ticket-" + (re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or str(user.id))
                ch = await cat.create_text_channel(name[:90], overwrites=ow, reason="RCE HQ: customer ticket",
                                                   topic=f"Customer ticket of {user} ({user.id})")
                self.db.run("INSERT OR REPLACE INTO hq_tickets (channel_id, user_id, created_at) VALUES (?, ?, ?)",
                            (ch.id, user.id, int(time.time())))
            linked = hq_tickets.linked_customer(self.db, user.id)
            v = discord.ui.View(timeout=None)
            if not linked and not self.db.one("SELECT 1 FROM hq_trials WHERE discord_id = ?", (user.id,)):
                v.add_item(discord.ui.Button(label=f"Start my free week", custom_id="hq:t:trial", emoji="🎁",
                                             style=discord.ButtonStyle.success))
            v.add_item(discord.ui.Button(label="Close ticket", custom_id="hq:t:close", emoji="🔒",
                                         style=discord.ButtonStyle.secondary))
            v.stop()
            await ch.send(f"👋 Hi {user.mention}! I'm the setup assistant – tell me what you need, e.g. "
                          "*\"set up my bot\"*. " + ("Your account is linked already." if linked else
                          f"Already a customer? Send me your **link code** – it's on your shop dashboard "
                          f"({self.settings.portal_url}/dashboard). New here? Press **🎁 Start my free week** – "
                          f"{hq_tickets.TRIAL_DAYS} days free, no payment details."),
                          view=v, allowed_mentions=discord.AllowedMentions(users=[user]))
            self.db.audit(f"discord:{user.id}", "hq.ticket_open")
            await interaction.followup.send(f"✅ Your ticket: {ch.mention}", ephemeral=True)
            return
        if parts[2] == "trial":
            row = self.db.one("SELECT user_id FROM hq_tickets WHERE channel_id = ?", (interaction.channel.id,))
            if row is None or row["user_id"] != user.id:
                await interaction.response.send_message("Press it in your own ticket.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            out = hq_tickets.claim_trial(self.db, self.box, self.settings, user.id, getattr(user, "name", "") or "")
            if not out.get("ok"):
                await interaction.followup.send(f"❌ {out['error']}")
                return
            await self.changed()
            await interaction.followup.send(self._trial_text(out))
            try:
                async with self._lock:
                    await self.sync_sub_roles(self.guild)
            except discord.HTTPException as exc:
                log.warning("HQ package roles: %s", exc)
            return
        if parts[2] == "close":
            row = self.db.one("SELECT user_id FROM hq_tickets WHERE channel_id = ?", (interaction.channel.id,))
            if row is None or (row["user_id"] != user.id and self.level(user) < SUPPORT):
                await interaction.response.send_message("Not your ticket.", ephemeral=True)
                return
            await interaction.response.send_message("🔒 Closing …")
            self.db.run("DELETE FROM hq_tickets WHERE channel_id = ?", (interaction.channel.id,))
            self.ticket_history.pop(interaction.channel.id, None)
            self.db.audit(f"discord:{user.id}", "hq.ticket_close")
            await interaction.channel.delete(reason="RCE HQ: ticket closed")

    @staticmethod
    def _trial_text(out: dict) -> str:
        return (f"🎁 **Your free week is on** – until **{out['until']}**.\n"
                f"Your shop login (save it – the password is shown only now): {out['shop']}\n"
                f"Username: `{out['username']}` · Password: `{out['password']}`\n"
                "Next: tell me *\"create my server\"* and we set it up together.")

    async def verify_guild(self, guild_id: str, user_id: int) -> str | None:
        """None if the bot is in that Discord server and the user owns it or has Manage Server there."""
        if not self.on_shared:
            return ("Use 'Add the bot to Discord' on your shop dashboard – it connects your Discord server "
                    "automatically.")
        g = self.client.get_guild(int(guild_id))
        if g is None:
            return "The bot isn't in that Discord server yet – add it with the invite link first, then try again."
        if g.id == self.settings.hq_guild_id:
            return "That's this support Discord – use your own server's id."
        if g.owner_id == user_id:
            return None
        try:
            member = g.get_member(user_id) or await g.fetch_member(user_id)
        except discord.HTTPException:
            member = None
        if member is None or not member.guild_permissions.manage_guild:
            return "You need to own that Discord server or have Manage Server there."
        return None

    def _ticket_ai(self):
        if self._ai is None and self.settings.deepseek_api_key:
            import os
            from support.deepseek import DeepSeekClient
            self._ai = DeepSeekClient(self.settings.deepseek_api_key,
                                      model=os.environ.get("CP_DEEPSEEK_MODEL", "deepseek-chat"),
                                      base_url="https://api.deepseek.com")
        return self._ai

    async def ticket_message(self, message) -> None:
        from support.deepseek import DeepSeekError
        client = self._ticket_ai()
        if client is None:
            await message.reply("The assistant isn't switched on – a staff member will help you here.",
                                mention_author=False)
            return
        if hq_tickets.ai_turns.hit(f"ai:{message.author.id}") is not None:
            await message.reply("Slow down a little – try again in a few minutes.", mention_author=False)
            return
        from control_plane.web import password_link
        async def on_trial(out: dict) -> None:
            await message.channel.send(self._trial_text(out))
        tools = hq_tickets.TicketTools(self.db, self.box, self.settings, message.author.id,
                                       verify_guild=self.verify_guild, changed=self.changed, on_trial=on_trial,
                                       user_name=getattr(message.author, "name", "") or "",
                                       password_link=lambda sid, by: password_link(self.db, self.box, self.settings,
                                                                                   sid, by))
        hist = self.ticket_history.setdefault(message.channel.id, [])
        text = message.content or ""
        try:
            async with message.channel.typing():
                reply = await hq_tickets.answer(client, tools, hist, text)
        except DeepSeekError as exc:
            log.warning("HQ ticket assistant: %s", exc)
            reply = "The assistant can't answer right now – a staff member will help you here."
        hist += [{"role": "user", "content": text[:1500]}, {"role": "assistant", "content": reply[:1500]}]
        del hist[:-16]
        for i, part in enumerate(_chunks(reply)):
            if i == 0:
                await message.reply(part, mention_author=False)
            else:
                await message.channel.send(part)
        if tools.customer is not None:                       # linked / changed: their role and their panels
            try:
                async with self._lock:
                    await self.sync_sub_roles(self.guild)
            except discord.HTTPException as exc:
                log.warning("HQ package roles: %s", exc)

    # ------------------------------------------------------------------ suggestions (hq_ideas.py)

    def _is_ideas_channel(self, channel) -> bool:
        name = (self.settings.hq_ideas_channel or "").strip().lstrip("#").lower()
        return bool(name) and getattr(channel, "name", None) == name

    def _ideas_channel(self, guild):
        name = (self.settings.hq_ideas_channel or "").strip().lstrip("#").lower()
        if not name:
            return None
        chans = getattr(guild, "text_channels", None)
        if chans is None:
            chans = [ch for cat in guild.categories for ch in cat.text_channels]
        return next((ch for ch in chans if ch.name == name), None)

    async def sync_ideas(self, guild) -> None:
        """The suggestions channel (made if missing: customers with a package + HQ staff), its pinned board and the
        notices for changed statuses. An existing channel's permissions are left as the owner set them."""
        name = (self.settings.hq_ideas_channel or "").strip().lstrip("#").lower()
        if not name:
            return
        ch = self._ideas_channel(guild)
        if ch is None:
            cat = next((c for c in guild.categories if c.name == hq_tickets.SUPPORT_CATEGORY), None)
            if cat is None:
                return                                   # sync_tickets makes it first
            ow = self._overwrites(guild, CUSTOMER_ACCESS)
            row = self.db.one("SELECT value FROM settings_kv WHERE key = 'hq_sub_roles'")
            for rid in (json.loads(row["value"]) if row else {}).values():
                role = guild.get_role(rid)
                if role is not None:
                    ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)
            ch = await cat.create_text_channel(name, overwrites=ow, reason="RCE HQ: customers' chat",
                                               topic="Ideas and questions about the bot – an assistant collects every "
                                                     "suggestion; the pinned board shows how far each one is.")
        before = self.db.one("SELECT message_id FROM hq_messages WHERE kind = 'ideas' AND ref_id = 0")
        await self._show(ch, "ideas", 0, hq_ideas.board(self.db))
        after = self.db.one("SELECT message_id FROM hq_messages WHERE kind = 'ideas' AND ref_id = 0")
        if after and (before is None or before["message_id"] != after["message_id"]):
            try:                                         # a new board: pin it so it stays findable in the chat
                msg = await ch.fetch_message(after["message_id"])
                await msg.pin(reason="RCE HQ: suggestions board")
            except (discord.HTTPException, AttributeError):
                pass
        for s in hq_ideas.unannounced(self.db):
            hq_ideas.mark_announced(self.db, s["id"], s["status"])
            if s["status"] == "new":
                continue
            note = f"\n> {discord.utils.escape_mentions(s['note'])}" if s["note"] else ""
            await ch.send(f"{hq_ideas.label(s['status'])} – **#{s['id']}** "
                          f"{discord.utils.escape_mentions(s['text'][:200])} (suggested by <@{s['author_id']}>){note}",
                          allowed_mentions=discord.AllowedMentions(users=[discord.Object(s["author_id"])],
                                                                   everyone=False, roles=False))

    async def ideas_message(self, message) -> None:
        from support.deepseek import DeepSeekError
        staff = self.level(message.author) >= SUPPORT
        me = getattr(self.client, "user", None)
        if staff and not (me is not None and me in (getattr(message, "mentions", None) or [])):
            return                                       # staff talking to customers – only answers when @mentioned
        text = re.sub(r"<@!?\d+>", "", message.content or "").strip()
        client = self._ticket_ai()
        if not text or client is None:
            return
        if hq_ideas.idea_turns.hit(f"idea:{message.author.id}") is not None:
            return                                       # quietly: no reply spam in a shared channel
        name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or ""
        tools = hq_ideas.IdeaTools(self.db, message.author.id, name, staff=staff)
        hist = self.idea_history.setdefault(message.channel.id, [])
        try:
            async with message.channel.typing():
                reply = await hq_ideas.answer(client, tools, hist, text)
        except DeepSeekError as exc:
            log.warning("HQ suggestions AI: %s", exc)
            return
        hist.append({"role": "user", "content": f"{name}: {text[:800]}"})
        if reply:
            hist.append({"role": "assistant", "content": reply[:800]})
        del hist[:-12]
        for i, part in enumerate(_chunks(reply) if reply else []):
            if i == 0:
                await message.reply(part, mention_author=False)
            else:
                await message.channel.send(part)
        if tools.changed and self.guild is not None:
            try:
                async with self._lock:
                    await self.sync_ideas(self.guild)
            except discord.HTTPException as exc:
                log.warning("HQ suggestions board: %s", exc)


def enabled(settings: Settings) -> bool:
    return bool(settings.hq_guild_id and (settings.hq_bot_token or settings.shared_bot_token))
