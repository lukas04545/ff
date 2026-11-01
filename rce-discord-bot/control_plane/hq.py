"""The management Discord ("HQ"): connect YOUR own Discord server to the control plane.

Its own bot (CP_HQ_BOT_TOKEN – never the shared bot) builds and keeps up to date, in CP_HQ_GUILD_ID:

  🛠 RCE HQ          (private category)
    #hq-overview     numbers, update status, [Check for updates] [Install update for ALL servers]
    #hq-ai           the management AI for any server: "#12 list the emote binds"
  👥 Customers       (private category; "Customers 2", … after 50 channels)
    #12-alex         one channel per customer who bought the bot: a customer panel (account, subscription, slots,
                     add-ons + buttons) and one panel per server (health + Restart / Suspend / Discord AI on/off).
                     Typing here talks to the management AI of that customer's server ("#<id> …" if they have more).

Who can use it: CP_HQ_ADMIN_IDS (user ids), members with CP_HQ_ADMIN_ROLE_ID and the HQ server's owner – everyone
else is ignored (and the categories are hidden from @everyone). The bot leaves any other Discord server it's added
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
from typing import Awaitable, Callable

import discord

from control_plane import actions, features, manage_ai, provisioning, slots, updates
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

log = logging.getLogger("cp.hq")

HQ_CATEGORY, CUSTOMER_CATEGORY = "🛠 RCE HQ", "👥 Customers"
OVERVIEW_CHANNEL, AI_CHANNEL = "hq-overview", "hq-ai"
CHANNELS_PER_CATEGORY = 50
REFRESH_EVERY = 120
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
                 supervisor=None, client: discord.Client | None = None) -> None:
        if settings.hq_bot_token and settings.hq_bot_token == settings.shared_bot_token:
            raise HQError("CP_HQ_BOT_TOKEN must be its own bot, not the shared bot.")
        if not settings.hq_guild_id:
            raise HQError("Set CP_HQ_GUILD_ID (your management Discord server's id).")
        self.db, self.box, self.settings, self.changed, self.supervisor = db, box, settings, changed, supervisor
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = True
        self.client = client or discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.history: dict[tuple[int, int], list[dict]] = {}       # (channel, server) -> AI conversation
        self.retries: dict[str, tuple[int, int, str]] = {}         # token -> (channel, server, text)
        self._last: dict[tuple[str, int], str] = {}                # panel -> hash of what's shown (no needless edits)
        self._lock = asyncio.Lock()
        if client is None:
            self.client.event(self.on_ready)
            self.client.event(self.on_guild_join)
            self.client.event(self.on_message)
            self.client.event(self.on_interaction)

    # ------------------------------------------------------------------ access

    @property
    def guild(self):
        return self.client.get_guild(self.settings.hq_guild_id)

    def is_admin(self, user) -> bool:
        guild = getattr(user, "guild", None)
        if guild is None or guild.id != self.settings.hq_guild_id or getattr(user, "bot", False):
            return False
        if user.id in self.settings.hq_admin_ids or user.id == guild.owner_id:
            return True
        role = self.settings.hq_admin_role_id
        return bool(role) and any(r.id == role for r in getattr(user, "roles", []))

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.client.start(self.settings.hq_bot_token)

    async def on_ready(self) -> None:
        for g in list(self.client.guilds):
            if g.id != self.settings.hq_guild_id:
                log.warning("HQ bot: leaving %s (%s) – it only works in the management Discord", g.name, g.id)
                await g.leave()
        if self.guild is None:
            log.error("HQ bot isn't in the management Discord %s – invite it there.", self.settings.hq_guild_id)
            return
        await self.sync()

    async def on_guild_join(self, guild) -> None:
        if guild.id != self.settings.hq_guild_id:
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

    def _overwrites(self, guild) -> dict:
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    read_message_history=True, manage_channels=True)}
        role = guild.get_role(self.settings.hq_admin_role_id) if self.settings.hq_admin_role_id else None
        if role is not None:
            ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for uid in self.settings.hq_admin_ids:
            member = guild.get_member(uid)
            if member is not None:
                ow[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                         read_message_history=True)
        return ow

    async def _category(self, guild, name: str):
        for cat in guild.categories:
            if cat.name == name:
                return cat
        return await guild.create_category(name, overwrites=self._overwrites(guild), reason="RCE HQ")

    async def _text(self, guild, category, name: str):
        for ch in category.text_channels:
            if ch.name == name:
                return ch
        return await category.create_text_channel(name, reason="RCE HQ")

    async def _customer_category(self, guild):
        n = 1
        while True:
            cat = await self._category(guild, CUSTOMER_CATEGORY if n == 1 else f"{CUSTOMER_CATEGORY} {n}")
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
        subs = self.db.one("SELECT SUM(status IN ('active','trialing','comped')) AS ok, "
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
        tone = "bad" if c["status"] != "active" else ("ok" if sub and sub["status"] in ("active", "trialing",
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
            hq = await self._category(guild, HQ_CATEGORY)
            await self._show(await self._text(guild, hq, OVERVIEW_CHANNEL), "overview", 0, self.overview())
            await self._text(guild, hq, AI_CHANNEL)
            for c in self.db.q("SELECT id FROM customers WHERE status != 'deleted' ORDER BY id"):
                await self.refresh_customer(c["id"], guild)

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
        if not cid_.startswith("hq:"):
            return
        if not self.is_admin(interaction.user):
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return
        actor = f"discord:{interaction.user.id}"
        owner = f"discord:{getattr(interaction.user, 'name', None) or interaction.user.id}"
        parts = cid_.split(":")
        try:
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
                    r = await updates.install(self.db, self.settings, self.supervisor, owner=owner)
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
        except (slots.SlotError, provisioning.ProvisionError, features.FeatureError, ValueError, IndexError) as exc:
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send(f"❌ {exc}", ephemeral=True)
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
        if not self.is_admin(message.author):
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


def enabled(settings: Settings) -> bool:
    return bool(settings.hq_bot_token)
