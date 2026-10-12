"""Discord bot for Rust Console Edition (G-Portal) via WebRCON.

Start:  python bot.py   (or start.bat / start.sh – they set everything up automatically)
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import logging.handlers
import os
import sys
import time
from collections import deque
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import Config, ConfigError, load_config, required_intents
from logbuffer import memory_log
from permcheck import check_permissions, explain_forbidden
from rce import log_parser
from rce.rcon_client import RconClient, RconError, RconMessage
from storage import Storage
from utils import ChannelBuffer, NotAdmin

log = logging.getLogger("rce.bot")

# Exit code for errors a restart won't fix (start.bat/start.sh then don't restart)
EXIT_FATAL = 3
# Exit code after an automatic update – start.bat/start.sh start the new version right away
EXIT_UPDATED = 4
BASE_DIR = Path(__file__).resolve().parent

COGS = ("cogs.status", "cogs.feeds", "cogs.admin", "cogs.stats", "cogs.kits", "cogs.custom_kits", "cogs.tickets",
        "cogs.community", "cogs.moderation", "cogs.giveaways", "cogs.updates", "cogs.timed_roles", "cogs.monitor",
        "cogs.economy", "cogs.wipe", "cogs.schedule", "cogs.reports", "cogs.abuse", "cogs.kit_requests", "cogs.store")

# Event class -> Discord event name (listeners are then called on_<name>)
EVENT_NAMES: dict[type, str] = {
    log_parser.ChatEvent: "rce_chat",
    log_parser.KillEvent: "rce_kill",
    log_parser.SuicideEvent: "rce_suicide",
    log_parser.RespawnEvent: "rce_respawn",
    log_parser.BanEvent: "rce_ban",
    log_parser.RoleEvent: "rce_role",
    log_parser.TeamEvent: "rce_team",
    log_parser.AdminActionEvent: "rce_admin_action",
    log_parser.ServerEvent: "rce_server_event",
    log_parser.SaveEvent: "rce_save",
}


class RceBot(commands.Bot):
    def __init__(self, config: Config, *, disabled_intents: set[str] | frozenset[str] = frozenset(),
                 intent_warning: str | None = None, intent_fix_url: str | None = None) -> None:
        intents = discord.Intents.default()
        # Only request privileged intents when a feature needs them (see config.required_intents);
        # they must be enabled in the Developer Portal under Bot → Privileged Gateway Intents.
        # If they're missing there, the bot keeps running without them (limited mode) instead of not at all.
        needed = required_intents(config)
        intents.message_content = bool(needed["message_content"]) and "message_content" not in disabled_intents
        intents.members = bool(needed["members"]) and "members" not in disabled_intents
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.intent_warning = intent_warning
        self.intent_fix_url = intent_fix_url
        self.db = Storage(config.database_path)
        self.rcon = RconClient(config.rcon_host, config.rcon_port, config.rcon_password)

        # Live state, maintained by cogs/status.py
        self.online_players: dict[str, dict] = {}
        self.server_info: dict | None = None

        self._feeds: dict[int, ChannelBuffer] = {}

        # Recent console lines for the web interface (id, timestamp, text)
        self.console_buffer: deque[dict] = deque(maxlen=1000)
        self._console_ids = itertools.count(1)
        self.web = None  # web.server.WebInterface, if enabled
        self.restart_requested = False
        self.update_restart = False   # new version installed -> restart the whole process
        self.launched_by_script = os.environ.get("RCE_LAUNCHER") == "1"   # start.bat/start.sh restart us
        self._first_start = True
        # Result of the last automatic RCON diagnosis (for the web interface)
        self.rcon_diagnosis: dict | None = None

    # ------------------------------------------------------------ Lifecycle

    async def setup_hook(self) -> None:
        self.rcon.add_message_listener(self._on_rcon_message)
        self.rcon.add_state_listener(self._on_rcon_state)

        for cog in COGS:
            await self.load_extension(cog)

        self.tree.on_error = self.on_app_command_error
        if self.config.guild_id:
            # Guild sync: commands show up immediately (global can take up to 1 h)
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # Remove old global registrations (e.g. from a start without GUILD_ID or an older version with
            # other command names) – otherwise Discord shows them next to the current commands.
            self.tree.clear_commands(guild=None)
            try:
                await self.tree.sync()
            except discord.HTTPException as exc:
                log.warning("Could not remove old global slash commands: %s", exc)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands.", len(synced))

        await self.rcon.start()
        asyncio.create_task(self._rcon_watchdog(), name="rcon-watchdog")

        if self.config.web_enabled:
            from web.server import WebInterface
            self.web = WebInterface(self)
            # Only open the browser on the very first start, not on every restart from the web interface
            await self.web.start(open_browser=None if self._first_start else False)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID %s).", self.user, self.user.id if self.user else "?")
        if not self.config.guild_id and not getattr(self, "_guild_commands_cleaned", False):
            # Commands are global: remove old server-only registrations (from a start with GUILD_ID)
            self._guild_commands_cleaned = True
            for guild in self.guilds:
                try:
                    await self.tree.sync(guild=guild)
                except discord.HTTPException as exc:
                    log.warning("Could not remove old slash commands on %s: %s", guild.name, exc)
        problems = check_permissions(self)
        if problems:
            log.warning("The bot is missing Discord permissions (details also in the web interface → Overview):")
            for problem in problems:
                log.warning("  - %s", problem)
        else:
            log.info("Discord permissions: all good.")

    async def _rcon_watchdog(self) -> None:
        """If RCON is not working, find the cause automatically and show it in the log/web interface."""
        from rce.diagnostics import diagnose_rcon

        await asyncio.sleep(20)  # give the first connection attempt some time
        while not self.is_closed():
            stale = self.rcon_diagnosis is None or time.time() - self.rcon_diagnosis["ts"] > 600
            if not self.rcon.connected and self.rcon.failed_attempts and stale:
                cfg = self.config
                result = await diagnose_rcon(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
                self.rcon_diagnosis = {"ts": time.time(), "ok": result.ok, "steps": result.steps}
                log.warning("RCON diagnosis (automatic, because there is no connection):")
                for step in result.steps:
                    log.warning("  [%s] %s", step["level"].upper(), step["text"])
                if not result.ok:
                    log.warning("  → Fix it in the web interface under ⚙️ Settings → Rust server (RCON).")
            await asyncio.sleep(30)

    def request_restart(self) -> None:
        """Shuts the bot down cleanly; main() then starts it again with the reloaded .env."""
        self.restart_requested = True
        asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(self.close()))

    # Settings that only take effect after a restart (everything else is applied live)
    RESTART_FIELDS = {"discord_token": "Bot token", "guild_id": "Discord server", "database_path": "Database file",
                      "web_enabled": "Web interface on/off", "web_host": "Web interface: reachable from",
                      "web_port": "Web interface port"}

    async def apply_config(self, new: Config) -> list[str]:
        """Applies changed settings without a restart. Returns what still needs a restart (labels)."""
        old = self.config
        needs = [label for key, label in self.RESTART_FIELDS.items() if getattr(old, key) != getattr(new, key)]
        wanted = required_intents(new)
        if wanted["message_content"] and not self.intents.message_content:
            needs.append("Message Content intent (" + ", ".join(wanted["message_content"]) + ")")
        if wanted["members"] and not self.intents.members:
            needs.append("Server Members intent (" + ", ".join(wanted["members"]) + ")")
        self.config = new
        await self.rcon.reconfigure(new.rcon_host, new.rcon_port, new.rcon_password)
        # Cogs that keep something from the settings (loops, intervals, AI client …) adjust themselves
        for cog in list(self.cogs.values()):
            handler = getattr(cog, "on_config_changed", None)
            if handler is not None:
                try:
                    await handler(old, new)
                except Exception:
                    log.exception("Applying new settings in %s failed", type(cog).__name__)
        log.info("Settings applied without restart%s.", f" (restart needed for: {', '.join(needs)})" if needs else "")
        return needs

    def request_update_restart(self) -> None:
        """A new version was installed: shut down, main() then restarts the process (new code is loaded)."""
        self.update_restart = True
        asyncio.get_running_loop().call_later(2, lambda: asyncio.ensure_future(self.close()))

    async def close(self) -> None:
        if self.web is not None:
            await self.web.stop()
        await self.rcon.close()
        for buffer in self._feeds.values():
            await buffer.flush()
        await super().close()
        self.db.close()

    # ------------------------------------------------------------ Feeds

    def feed(self, channel_id: int | None, *, code_block: bool = False) -> ChannelBuffer | None:
        """Buffered output channel; None if the channel is not configured."""
        if not channel_id:
            return None
        if channel_id not in self._feeds:
            self._feeds[channel_id] = ChannelBuffer(self, channel_id, code_block=code_block)
        return self._feeds[channel_id]

    def discord_log(self, embed: discord.Embed) -> None:
        """Entry in the Discord log channel (members, messages, moderation, giveaways)."""
        feed = self.feed(self.config.discord_log_channel_id)
        if feed:
            if embed.timestamp is None:
                embed.timestamp = discord.utils.utcnow()
            feed.add_embed(embed)

    def audit(self, text: str) -> None:
        """Entry in the admin log channel (if configured)."""
        feed = self.feed(self.config.admin_log_channel_id)
        if feed:
            feed.add_line(text)

    # ------------------------------------------------------------ RCON -> Events

    async def _on_rcon_message(self, msg: RconMessage) -> None:
        self.dispatch("rce_console", msg)
        now = time.time()
        for line in msg.text.splitlines():
            if line.strip():
                self.console_buffer.append({"id": next(self._console_ids), "ts": now, "text": line.strip()})

        # Fallback: Rust PC format, in case a server sends chat as JSON (Type "Chat").
        if msg.type == "Chat":
            try:
                data = json.loads(msg.text)
                text = str(data.get("Message", ""))
                self.dispatch("rce_chat", log_parser.ChatEvent(
                    "GLOBAL", str(data.get("Username", "?")), text, text))
                return
            except (json.JSONDecodeError, AttributeError):
                pass

        for line in msg.text.splitlines():
            event = log_parser.parse_line(line)
            if event is not None:
                self.dispatch(EVENT_NAMES[type(event)], event)

    async def _on_rcon_state(self, connected: bool) -> None:
        self.dispatch("rce_connection", connected)

    # ------------------------------------------------------------ Errors

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, NotAdmin):
            text = "⛔ You don't have permission for this (configured admin roles/users only)."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "This command only works on the Discord server, not in DMs."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "⛔ You're missing the Discord permission: " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "⚠️ The bot is missing the Discord permission: " + ", ".join(error.missing_permissions)
        elif isinstance(original, discord.Forbidden):
            text = explain_forbidden(original, self)
            log.warning("/%s: %s", interaction.command and interaction.command.qualified_name, text.replace("\n", " "))
        elif isinstance(original, RconError):
            text = f"⚠️ {original}"
        else:
            log.error("Unexpected error in /%s", interaction.command and interaction.command.name,
                      exc_info=original)
            text = "❌ Unexpected error. Details are in the bot log."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


def disable_console_quick_edit() -> None:
    """Windows: clicking/selecting text in the bot window ("QuickEdit") pauses the whole program until
    Esc/Enter – the bot, the web interface and the tunnel would stop answering. Switch it off for this window
    (copying via right-click → Mark still works)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:  # no console (e.g. started as a service) – nothing to do
        pass


