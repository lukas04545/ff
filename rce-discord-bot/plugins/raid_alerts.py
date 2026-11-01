"""Raid alerts – from the server's console log.

Console Edition servers can't run plugins and have no Rust+, and the server doesn't log building damage by itself.
So this alerts on console lines that match YOUR patterns (one regular expression per line). A pattern can name the
player or team in it: (?P<player>…) / (?P<team>…) – the alert then goes to that player and their in-game team.
Without a name in the pattern, every opted-in player whose name appears in the line is alerted.

Finding patterns: the plugin keeps the last console lines that contain one of the "watch words" (destroyed,
explosive, rocket, c4 …) – /raidalerts scan shows them, copy the part that matters into a pattern.
Players opt in with /raidalerts on (needs a linked player); they get a Discord DM. Optional: a channel too.
"""
from __future__ import annotations

import re
import time
from collections import deque

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from utils import admin_only

WATCH = "destroyed, destroy, explosive, explosion, rocket, c4, satchel, raid, damaged"


class RaidAlerts(Plugin):
    NAME = "Raid alerts"
    ICON = "🚨"
    DESCRIPTION = ("Alerts opted-in players on Discord when a console log line matches your raid patterns (Console "
                   "Edition doesn't log raids by itself – the scanner helps you find lines to use).")
    SETTINGS = (
        Setting("patterns", "Patterns (one regular expression per line)", "textarea", "",
                help="E.g.  (?P<player>.+?)'s .* was destroyed  – (?P<player>) / (?P<team>) say who to alert. Empty = "
                     "no alerts yet (use /raidalerts scan to see which lines your server logs)."),
        Setting("watch", "Watch words for the scanner", "text", WATCH),
        Setting("cooldown", "Alerts per player at most every … minutes", "int", 5, min=1, max=240),
        Setting("channel", "Also post in this channel", "channel", help="Empty = only DMs to the players."),
    )

    raidalerts = app_commands.Group(name="raidalerts", description="Raid alerts", guild_only=True)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.seen: deque = deque(maxlen=40)

    # ------------------------------------------------------------ Matching

    def patterns(self) -> list[re.Pattern]:
        out = []
        for line in str(self.setting("patterns") or "").splitlines():
            if line.strip() and not line.strip().startswith("#"):
                try:
                    out.append(re.compile(line.strip(), re.I))
                except re.error:
                    pass
        return out

    def subscribers(self) -> dict[str, int]:
        """in-game name (lower) -> Discord user id of everyone who opted in."""
        out = {}
        for key, uid in self.data.items("sub:").items():
            player = self.linked_player(int(uid))
            if player:
                out[player.lower()] = int(uid)
        return out

    def targets(self, line: str) -> list[tuple[int, str]]:
        """(user id, player) to alert for this line."""
        subs = self.subscribers()
        if not subs:
            return []
        for pat in self.patterns():
            m = pat.search(line)
            if not m:
                continue
            names: set[str] = set()
            groups = m.groupdict()
            if groups.get("player"):
                names.add(groups["player"].strip().lower())
                found = self.bot.db.team_of(groups["player"].strip())
                if found:
                    names.update(x.lower() for x in found[1])
            if groups.get("team"):
                team = groups["team"].strip()
                members = self.bot.db.team_members(int(team)) if team.isdigit() else []
                names.update(x.lower() for x in members)
            if not names:
                names = {p for p in subs if p in line.lower()}
            return [(subs[n], n) for n in sorted(names) if n in subs]
        return []

    async def handle(self, line: str) -> int:
        words = [w.strip().lower() for w in str(self.setting("watch") or "").split(",") if w.strip()]
        if any(w in line.lower() for w in words):
            self.seen.append((int(time.time()), line[:300]))
        sent = 0
        now = time.time()
        for uid, player in self.targets(line):
            if now - float(self.data.get(f"last:{uid}", 0)) < self.setting("cooldown") * 60:
                continue
            self.data.set(f"last:{uid}", now)
            sent += await self._alert(uid, player, line)
        return sent

    async def _alert(self, uid: int, player: str, line: str) -> int:
        text = f"🚨 **Raid alert** for **{self.md(player)}** on {self.md(self.server_name)}:\n> {self.md(line[:300])}"
        delivered = 0
        user = self.bot.get_user(uid)
        try:
            if user is None:
                user = await self.bot.fetch_user(uid)
            await user.send(text)
            delivered = 1
        except (discord.HTTPException, AttributeError):
            pass
        if self.setting("channel"):
            await self.post(self.channel(self.setting("channel")), f"🚨 Raid alert – **{self.md(player)}**: "
                                                                   f"{self.md(line[:200])}")
        self.data.set("alerts", int(self.data.get("alerts", 0)) + 1)
        return delivered

    @Plugin.listener()
    async def on_rce_console(self, msg) -> None:
        for line in (getattr(msg, "text", "") or "").splitlines():
            if line.strip():
                await self.handle(line.strip())

    # ------------------------------------------------------------ Discord

    @raidalerts.command(name="on", description="Get a DM when your base is raided (needs a linked player).")
    async def on_cmd(self, interaction: discord.Interaction) -> None:
        if not self.linked_player(interaction.user.id):
            await interaction.response.send_message("Link your in-game player first: /customkit link.", ephemeral=True)
            return
        self.data.set(f"sub:{interaction.user.id}", interaction.user.id)
        note = "" if self.patterns() else " (The admins haven't set up the raid patterns yet.)"
        await interaction.response.send_message("🚨 Raid alerts on – you'll get a DM (keep DMs from this server "
                                                "open)." + note, ephemeral=True)

    @raidalerts.command(name="off", description="No more raid alert DMs.")
    async def off_cmd(self, interaction: discord.Interaction) -> None:
        self.data.delete(f"sub:{interaction.user.id}")
        await interaction.response.send_message("Raid alerts off.", ephemeral=True)

    @raidalerts.command(name="scan", description="Recent console lines with watch words – to build patterns (admins).")
    @admin_only()
    async def scan_cmd(self, interaction: discord.Interaction) -> None:
        lines = [f"<t:{t}:R> `{line.replace('`', 'ˋ')[:180]}`" for t, line in list(self.seen)[-15:]]
        await interaction.response.send_message(
            "\n".join(lines)[:1900] or "No console line with a watch word since the bot started – wait for a raid, or "
                                       "add words under Watch words.", ephemeral=True)

    def problems(self) -> list[str]:
        out = []
        for line in str(self.setting("patterns") or "").splitlines():
            if line.strip() and not line.strip().startswith("#"):
                try:
                    re.compile(line.strip())
                except re.error as exc:
                    out.append(f"Pattern “{line.strip()[:40]}”: {exc}")
        if not self.patterns():
            out.append("No patterns yet – use /raidalerts scan after a raid to see which lines the server logs.")
        return out

    def status(self) -> dict:
        return {"Opted in": len(self.data.items("sub:")), "Alerts sent": int(self.data.get("alerts", 0)),
                "Lines seen by the scanner": len(self.seen)}
