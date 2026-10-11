"""Discord-Bot für Rust Console Edition (G-Portal) über WebRCON.

Start:  python bot.py   (oder start.bat / start.sh – richten alles automatisch ein)
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
from rce import log_parser
from rce.rcon_client import RconClient, RconError, RconMessage
from storage import Storage
from utils import ChannelBuffer, NotAdmin

log = logging.getLogger("rce.bot")

# Exit-Code für Fehler, bei denen ein Neustart nichts bringt (start.bat/start.sh starten dann nicht neu)
EXIT_FATAL = 3
BASE_DIR = Path(__file__).resolve().parent

COGS = ("cogs.status", "cogs.feeds", "cogs.admin", "cogs.stats", "cogs.kits", "cogs.custom_kits", "cogs.tickets",
        "cogs.community", "cogs.moderation", "cogs.giveaways")

# Ereignisklasse -> Discord-Eventname (Listener heißen dann on_<name>)
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
        # Privilegierte Intents nur anfordern, wenn eine Funktion sie braucht (siehe config.required_intents);
        # sie müssen im Developer Portal unter Bot → Privileged Gateway Intents eingeschaltet sein.
        # Fehlen sie dort, läuft der Bot ohne sie weiter (eingeschränkter Betrieb) statt gar nicht.
        needed = required_intents(config)
        intents.message_content = bool(needed["message_content"]) and "message_content" not in disabled_intents
        intents.members = bool(needed["members"]) and "members" not in disabled_intents
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.intent_warning = intent_warning
        self.intent_fix_url = intent_fix_url
        self.db = Storage(config.database_path)
        self.rcon = RconClient(config.rcon_host, config.rcon_port, config.rcon_password)

        # Live-Zustand, gepflegt von cogs/status.py
        self.online_players: dict[str, dict] = {}
        self.server_info: dict | None = None

        self._feeds: dict[int, ChannelBuffer] = {}

        # Letzte Konsolenzeilen für das Webinterface (id, Zeitstempel, Text)
        self.console_buffer: deque[dict] = deque(maxlen=1000)
        self._console_ids = itertools.count(1)
        self.web = None  # web.server.WebInterface, falls aktiviert
        self.restart_requested = False
        self._first_start = True
        # Ergebnis der letzten automatischen RCON-Diagnose (fürs Webinterface)
        self.rcon_diagnosis: dict | None = None

    # ------------------------------------------------------------ Lebenszyklus

    async def setup_hook(self) -> None:
        self.rcon.add_message_listener(self._on_rcon_message)
        self.rcon.add_state_listener(self._on_rcon_state)

        for cog in COGS:
            await self.load_extension(cog)

        self.tree.on_error = self.on_app_command_error
        if self.config.guild_id:
            # Guild-Sync: Befehle sind sofort sichtbar (global kann bis zu 1 h dauern)
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("%d Slash-Commands synchronisiert.", len(synced))

        await self.rcon.start()
        asyncio.create_task(self._rcon_watchdog(), name="rcon-watchdog")

        if self.config.web_enabled:
            from web.server import WebInterface
            self.web = WebInterface(self)
            # Browser nur beim allerersten Start öffnen, nicht bei jedem Neustart aus dem Webinterface
            await self.web.start(open_browser=None if self._first_start else False)

    async def on_ready(self) -> None:
        log.info("Eingeloggt als %s (ID %s).", self.user, self.user.id if self.user else "?")

    async def _rcon_watchdog(self) -> None:
        """Klappt RCON nicht, die Ursache automatisch ermitteln und im Log/Webinterface anzeigen."""
        from rce.diagnostics import diagnose_rcon

        await asyncio.sleep(20)  # dem ersten Verbindungsversuch Zeit lassen
        while not self.is_closed():
            stale = self.rcon_diagnosis is None or time.time() - self.rcon_diagnosis["ts"] > 600
            if not self.rcon.connected and self.rcon.failed_attempts and stale:
                cfg = self.config
                result = await diagnose_rcon(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
                self.rcon_diagnosis = {"ts": time.time(), "ok": result.ok, "steps": result.steps}
                log.warning("RCON-Diagnose (automatisch, weil keine Verbindung besteht):")
                for step in result.steps:
                    log.warning("  [%s] %s", step["level"].upper(), step["text"])
                if not result.ok:
                    log.warning("  → Korrigieren im Webinterface unter ⚙️ Einstellungen → Rust-Server (RCON).")
            await asyncio.sleep(30)

    def request_restart(self) -> None:
        """Beendet den Bot sauber; main() startet ihn danach mit neu geladener .env wieder."""
        self.restart_requested = True
        asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(self.close()))

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
        """Gepufferter Ausgabekanal; None wenn der Channel nicht konfiguriert ist."""
        if not channel_id:
            return None
        if channel_id not in self._feeds:
            self._feeds[channel_id] = ChannelBuffer(self, channel_id, code_block=code_block)
        return self._feeds[channel_id]

    def discord_log(self, embed: discord.Embed) -> None:
        """Eintrag im Discord-Log-Channel (Mitglieder, Nachrichten, Moderation, Giveaways)."""
        feed = self.feed(self.config.discord_log_channel_id)
        if feed:
            if embed.timestamp is None:
                embed.timestamp = discord.utils.utcnow()
            feed.add_embed(embed)

    def audit(self, text: str) -> None:
        """Eintrag im Admin-Log-Channel (falls konfiguriert)."""
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

        # Fallback: Rust-PC-Format, falls ein Server Chat als JSON (Type "Chat") sendet.
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

    # ------------------------------------------------------------ Fehler

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, NotAdmin):
            text = "⛔ Dafür hast du keine Berechtigung (nur für konfigurierte Admin-Rollen/User)."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "Dieser Befehl funktioniert nur auf dem Discord-Server, nicht per DM."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "⛔ Dir fehlt die Discord-Berechtigung: " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "⚠️ Dem Bot fehlt die Discord-Berechtigung: " + ", ".join(error.missing_permissions)
        elif isinstance(original, discord.Forbidden):
            text = "⚠️ Discord hat die Aktion verweigert – fehlen dem Bot Rechte oder steht seine Rolle zu tief?"
        elif isinstance(original, RconError):
            text = f"⚠️ {original}"
        else:
            log.error("Unerwarteter Fehler in /%s", interaction.command and interaction.command.name,
                      exc_info=original)
            text = "❌ Unerwarteter Fehler. Details stehen im Bot-Log."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


def setup_logging() -> None:
    # Windows-Konsole: UTF-8 erzwingen, sonst brechen Umlaute/Emojis in Logzeilen
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
    """Einrichtungsmodus: nur das Webinterface läuft, bis eine gültige Konfiguration gespeichert ist."""
    from web.server import WebInterface, web_settings_from_env

    done = asyncio.Event()
    web = WebInterface(None, web_settings=web_settings_from_env(), setup_error=error, setup_fix_url=fix_url,
                       on_config_saved=done.set)
    if not await web.start():
        fatal("Einrichtung nicht möglich: Das Webinterface konnte nicht starten (Port belegt?). "
              "WEB_PORT in der .env ändern oder die .env von Hand ausfüllen.")
    await done.wait()
    await asyncio.sleep(1.5)  # Antwort noch an den Browser ausliefern
    await web.stop()


PRIVILEGED_INTENTS_HELP = (
    "Ein privilegierter Intent ist im Developer Portal nicht aktiviert. Unter Bot → Privileged Gateway Intents "
    "„Message Content Intent“ (Chat-Bridge, Tickets, Discord-Log) und „Server Members Intent“ (Willkommen, "
    "Auto-Rolle, Join/Leave-Log) einschalten – dann hier „Bot starten“ klicken. Oder die betroffenen Channels leeren.")


def main() -> None:
    # Arbeitsverzeichnis = Bot-Ordner, damit .env, Datenbank und Wissensbasis auch bei
    # Doppelklick oder Autostart gefunden werden.
    os.chdir(BASE_DIR)
    setup_logging()
    from web.server import discord_preflight, intent_warning, intents_fix_url, web_settings_from_env

    setup_error: str | None = None
    setup_fix_url: str | None = None
    first_start = True
    forced_off: set[str] = set()  # Intents, die Discord trotz Prüfung abgelehnt hat
    while True:
        if setup_error is None:
            try:
                config = load_config()
            except ConfigError as exc:
                setup_error = f"Konfiguration unvollständig: {exc}"
        if setup_error is not None:
            # Statt abzubrechen: Einstellungen im Browser eintragen lassen
            if not web_settings_from_env().enabled:
                fatal(f"{setup_error} (Webinterface ist deaktiviert – bitte die .env von Hand korrigieren.)")
            log.warning(setup_error)
            asyncio.run(run_setup(setup_error, setup_fix_url))
            setup_error = setup_fix_url = None
            first_start = False  # Browser ist schon offen
            continue

        # Vorab bei Discord prüfen (Token, Intents). Sonst hängt der Bot minutenlang in
        # "session has been invalidated" und beantwortet keinen Befehl.
        preflight = asyncio.run(discord_preflight(config)) or {}
        if preflight.get("preflight_error"):
            setup_error, setup_fix_url = preflight["preflight_error"], preflight["fix_url"]
            continue
        disabled = set(preflight.get("missing_intents") or []) | forced_off
        warning = intent_warning(config, sorted(disabled)) if disabled else None
        fix_url = preflight.get("fix_url") or (intents_fix_url(None) if disabled else None)
        if warning:
            log.warning(warning)
            log.warning("Beheben: %s", fix_url)

        bot = RceBot(config, disabled_intents=disabled, intent_warning=warning, intent_fix_url=fix_url)
        bot._first_start = first_start
        first_start = False
        try:
            bot.run(config.discord_token, log_handler=None)
        except discord.LoginFailure:
            setup_error = "Discord hat den Bot-Token abgelehnt – bitte einen neuen Token eintragen."
            continue
        except discord.PrivilegedIntentsRequired:
            if forced_off >= {"message_content", "members"}:
                setup_error = PRIVILEGED_INTENTS_HELP  # sollte nicht vorkommen: ohne Intents gibt es nichts abzulehnen
                continue
            # Discord lehnt die Intents ab, obwohl die Prüfung nichts fand -> ohne sie neu starten
            log.warning("Discord lehnt die privilegierten Intents ab – starte im eingeschränkten Betrieb ohne sie.")
            forced_off = {"message_content", "members"}
            continue
        except (aiohttp.ClientError, OSError) as exc:
            # z. B. PC gerade gestartet und noch ohne Internet – einfach später erneut versuchen
            log.error("Keine Verbindung zu Discord (%s). Neuer Versuch in 30 Sekunden …", exc)
            time.sleep(30)
            continue
        if bot.restart_requested:
            log.info("Neustart mit neuen Einstellungen …")
            continue
        break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