def setup_logging() -> None:
    # Windows console: force UTF-8, otherwise umlauts/emojis break in log lines
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        BASE_DIR / "logs" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console, memory_log):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler, memory_log])
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def fatal(message: str) -> None:
    log.critical(message)
    sys.exit(EXIT_FATAL)


async def run_setup(error: str, fix_url: str | None = None) -> None:
    """Setup mode: only the web interface runs until a valid configuration has been saved."""
    from web.server import WebInterface, web_settings_from_env

    done = asyncio.Event()
    web = WebInterface(None, web_settings=web_settings_from_env(), setup_error=error, setup_fix_url=fix_url,
                       on_config_saved=done.set)
    if not await web.start():
        fatal("Setup not possible: the web interface couldn't start (port in use?). "
              "Change WEB_PORT in the .env or fill in the .env by hand.")
    await done.wait()
    await asyncio.sleep(1.5)  # still deliver the response to the browser
    await web.stop()


PRIVILEGED_INTENTS_HELP = (
    "A privileged intent is not enabled in the Developer Portal. Under Bot → Privileged Gateway Intents, enable "
    "\"Message Content Intent\" (chat bridge, tickets, Discord log) and \"Server Members Intent\" (welcome, "
    "auto role, join/leave log) – then click \"Start bot with saved settings\" here. Or clear the affected channels.")


