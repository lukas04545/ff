"""Discord-Bot für Rust Console Edition (G-Portal) über WebRCON.

Start:  python bot.py
"""
from __future__ import annotations

import json
import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, ConfigError, load_config
from rce import log_parser
from rce.rcon_client import RconClient, RconError, RconMessage
from storage import Storage
from utils import ChannelBuffer, NotAdmin

log = logging.getLogger("rce.bot")

COGS = ("cogs.status", "cogs.feeds", "cogs.admin", "cogs.stats", "cogs.kits")

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
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # Nachrichteninhalt wird nur für die Chat-Bridge Discord -> Spiel gebraucht
        # (privilegierter Intent, muss im Developer Portal aktiviert sein).
        intents.message_content = bool(config.chat_channel_id and config.chat_bridge_to_game)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.db = Storage(config.database_path)
        self.rcon = RconClient(config.rcon_host, config.rcon_port, config.rcon_password)

        # Live-Zustand, gepflegt von cogs/status.py
        self.online_players: dict[str, dict] = {}
        self.server_info: dict | None = None

        self._feeds: dict[int, ChannelBuffer] = {}

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

    async def on_ready(self) -> None:
        log.info("Eingeloggt als %s (ID %s).", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
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

    # ------------------------------------------------------------ RCON -> Events

    async def _on_rcon_message(self, msg: RconMessage) -> None:
        self.dispatch("rce_console", msg)

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        sys.exit(1)

    bot = RceBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        print("Discord-Login fehlgeschlagen: DISCORD_TOKEN ist ungültig.", file=sys.stderr)
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        print(
            "Der 'Message Content Intent' ist im Developer Portal nicht aktiviert "
            "(nötig für die Chat-Bridge). Aktivieren oder CHAT_BRIDGE_TO_GAME=false setzen.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
