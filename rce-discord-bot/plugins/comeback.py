"""Wipe comeback DM: before a wipe, players who haven't played for a while get a DM ("wipe tomorrow – come back").

Only linked members (/customkit link) whose player hasn't been online for "Inactive for" days. Max. one DM per
wipe, sent slowly (Discord doesn't like mass DMs), and anyone can switch them off with /wipedm off.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0x5DADE2)
DEFAULT_TEXT = ("Hey {member}! 🧹 **{server}** wipes {when}. Fresh start for everyone – come back and build with us!"
                "{bonus}")


def last_seen_ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class WipeComeback(Plugin):
    NAME = "Wipe comeback DM"
    ICON = "📬"
    DESCRIPTION = ("Before each wipe, linked players who haven't played for a few days get a friendly DM with the "
                   "wipe time – brings old players back every wipe. Opt-out with /wipedm off.")
    SETTINGS = (
        Setting("hours", "Send … hours before the wipe", "int", 24, min=1, max=168),
        Setting("inactive_days", "Only players inactive for (days)", "int", 3, min=0, max=365,
                help="0 = every linked player who isn't online."),
        Setting("max", "Max. DMs per wipe", "int", 100, min=1, max=1000),
        Setting("message", "Message", "textarea", DEFAULT_TEXT,
                help="{member}, {player}, {server}, {when} and {bonus} (early-bird line) are replaced."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None

    async def on_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def recipients(self, now: int | None = None) -> list[tuple[int, str]]:
        now = self.now() if now is None else now
        cutoff = now - self.setting("inactive_days") * 86400
        out = []
        for row in self.bot.db.linked_players():
            user_id, player = int(row["user_id"]), row["player"]
            if self.data.get(f"optout:{user_id}") or self.online_name(player):
                continue
            stats = self.bot.db.player(player)
            seen = last_seen_ts(stats["last_seen"]) if stats else None
            if seen is not None and seen > cutoff:
                continue
            out.append((user_id, player))
        return out[: self.setting("max")]

    def text_for(self, user, player: str, wipe_at: int) -> str:
        bonus = ""
        early = self.plugin("early_bird")
        if early is not None:
            bonus = (f"\n🐦 The first {early.setting('players')} linked players online after the wipe get an "
                     "early-bird reward!")
        return (self.setting("message").replace("{member}", getattr(user, "display_name", None) or "there")
                .replace("{player}", player).replace("{server}", self.server_name)
                .replace("{when}", f"<t:{wipe_at}:R> (<t:{wipe_at}:F>)").replace("{bonus}", bonus))[:1900]

    async def send_all(self, wipe_at: int, *, pause: float = 2.0) -> int:
        sent = 0
        for user_id, player in self.recipients():
            user = self.bot.get_user(user_id)
            try:
                user = user or await self.bot.fetch_user(user_id)
                embed = discord.Embed(colour=COLOUR, description=self.text_for(user, player, wipe_at))
                embed.set_footer(text="Don't want these? Use /wipedm off on the Discord server.")
                await user.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                pass       # DMs closed or not on the server any more
            await asyncio.sleep(pause)
        self.data.set("last_count", sent)
        self.audit(f"Wipe comeback DMs sent: {sent}")
        return sent

    def due(self, now: int | None = None) -> int | None:
        """The wipe time if the DMs should go out now (once per wipe)."""
        now = self.now() if now is None else now
        nxt = self.next_wipe()
        if not nxt or nxt <= now or nxt - now > self.setting("hours") * 3600:
            return None
        if self.data.get("sent_for") == nxt:
            return None
        return nxt

    @tasks.loop(minutes=5)
    async def dm_loop(self) -> None:
        nxt = self.due()
        if nxt is None or (self._task is not None and not self._task.done()):
            return
        self.data.set("sent_for", nxt)
        self._task = asyncio.create_task(self.send_all(nxt), name="comeback-dms")

    @app_commands.command(name="wipedm", description="Wipe reminder DMs when you haven't played for a while (on/off).")
    @app_commands.describe(enabled="on = remind me before wipes, off = no DMs")
    async def wipedm(self, interaction: discord.Interaction, enabled: bool) -> None:
        if enabled:
            self.data.delete(f"optout:{interaction.user.id}")
            text = "📬 You'll get a reminder before a wipe when you haven't played for a while."
        else:
            self.data.set(f"optout:{interaction.user.id}", True)
            text = "Okay – no more wipe reminder DMs."
        await interaction.response.send_message(text, ephemeral=True)

    def problems(self) -> list[str]:
        return [] if self.next_wipe() else ["No wipe planned – set one with /wipe set or in Community → Wipe."]

    def status(self) -> dict:
        return {"DMs at the last wipe": int(self.data.get("last_count", 0)),
                "Opted out": len(self.data.items("optout:"))}