def main() -> None:
    # Working directory = bot folder, so .env, database and knowledge base are found
    # even when started by double-click or autostart.
    os.chdir(BASE_DIR)
    disable_console_quick_edit()
    setup_logging()
    from web.server import discord_preflight, intent_warning, intents_fix_url, web_settings_from_env

    setup_error: str | None = None
    setup_fix_url: str | None = None
    first_start = True
    forced_off: set[str] = set()  # intents Discord rejected despite the check
    while True:
        if setup_error is None:
            try:
                config = load_config()
            except ConfigError as exc:
                setup_error = f"Configuration incomplete: {exc}"
        if setup_error is not None:
            # Instead of aborting: let the user enter settings in the browser
            if not web_settings_from_env().enabled:
                fatal(f"{setup_error} (Web interface is disabled – please fix the .env by hand.)")
            log.warning(setup_error)
            asyncio.run(run_setup(setup_error, setup_fix_url))
            setup_error = setup_fix_url = None
            first_start = False  # browser is already open
            continue

        # Check with Discord up front (token, intents). Otherwise the bot hangs for minutes in
        # "session has been invalidated" and doesn't answer any commands.
        preflight = asyncio.run(discord_preflight(config)) or {}
        if preflight.get("preflight_error"):
            setup_error, setup_fix_url = preflight["preflight_error"], preflight["fix_url"]
            continue
        disabled = set(preflight.get("missing_intents") or []) | forced_off
        warning = intent_warning(config, sorted(disabled)) if disabled else None
        fix_url = preflight.get("fix_url") or (intents_fix_url(None) if disabled else None)
        if warning:
            log.warning(warning)
            log.warning("Fix: %s", fix_url)

        bot = RceBot(config, disabled_intents=disabled, intent_warning=warning, intent_fix_url=fix_url)
        bot._first_start = first_start
        first_start = False
        try:
            bot.run(config.discord_token, log_handler=None)
        except discord.LoginFailure:
            setup_error = "Discord rejected the bot token – please enter a new token."
            continue
        except discord.PrivilegedIntentsRequired:
            if forced_off >= {"message_content", "members"}:
                setup_error = PRIVILEGED_INTENTS_HELP  # shouldn't happen: without intents there's nothing to reject
                continue
            # Discord rejects the intents even though the check found nothing -> restart without them
            log.warning("Discord rejected the privileged intents – starting in limited mode without them.")
            forced_off = {"message_content", "members"}
            continue
        except (aiohttp.ClientError, OSError) as exc:
            # e.g. PC just booted and no internet yet – simply try again later
            log.error("No connection to Discord (%s). Retrying in 30 seconds …", exc)
            time.sleep(30)
            continue
        if bot.update_restart:
            restart_after_update(bot.launched_by_script)
        if bot.restart_requested:
            log.info("Restarting with new settings …")
            continue
        break


def restart_after_update(launched_by_script: bool) -> None:
    """Starts the freshly installed version: via start.bat/start.sh, or by replacing this process."""
    if launched_by_script:
        from web.tunnel import detach_tunnel
        log.warning("Restarting with the new version …")
        detach_tunnel()      # the new process takes over the running tunnel – same link
        sys.exit(EXIT_UPDATED)
    from web.tunnel import detach_tunnel
    log.warning("Restarting with the new version (python bot.py) …")
    detach_tunnel()      # the new process takes over the running tunnel – same link
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, str(BASE_DIR / "bot.py")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
