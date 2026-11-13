"""Auto nuke: the bot drops a 💣 nuke on Discord by itself, on a timer.

A nuke is the points drop from the store (cogs/economy.py): a message with a Claim button – the first N members (or
everyone) who press it get points, for 24 hours. This plugin launches one every … hours, announces it beforehand
(Discord and in game) and can ping a role when it drops.

  /autonuke status          when the next nuke drops
  /autonuke now             launch one right now (admins)
  /autonuke skip            skip the next one (admins)
  /autonuke on | off        pause / resume the timer without changing the settings (admins)
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import admin_only


class AutoNuke(Plugin):
    NAME = "Auto nuke"
    ICON = "💣"
    DESCRIPTION = ("Drops a nuke (points for everyone who presses Claim) on Discord every few hours – with a warning "
                   "beforehand in Discord and in game.")
    SETTINGS = (
        Setting("every_hours", "A nuke every … hours", "int", 24, min=1, max=720),
        Setting("points", "Points per claim", "int", 100, min=1, max=1_000_000),
        Setting("max_claims", "Only the first … members (0 = everyone)", "int", 0, min=0, max=10_000),
        Setting("title", "Nuke title", "text", "Auto nuke"),
        Setting("channel", "Nuke channel", "channel", help="Empty = the store's nuke channel, else the events channel."),
        Setting("warn_minutes", "Announce it … minutes before (0 = no warning)", "int", 10, min=0, max=720),
        Setting("announce_ingame", "Announce in game too", "bool", True),
        Setting("ping_role", "Ping this role when it drops", "role", help="Empty = no ping. Never @everyone."),
        Setting("min_players", "Only with at least … players online (0 = always)", "int", 0, min=0, max=500,
                help="With fewer players the nuke waits and is checked again every minute."),
    )

    autonuke = app_commands.Group(name="autonuke", description="Automatic nukes", guild_only=True)

    # ------------------------------------------------------------ timer

    def paused(self) -> bool:
        return bool(self.data.get("paused", False))

    def next_at(self) -> int:
        at = self.data.get("next_at")
        if not at or self.data.get("every") != self.setting("every_hours"):      # first run or the timer changed
            at = self.now() + self.setting("every_hours") * 3600
            self._schedule(at)
        return int(at)

    def _schedule(self, at: int) -> None:
        self.data.set("next_at", int(at))
        self.data.set("every", self.setting("every_hours"))
        self.data.set("warned", False)

    def target(self):
        cfg = self.bot.config
        return self.channel(self.setting("channel"), cfg.store_nuke_channel_id, cfg.events_channel_id)

    async def tick(self, now: int | None = None) -> str:
        """One look at the clock. Returns what happened: "", "warned", "waiting", "launched" or "failed"."""
        if self.paused():
            return ""
        now = self.now() if now is None else now
        at = self.next_at()
        warn = self.setting("warn_minutes") * 60
        if warn and not self.data.get("warned") and at - warn <= now < at:
            self.data.set("warned", True)
            minutes = max(1, round((at - now) / 60))
            await self.post(self.target(), f"💣 **{self.md(self.setting('title'))}** drops in **{minutes} min** – "
                                           f"be ready to press Claim for {self.fmt_points(self.setting('points'))}!")
            if self.setting("announce_ingame"):
                await self.say(f"<color=#e74c3c>NUKE</color> in {minutes} min on our Discord – press Claim for "
                               f"{self.setting('points')} {self.bot.config.points_name}!")
            return "warned"
        if now < at:
            return ""
        if self.setting("min_players") and len(self.online_players()) < self.setting("min_players"):
            return "waiting"
        ok = await self.launch(by="timer")
        self._schedule(now + self.setting("every_hours") * 3600)
        return "launched" if ok else "failed"

    async def launch(self, by: str) -> bool:
        eco = self.bot.get_cog("Economy")
        channel = self.target()
        if eco is None or not eco.enabled or channel is None:
            self.audit("Auto nuke couldn't drop: " + ("points & shop are switched off" if channel is not None
                                                      else "no nuke channel"))
            return False
        buyer = self.bot.user.id if self.bot.user else 0
        try:
            if self.setting("ping_role"):
                await self.post(channel, "💣 Nuke incoming!", ping_role=self.setting("ping_role"))
            await eco.drop_nuke(buyer, str(self.setting("title") or "Auto nuke")[:80], self.setting("points"),
                                self.setting("max_claims"), channel)
        except Exception as exc:
            self.audit(f"Auto nuke couldn't drop: {self.md(str(exc))[:200]}")
            return False
        self.data.set("launched", int(self.data.get("launched", 0)) + 1)
        self.data.set("last_at", self.now())
        if self.setting("announce_ingame"):
            await self.say(f"<color=#e74c3c>NUKE DROPPED</color> on our Discord – press Claim for "
                           f"{self.setting('points')} {self.bot.config.points_name}!")
        self.audit(f"Nuke dropped in <#{channel.id}> ({by}) – {self.fmt_points(self.setting('points'))} per claim")
        return True

    @tasks.loop(seconds=30)
    async def nuke_loop(self) -> None:
        await self.tick()

    # ------------------------------------------------------------ commands

    @autonuke.command(name="status", description="When the next automatic nuke drops.")
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        text = ("⏸️ Auto nuke is paused." if self.paused() else
                f"💣 Next nuke <t:{self.next_at()}:R> – every {self.setting('every_hours')} h, "
                f"{self.fmt_points(self.setting('points'))} per claim.")
        await interaction.response.send_message(text, ephemeral=True)

    @autonuke.command(name="now", description="Drop a nuke right now.")
    @admin_only()
    async def now_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        ok = await self.launch(by=str(interaction.user))
        await interaction.followup.send("💣 Dropped." if ok else "It couldn't drop – see the admin log.", ephemeral=True)

    @autonuke.command(name="skip", description="Skip the next automatic nuke.")
    @admin_only()
    async def skip_cmd(self, interaction: discord.Interaction) -> None:
        self._schedule(self.next_at() + self.setting("every_hours") * 3600)
        await interaction.response.send_message(f"Skipped – the next nuke drops <t:{self.next_at()}:R>.", ephemeral=True)

    @autonuke.command(name="on", description="Resume the automatic nukes.")
    @admin_only()
    async def on_cmd(self, interaction: discord.Interaction) -> None:
        self.data.set("paused", False)
        self._schedule(self.now() + self.setting("every_hours") * 3600)
        self.audit(f"Auto nuke switched ON by {self.md(str(interaction.user))}")
        await interaction.response.send_message(f"💣 Auto nuke on – next <t:{self.next_at()}:R>.", ephemeral=True)

    @autonuke.command(name="off", description="Pause the automatic nukes.")
    @admin_only()
    async def off_cmd(self, interaction: discord.Interaction) -> None:
        self.data.set("paused", True)
        self.audit(f"Auto nuke switched OFF by {self.md(str(interaction.user))}")
        await interaction.response.send_message("⏸️ Auto nuke paused.", ephemeral=True)

    def problems(self) -> list[str]:
        out = []
        if not self.points_enabled:
            out.append("Points & shop are switched off – nukes give points.")
        if self.target() is None:
            out.append("Pick the nuke channel.")
        return out

    def status(self) -> dict:
        return {"State": "paused" if self.paused() else "running", "Nukes dropped": int(self.data.get("launched", 0))}
