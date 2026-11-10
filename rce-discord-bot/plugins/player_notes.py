"""Warnings & notes for in-game players – staff only.

  /warn add <player> <reason>   a warning (the player is told in game / by DM if you like; kick or ban after N)
  /warn list <player>           their warnings · /warn remove <id> · /warn clear <player>
  /note add <player> <text>     a private staff note (never shown to players) · /note list · /note remove <id>

Warnings count per in-game name (case-insensitive). A linked player sees their warning count in /profile.
"""
from __future__ import annotations

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from utils import admin_only, sanitize_player_name


class PlayerNotes(Plugin):
    NAME = "Warnings & notes"
    ICON = "📝"
    DESCRIPTION = ("Staff warn in-game players (with an automatic kick or ban after a number of warnings) and keep "
                   "private notes on players – /warn and /note.")
    SETTINGS = (
        Setting("tell_ingame", "Tell the player in game", "bool", True,
                help="Shows “<player> was warned: <reason>” in game (only if they're online)."),
        Setting("dm", "DM the player (if linked)", "bool", True),
        Setting("kick_after", "Kick after … warnings", "int", 0, min=0, max=50, help="0 = never."),
        Setting("ban_after", "Ban after … warnings", "int", 0, min=0, max=50, help="0 = never."),
    )

    warn = app_commands.Group(name="warn", description="Warn in-game players (staff)", guild_only=True)
    note = app_commands.Group(name="note", description="Private staff notes on players", guild_only=True)

    # ------------------------------------------------------------ data

    def _next(self) -> int:
        n = int(self.data.get("next", 0)) + 1
        self.data.set("next", n)
        return n

    def entries(self, kind: str, player: str | None = None) -> list[dict]:
        rows = [dict(v, id=int(k.split(":")[1])) for k, v in self.data.items(f"{kind}:").items()]
        if player:
            rows = [r for r in rows if r["player"].lower() == player.lower()]
        return sorted(rows, key=lambda r: r["id"])

    def add(self, kind: str, player: str, text: str, by: str) -> dict:
        row = {"player": sanitize_player_name(player)[:64], "text": str(text)[:500], "by": by[:64], "at": self.now()}
        n = self._next()
        self.data.set(f"{kind}:{n}", row)
        return dict(row, id=n)

    def remove(self, kind: str, entry_id: int) -> bool:
        return self.data.delete(f"{kind}:{int(entry_id)}")

    def clear(self, kind: str, player: str) -> int:
        n = 0
        for row in self.entries(kind, player):
            n += self.remove(kind, row["id"])
        return n

    async def warn_player(self, player: str, reason: str, by: str) -> tuple[dict, int, str]:
        """Adds a warning and applies the automatic kick/ban. Returns (warning, count, what happened)."""
        row = self.add("w", player, reason, by)
        count = len(self.entries("w", row["player"]))
        name = row["player"]
        online = self.online_name(name)
        if self.setting("tell_ingame") and online:
            await self.say(f"<color=#e67e22>{online} was warned</color> ({count}): {reason[:120]}")
        if self.setting("dm") and (uid := self.linked_user(name)):
            user = self.bot.get_user(uid)
            try:
                user = user or await self.bot.fetch_user(uid)
                await user.send(f"⚠️ You got a warning on **{self.md(self.server_name)}** ({count} so far): "
                                f"{self.md(reason[:300])}")
            except (discord.HTTPException, AttributeError):
                pass
        action = ""
        cfg = self.bot.config
        if self.setting("ban_after") and count >= self.setting("ban_after"):
            await self.bot.rcon.command(cfg.ban_template.format(name=name), expect_response=False)
            action = f"banned ({count} warnings)"
        elif self.setting("kick_after") and count >= self.setting("kick_after") and online:
            await self.bot.rcon.command(cfg.kick_template.format(name=online), expect_response=False)
            action = f"kicked ({count} warnings)"
        self.audit(f"⚠️ **{self.md(name)}** warned by {self.md(by)} ({count}): {self.md(reason[:200])}"
                   + (f" – **{action}**" if action else ""))
        return row, count, action

    @staticmethod
    def _lines(rows: list[dict]) -> str:
        return "\n".join(f"`#{r['id']}` <t:{r['at']}:d> **{r['text'][:150]}** – {r['by']}" for r in rows[-15:])

    # ------------------------------------------------------------ commands

    @warn.command(name="add", description="Warn an in-game player.")
    @app_commands.describe(player="In-game name", reason="Why (the player sees it)")
    @admin_only()
    async def warn_add(self, interaction: discord.Interaction, player: str, reason: str) -> None:
        await interaction.response.defer(ephemeral=True)
        row, count, action = await self.warn_player(player, reason, str(interaction.user))
        await interaction.followup.send(f"⚠️ **{self.md(row['player'])}** warned (#{row['id']}) – {count} warning"
                                        f"{'s' if count != 1 else ''} now." + (f" They were **{action}**." if action
                                                                               else ""), ephemeral=True)

    @warn.command(name="list", description="A player's warnings.")
    @admin_only()
    async def warn_list(self, interaction: discord.Interaction, player: str) -> None:
        rows = self.entries("w", player)
        await interaction.response.send_message(
            f"**{self.md(player)}** – {len(rows)} warning(s)\n{self._lines(rows)}" if rows else
            f"**{self.md(player)}** has no warnings.", ephemeral=True)

    @warn.command(name="remove", description="Remove a warning by its number.")
    @admin_only()
    async def warn_remove(self, interaction: discord.Interaction, warning: int) -> None:
        ok = self.remove("w", warning)
        if ok:
            self.audit(f"⚠️ Warning #{warning} removed by {self.md(str(interaction.user))}")
        await interaction.response.send_message("Removed." if ok else "No such warning.", ephemeral=True)

    @warn.command(name="clear", description="Remove all warnings of a player.")
    @admin_only()
    async def warn_clear(self, interaction: discord.Interaction, player: str) -> None:
        n = self.clear("w", player)
        self.audit(f"⚠️ {n} warning(s) of **{self.md(player)}** cleared by {self.md(str(interaction.user))}")
        await interaction.response.send_message(f"{n} warning(s) removed.", ephemeral=True)

    @note.command(name="add", description="A private staff note on a player.")
    @admin_only()
    async def note_add(self, interaction: discord.Interaction, player: str, text: str) -> None:
        row = self.add("n", player, text, str(interaction.user))
        await interaction.response.send_message(f"📝 Note #{row['id']} on **{self.md(row['player'])}** saved.",
                                                ephemeral=True)

    @note.command(name="list", description="Staff notes on a player.")
    @admin_only()
    async def note_list(self, interaction: discord.Interaction, player: str) -> None:
        rows = self.entries("n", player)
        await interaction.response.send_message(
            f"📝 **{self.md(player)}** – {len(rows)} note(s)\n{self._lines(rows)}" if rows else "No notes.",
            ephemeral=True)

    @note.command(name="remove", description="Remove a note by its number.")
    @admin_only()
    async def note_remove(self, interaction: discord.Interaction, note: int) -> None:
        ok = self.remove("n", note)
        await interaction.response.send_message("Removed." if ok else "No such note.", ephemeral=True)

    # ------------------------------------------------------------ elsewhere

    def profile_fields(self, member) -> list[tuple[str, str]]:
        player = self.linked_player(member.id)
        n = len(self.entries("w", player)) if player else 0
        return [("⚠️ Warnings", str(n))] if n else []

    def status(self) -> dict:
        return {"Warnings": len(self.entries("w")), "Notes": len(self.entries("n"))}
