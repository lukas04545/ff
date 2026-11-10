"""/connect – how to join the server: its name, how to find it in game, the website and who's online."""
from __future__ import annotations

import discord
from discord import app_commands

from plugin_api import Plugin, Setting


class ConnectInfo(Plugin):
    NAME = "Connect info"
    ICON = "🔌"
    DESCRIPTION = "/connect shows players how to join your server (search name, steps, website, players online)."
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("how_to_join", "How to join", "textarea", "",
                help="E.g. “Play → Community servers → search ‘Kryvon EU’”. Empty = the text from the Player website "
                     "plugin (or a general hint)."),
    )

    def text(self) -> str:
        own = str(self.setting("how_to_join") or "").strip()
        if own:
            return own
        site = self.plugin("website")
        if site is not None and str(site.setting("how_to_join") or "").strip():
            return str(site.setting("how_to_join")).strip()
        return (f"In Rust Console Edition: **Play → Community servers** and search for "
                f"**{self.server_name}**.")

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=f"🔌 Join {self.server_name}", description=self.text()[:3500], colour=0xE67E22)
        info = getattr(self.bot, "server_info", None) or {}
        if self.bot.rcon.connected and "Players" in info:
            e.add_field(name="Online now", value=f"{info.get('Players')} / {info.get('MaxPlayers', '?')}")
        url = (self.bot.config.web_public_url or "").rstrip("/")
        if url:
            e.add_field(name="Website", value=url)
        e.set_footer(text="Link your player on Discord with /link to get kits and rewards.")
        return e

    @app_commands.command(name="connect", description="How to join our Rust server.")
    async def connect_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.embed())
