"""Staff activity: who of the staff did how much – Discord admin commands and control panel changes, per day.

  /staffactivity [days]   a table per staff member (default: the last 7 days)
"""
from __future__ import annotations

import datetime

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from utils import admin_only, is_admin


class StaffActivity(Plugin):
    NAME = "Staff activity"
    ICON = "👮"
    DESCRIPTION = "Counts what every staff member does (Discord admin commands, panel changes) – /staffactivity."
    DEFAULT_ENABLED = True
    SETTINGS = (Setting("keep_days", "Keep … days", "int", 90, min=7, max=730),)

    def _day(self, ts: int | None = None) -> str:
        return datetime.datetime.fromtimestamp(ts or self.now(), datetime.timezone.utc).strftime("%Y-%m-%d")

    def record(self, who: str, kind: str, what: str) -> None:
        key = f"d:{self._day()}"
        day = dict(self.data.get(key, {}))
        row = day.setdefault(who[:64], {"commands": 0, "panel": 0, "last": ""})
        row[kind] = row.get(kind, 0) + 1
        row["last"] = what[:80]
        self.data.set(key, day)
        if self.now() - int(self.data.get("pruned", 0)) > 86400:
            self.data.set("pruned", self.now())
            cutoff = self._day(self.now() - self.setting("keep_days") * 86400)
            for k in list(self.data.items("d:")):
                if k[2:] < cutoff:
                    self.data.delete(k)

    def summary(self, days: int) -> list[tuple[str, dict]]:
        cutoff = self._day(self.now() - (days - 1) * 86400)
        out: dict[str, dict] = {}
        for k, day in self.data.items("d:").items():
            if k[2:] < cutoff:
                continue
            for who, row in day.items():
                agg = out.setdefault(who, {"commands": 0, "panel": 0, "days": 0, "last": ""})
                agg["commands"] += row.get("commands", 0)
                agg["panel"] += row.get("panel", 0)
                agg["days"] += 1
                if k[2:] >= agg.get("last_day", ""):
                    agg["last_day"], agg["last"] = k[2:], row.get("last", "")
        return sorted(out.items(), key=lambda kv: -(kv[1]["commands"] + kv[1]["panel"]))

    @Plugin.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        if is_admin(self.bot, interaction.user):
            self.record(str(interaction.user), "commands", "/" + getattr(command, "qualified_name", "?"))

    @Plugin.listener()
    async def on_rce_panel_action(self, user: str, text: str) -> None:
        self.record(f"panel:{user}", "panel", text)

    @app_commands.command(name="staffactivity", description="What every staff member did (default: last 7 days).")
    @app_commands.guild_only()
    @admin_only()
    async def activity_cmd(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7) -> None:
        rows = self.summary(days)
        lines = [f"**{self.md(who.removeprefix('panel:'))}**{' (panel)' if who.startswith('panel:') else ''} – "
                 f"{r['commands']} command(s), {r['panel']} panel change(s), active {r['days']} day(s) · last: "
                 f"{self.md(r['last'])}" for who, r in rows[:20]]
        await interaction.response.send_message(f"👮 **Staff activity – last {days} day(s)**\n" + "\n".join(lines)
                                                if lines else "No staff activity recorded yet.", ephemeral=True)

    def status(self) -> dict:
        return {"Staff active (7 days)": len(self.summary(7))}
