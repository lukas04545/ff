"""Automatic updates: checks GitHub regularly and restarts the bot with the new version (see updater.py)."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from updater import Result, Updater

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.updates")

FIRST_CHECK_DELAY = 60   # seconds after start (the bot settles first)


class Updates(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        cfg = bot.config
        self.updater = Updater(cfg.auto_update_repo, cfg.auto_update_branch)
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if self.bot.config.auto_update:
            self.update_loop.change_interval(minutes=self.bot.config.auto_update_interval)
            self.update_loop.start()

    async def cog_unload(self) -> None:
        self.update_loop.cancel()

    @tasks.loop(minutes=5)
    async def update_loop(self) -> None:
        await self.check_now()

    @update_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(FIRST_CHECK_DELAY)

    async def check_now(self) -> Result:
        """Checks once (also from the web interface) and restarts after a successful update."""
        async with self._lock:
            result = await asyncio.to_thread(self.updater.check)
        if result.status == "error":
            log.warning("Auto-update: %s", result.message)
        elif result.status == "skipped":
            log.warning("Auto-update skipped: %s", result.message)
        elif result.restart_needed:
            text = (f"🔄 Update installed: `{(result.sha or '')[:7]}` {result.title} – restarting the bot "
                    f"({len(result.changed)} file(s) changed).")
            log.warning(text.replace("`", ""))
            self.bot.audit(text)
            self.bot.request_update_restart()
        return result

    def info(self) -> dict:
        last = self.updater.last_result
        return {"enabled": self.bot.config.auto_update, "interval": self.bot.config.auto_update_interval,
                "repo": self.updater.repo, "branch": self.updater.branch, "mode": self.updater.mode,
                "current": self.updater.current_version(), "last_check": self.updater.last_check,
                "last_status": last.status if last else None, "last_message": last.message if last else None,
                "launcher": self.bot.launched_by_script}


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Updates(bot))
