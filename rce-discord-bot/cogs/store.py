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

from discord.ext import commands, tasks

from support.tip4serv import Tip4ServClient, Tip4ServError, parse_command
from utils import safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.store")

POLL_SECONDS = 60


class Store(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.received = 0

    async def cog_load(self) -> None:
        self.poll_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    def status(self) -> dict:
        """For the web interface."""
        return {"enabled": bool(self.bot.config.tip4serv_key), "last_ok": self.last_ok, "error": self.last_error,
                "received": self.received}

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
                    continue                      # not for the bot (e.g. a command for the game server)
                cmd_id = str(cmd.get("id"))
                stored = self.bot.db.add_store_payment(
                    f"{pid}:{cmd_id}", pid, amount=parsed["amount"], currency=parsed["currency"],
                    discord_id=parsed["discord_id"], player=parsed["player"],
                    username=_text(p.get("username")), customer=_text(p.get("player")) or _text(p.get("steam_id")),
                    command=str(cmd.get("str"))[:300])
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


def _text(value) -> str | None:
    return str(value).strip()[:100] if value not in (None, "") else None


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Store(bot))
