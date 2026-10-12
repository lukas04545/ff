"""Tip4Serv store: receives custom kit purchases and hands them to the kit requests (auto-approve).

Every minute (when TIP4SERV_KEY is set) the bot asks Tip4Serv for new "rcebot ..." commands – see
support/tip4serv.py for the setup. Each command is stored once (store_payments), reported back to Tip4Serv as
executed, and the open kit purchase requests are checked again.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from support.tip4serv import Tip4ServClient, Tip4ServError, parse_command, parse_webhook, strip_private
from utils import is_admin, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.store")

POLL_SECONDS = 60


class Store(commands.Cog):
    tip4serv = app_commands.Group(name="tip4serv", description="Tip4Serv purchases (staff)", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.received = 0
        self.ignored: str | None = None       # a payment Tip4Serv sent without an "rcebot" command (setup hint)

    async def cog_load(self) -> None:
        self.poll_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    def status(self) -> dict:
        """For the web interface."""
        return {"enabled": bool(self.bot.config.tip4serv_key), "last_ok": self.last_ok, "error": self.last_error,
                "received": self.received, "ignored": self.ignored,
                "stored_total": self.bot.db.count_store_payments(),
                "webhook_last": self.bot.db.last_webhook_payment()}

    def problem(self) -> str | None:
        """Why purchases may not arrive – for the request message and the AI (None = looks fine)."""
        st = self.status()
        if st["webhook_last"]:
            return None                       # live payments arrive via the webhook
        if not st["enabled"]:
            return ("the Tip4Serv store is not connected – no webhook payment received yet (web → Shop shows the "
                    "webhook URL for Tip4Serv) and no TIP4SERV_KEY")
        if st["error"]:
            return f"the Tip4Serv connection fails: {st['error'][:200]}"
        if not st["last_ok"]:
            return "the bot hasn't reached Tip4Serv yet (checks every minute)"
        if not st["stored_total"]:
            return ("the bot hasn't received any Tip4Serv purchase yet – does the product have the command "
                    "'rcebot {discord_id} {total_paid} {currency} {PlayerName}' on the bot's server?"
                    + (f" (Tip4Serv sent other commands: {st['ignored']})" if st["ignored"] else ""))
        return None

    # ------------------------------------------------------------ Staff commands

    async def on_webhook(self, data) -> dict:
        """A "live payment" from Tip4Serv's webhook (called by the web interface)."""
        parsed = parse_webhook(data)
        if parsed is None:
            log.info("Tip4Serv webhook without payment ID ignored: %s", json.dumps(data)[:300])
            return {"ok": True, "stored": False, "reason": "no payment id"}
        pid = parsed["payment_id"]
        if parsed["kind"] == "refund":
            used = self.bot.db.void_store_payment(pid)
            self.bot.audit(f"💳 Tip4Serv payment {safe_md(pid)} refunded/refused – it can't confirm a kit any more"
                           + ("" if not used else " ⚠️ it already confirmed request "
                              + ", ".join(f"#{r['used_by']}" for r in used) + " – check that kit!"))
            return {"ok": True, "voided": True}
        if parsed["kind"] != "payment":
            return {"ok": True, "stored": False, "reason": "event ignored"}
        if self.bot.db.store_payment_known(pid):          # also came via the server key / sent twice
            return {"ok": True, "stored": False, "reason": "known"}
        self.bot.db.add_store_payment(
            f"{pid}:webhook", pid, amount=parsed["amount"], currency=parsed["currency"],
            discord_id=parsed["discord_id"], player=parsed["player"], username=None, customer=None,
            command="webhook", raw=json.dumps(strip_private(data), ensure_ascii=False)[:4000])
        who = f"<@{parsed['discord_id']}>" if parsed["discord_id"] else safe_md(parsed["player"] or "?")
        amount = f"{parsed['amount']:.2f} {parsed['currency'] or ''}".strip() if parsed["amount"] is not None else "?"
        self.bot.audit(f"💳 Tip4Serv payment {safe_md(pid)} (webhook): {who} paid {amount}")
        self.received += 1
        requests = self.bot.get_cog("KitRequests")
        if requests is not None:
            await requests.on_new_payments()
        return {"ok": True, "stored": True}

    def _allowed(self, user) -> bool:
        requests = self.bot.get_cog("KitRequests")
        return is_admin(self.bot, user) or (requests is not None and requests.can_decide(user))

    @tip4serv.command(name="status", description="Is the Tip4Serv connection working? Last purchases.")
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
            return
        if self.bot.config.tip4serv_key:      # check right now instead of waiting for the next minute
            await interaction.response.defer(ephemeral=True)
            await self.poll()
            reply = interaction.followup.send
        else:
            reply = interaction.response.send_message
        st, problem = self.status(), self.problem()
        key = self.bot.config.tip4serv_key
        server = key.split(".")[0][:20] if key else None
        lines = [f"**Connection:** {'❌ ' + problem if problem else '✅ working'}",
                 "**Key:** not set" if not server else
                 "**Key:** store API key (wrong type – a server key is needed)" if server.startswith("eyJ") else
                 f"**Server number in the key:** `{safe_md(server)}`",
                 f"**Last check:** <t:{int(st['last_ok'])}:R>" if st["last_ok"] else "**Last check:** never",
                 f"**Purchases received:** {st['stored_total']}",
                 f"**Last webhook payment:** <t:{st['webhook_last']}:R>" if st["webhook_last"] else
                 "**Webhook:** no payment received yet (URL: web interface → Shop)"]
        if st["ignored"]:
            lines.append(f"**Last command that wasn't for the bot:** `{safe_md(st['ignored'])}`")
        recent = self.bot.db.store_payments(limit=8)
        if recent:
            lines.append("\n**Latest:**")
            lines += [f"• `{safe_md(p['payment_id'])}` {_money(p)} – {_who(p)} – <t:{p['received_at']}:R> – "
                      + (f"used by request #{p['used_by']}" if p["used_by"] else "unused") for p in recent]
        await reply("\n".join(lines)[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tip4serv.command(name="find", description="Looks up a Tip4Serv order the bot received.")
    @app_commands.describe(order="Order / payment ID, e.g. 1110065")
    async def find_cmd(self, interaction: discord.Interaction, order: str) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
            return
        rows = self.bot.db.find_store_payments(order)
        if not rows:
            problem = self.problem()
            await interaction.response.send_message(
                f"🔎 The bot has not received order `{safe_md(order)}` from Tip4Serv."
                + (f"\n⚠️ {problem}" if problem else "\nIs the `rcebot …` command set on that product (for the bot's "
                                                       "server)? Purchases made before that never reach the bot."),
                ephemeral=True)
            return
        await interaction.response.send_message("\n".join(
            f"• `{safe_md(p['payment_id'])}` {_money(p)} – {_who(p)} – <t:{p['received_at']}:f> – "
            + (f"used by request #{p['used_by']}" if p["used_by"] else "unused") for p in rows)[:1900],
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tip4serv.command(name="assign", description="This order belongs to this member – their open kit request is "
                                                 "checked again.")
    @app_commands.describe(order="Order / payment ID", member="Who paid")
    async def assign_cmd(self, interaction: discord.Interaction, order: str, member: discord.Member) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
            return
        rows = [p for p in self.bot.db.find_store_payments(order) if not p["used_by"]]
        if not rows:
            await interaction.response.send_message(
                f"No unused payment `{safe_md(order)}` – see `/tip4serv find`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        for pid in {p["payment_id"] for p in rows}:
            self.bot.db.assign_store_payment(pid, member.id)
        self.bot.audit(f"💳 {safe_md(str(interaction.user))} assigned Tip4Serv payment {safe_md(order)} to "
                       f"{member.mention}")
        requests = self.bot.get_cog("KitRequests")
        if requests is not None:
            await requests.on_new_payments()
        pending = [r for r in self.bot.db.kit_requests(status="pending", user_id=member.id)]
        await interaction.followup.send(
            f"✅ Payment `{safe_md(order)}` now belongs to {member.mention}. "
            + ("Their open kit request was checked again – see the request message." if pending else
               "They have no open kit request – it's used as soon as they file one."), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_loop(self) -> None:
        if self.bot.config.tip4serv_key:
            await self.poll()

    @poll_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def poll(self, client: Tip4ServClient | None = None) -> int:
        """Fetches new purchases; returns how many new ones were stored."""
        try:
            client = client or Tip4ServClient(self.bot.config.tip4serv_key)
            payments = await client.pending()
        except Tip4ServError as exc:
            if self.last_error != str(exc):
                log.warning("Tip4Serv: %s", exc)
            self.last_error = str(exc)
            return 0
        new, done = 0, {}
        for p in payments:
            pid = str(p.get("id"))
            for cmd in p.get("cmds") or []:
                if not isinstance(cmd, dict):
                    continue
                parsed = parse_command(str(cmd.get("str") or ""))
                if parsed is None:
                    self.ignored = str(cmd.get("str") or "")[:120]
                    continue                      # not for the bot (e.g. a command for the game server)
                cmd_id = str(cmd.get("id"))
                done.setdefault(pid, (p.get("action"), []))
                if self.bot.db.store_payment_known(pid, source="webhook"):
                    done[pid][1].append(cmd_id)     # already received via the webhook – count it once
                    continue
                stored = self.bot.db.add_store_payment(
                    f"{pid}:{cmd_id}", pid, amount=parsed["amount"], currency=parsed["currency"],
                    discord_id=parsed["discord_id"], player=parsed["player"],
                    username=_text(p.get("username")), customer=_text(p.get("player")) or _text(p.get("steam_id")),
                    command=str(cmd.get("str"))[:300],
                    raw=json.dumps({k: v for k, v in p.items() if k != "cmds"}, ensure_ascii=False)[:4000])
                done.setdefault(pid, (p.get("action"), []))[1].append(cmd_id)
                if stored:
                    new += 1
                    who = f"<@{parsed['discord_id']}>" if parsed["discord_id"] else safe_md(
                        parsed["player"] or _text(p.get("username")) or "?")
                    amount = f"{parsed['amount']:.2f} {parsed['currency'] or ''}".strip() if parsed["amount"] else "?"
                    self.bot.audit(f"💳 Tip4Serv purchase {safe_md(pid)}: {who} paid {amount}")
                    log.info("Tip4Serv payment %s stored: %s", pid, json.dumps(parsed))
        try:
            await client.mark_executed(done)
        except Tip4ServError as exc:              # stored already; Tip4Serv re-sends, duplicates are ignored
            log.warning("Tip4Serv: could not mark commands as executed: %s", exc)
        self.last_ok, self.last_error = time.time(), None
        self.received += new
        requests = self.bot.get_cog("KitRequests")
        if new and requests is not None:
            await requests.on_new_payments()
        return new


def _money(p) -> str:
    return f"{p['amount']:.2f} {p['currency'] or ''}".strip() if p["amount"] is not None else "?"


def _who(p) -> str:
    parts = [f"<@{p['discord_id']}>" if p["discord_id"] else "", safe_md(p["player"] or p["username"] or p["customer"]
                                                                          or "")]
    return " · ".join(x for x in parts if x) or "?"


def _text(value) -> str | None:
    return str(value).strip()[:100] if value not in (None, "") else None


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Store(bot))
