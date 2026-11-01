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
        from plugin_manager import user_dir_for
        db_path = getattr(cfg, "database_path", None)
        self.updater = Updater(cfg.auto_update_repo, cfg.auto_update_branch, before_apply=self._backup,
                               plugin_dir=user_dir_for(db_path) if db_path else None)    # your plugins (kept)
        self._lock = asyncio.Lock()

    def _backup(self) -> None:
        """Runs in the update thread right before files are replaced."""
        cfg = self.bot.config
        if cfg.backup_enabled:
            import backups
            path = backups.make_backup(cfg.database_path, label="pre-update", include_env=cfg.backup_include_env)
            if cfg.backup_copy_dir is not None:
                try:
                    backups.copy_to_folder(path, cfg.backup_copy_dir)
                except OSError:
                    pass       # the local backup exists; the copy is a bonus

    @property
    def follower(self) -> bool:
        """An extra server (servers.py) shares the main server's code – only the main server updates it."""
        import saas_mode
        import servers
        return servers.is_child() or saas_mode.enabled()      # hosted: the control plane deploys the code

    async def cog_load(self) -> None:
        if self.bot.config.auto_update and not self.follower:
            self.update_loop.change_interval(minutes=self.bot.config.auto_update_interval)
            self.update_loop.start()

    async def cog_unload(self) -> None:
        self.update_loop.cancel()

    async def on_config_changed(self, old, new) -> None:
        self.updater.repo, self.updater.branch = new.auto_update_repo.strip().strip("/"), new.auto_update_branch or "main"
        if new.auto_update_interval != old.auto_update_interval:
            self.update_loop.change_interval(minutes=new.auto_update_interval)
        if new.auto_update and not self.follower and not self.update_loop.is_running():
            self.update_loop.start()
        elif not new.auto_update and self.update_loop.is_running():
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
        if self.follower:
            return Result("skipped", "Updates are installed by the main server – this server restarts with it.")
        async with self._lock:
            result = await asyncio.to_thread(self.updater.check)
        if result.status == "error":
            log.warning("Auto-update: %s", result.message)
        elif result.status == "skipped":
            log.warning("Auto-update skipped: %s", result.message)
        elif result.restart_needed:
            text = (f"🔄 Update installed: `{(result.sha or '')[:7]}` {result.title} – restarting the bot "
                    f"({len(result.changed)} file(s) changed).")
            if result.rescued:
                text += (" 🧩 Your changed plugin file(s) were kept in data/plugins/ (they replace the built-in with the "
                         "same name – delete them on the Plugins page to use the new built-in): "
                         + ", ".join(f"`{r}`" for r in result.rescued))
            log.warning(text.replace("`", ""))
            self.bot.audit(text)
            self.bot.request_update_restart()
        return result

    def info(self) -> dict:
        last = self.updater.last_result
        return {"enabled": self.bot.config.auto_update and not self.follower, "follower": self.follower, "interval": self.bot.config.auto_update_interval,
                "repo": self.updater.repo, "branch": self.updater.branch, "mode": self.updater.mode,
                "current": self.updater.current_version(), "last_check": self.updater.last_check,
                "last_status": last.status if last else None, "last_message": last.message if last else None,
                "launcher": self.bot.launched_by_script}


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Updates(bot))
