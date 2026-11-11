"""Raid alerts – from the server's console log.

Console Edition servers can't run plugins and have no Rust+, and the server doesn't log building damage by itself.
So this alerts on console lines that match YOUR patterns (one regular expression per line). A pattern can name the
player or team in it: (?P<player>…) / (?P<team>…) – the alert then goes to that player and their in-game team.
Without a name in the pattern, every opted-in player whose name appears in the line is alerted.

Finding patterns: the plugin keeps the last console lines that contain one of the "watch words" (destroyed,
explosive, rocket, c4 …) – /raidalerts scan shows them, copy the part that matters into a pattern.
Players opt in with /raidalerts on (needs a linked player); they get a Discord DM. Optional: a channel too.

RF raid alarm (the reliable way on Console Edition): in the base, an explosion sensor powers an RF broadcaster set to
the player's own frequency. A powered broadcaster shows up in the server's broadcaster list (console command
rf.listboardcaster – Console Edition spells it like that), which the bot checks every few seconds. When a claimed
frequency appears, the player who claimed it and their linked in-game team get an alert (Discord DM, the RCE
Companion app, the channel).
  /raidalerts rf claim <frequency>   claim your frequency (1–9999, one per player, unique on the server)
  /raidalerts rf release · /raidalerts rf mine   ·   /raidalerts rftest (admins): the server's raw answer
"""
from __future__ import annotations

import re
import time
from collections import deque

from discord.ext import tasks

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
        Setting("rf", "RF raid alarm (explosion sensor + RF broadcaster)", "bool", True,
                help="Players claim a frequency with /raidalerts rf claim; a powered broadcaster on it = alert."),
        Setting("rf_command", "Command that lists the RF broadcasters", "text", "rf.listboardcaster",
                help="Console Edition: rf.listboardcaster (with that spelling). Check with /raidalerts rftest."),
        Setting("rf_seconds", "Check the broadcasters every … seconds", "int", 15, min=5, max=300),
    )

    raidalerts = app_commands.Group(name="raidalerts", description="Raid alerts", guild_only=True)
    rf = app_commands.Group(name="rf", description="RF raid alarm (explosion sensor + RF broadcaster)",
                            parent=raidalerts)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.seen: deque = deque(maxlen=40)
        self.rf_active: set[int] = set()            # claimed frequencies broadcasting at the last check
        self._rf_last = 0.0

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

    def raided(self, line: str) -> set[str] | None:
        """The players/teams a matching line names (an empty set = a raid without a name); None = no raid line."""
        for pat in self.patterns():
            m = pat.search(line)
            if not m:
                continue
            groups = m.groupdict()
            names = {groups[k].strip() for k in ("player", "team") if groups.get(k)}
            return names
        return None

    def count_raid(self, names: set[str]) -> None:
        """Raid statistics: per player/team and per day (all raids, not only opted-in players)."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.data.set("raids_total", int(self.data.get("raids_total", 0)) + 1)
        self.data.set(f"day:{day}", int(self.data.get(f"day:{day}", 0)) + 1)
        for name in names:
            key = f"raided:{name.lower()}"
            row = self.data.get(key) or {"name": name, "count": 0}
            row["count"], row["last"] = row["count"] + 1, int(time.time())
            self.data.set(key, row)

    def raid_stats(self, days: int = 7) -> dict:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
        per_day = {k[4:]: v for k, v in self.data.items("day:").items() if k[4:] >= cutoff}
        top = sorted(self.data.items("raided:").values(), key=lambda r: -r["count"])[:10]
        return {"total": int(self.data.get("raids_total", 0)), "recent": sum(per_day.values()), "per_day": per_day,
                "most_raided": top}

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
        names = self.raided(line)
        if names is not None:
            self.count_raid(names)
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
        companion = self.plugin("companion")
        if companion is not None:                     # the RCE Companion app on the player's phone
            companion.push(uid, "🚨 Raid alert", f"{player}: {line[:200]}", kind="raid")
            delivered = 1
        if self.setting("channel"):
            await self.post(self.channel(self.setting("channel")), f"🚨 Raid alert – **{self.md(player)}**: "
                                                                   f"{self.md(line[:200])}")
        self.data.set("alerts", int(self.data.get("alerts", 0)) + 1)
        return delivered

    # ------------------------------------------------------------ RF raid alarm

    def rf_claims(self) -> dict[int, int]:
        """frequency -> Discord user id."""
        return {int(k[3:]): int(v) for k, v in self.data.items("rf:").items()}

    def rf_of(self, user_id: int) -> int | None:
        return next((f for f, uid in self.rf_claims().items() if uid == user_id), None)

    def claim_rf(self, user_id: int, frequency: int) -> str:
        if not 1 <= frequency <= 9999:
            return "A frequency is 1 to 9999."
        owner = self.rf_claims().get(frequency)
        if owner is not None and owner != user_id:
            return f"Frequency {frequency} is taken – pick another one."
        old = self.rf_of(user_id)
        if old is not None and old != frequency:
            self.data.delete(f"rf:{old}")
        self.data.set(f"rf:{frequency}", user_id)
        return (f"📡 Your raid alarm frequency is **{frequency}**. In your base: an **explosion sensor** → power → an "
                f"**RF broadcaster** set to {frequency}. When something explodes there, you get an alert.")

    @staticmethod
    def parse_frequencies(text: str) -> set[int]:
        """The frequencies in the broadcaster list. Lines name them ("frequency 1234", "freq: 1234", "1234 …"):
        a number right after "freq…" counts; otherwise the first number of a line that has no position in it."""
        found: set[int] = set()
        for line in str(text or "").splitlines():
            m = re.search(r"freq(?:uency)?\s*[:=]?\s*(\d{1,4})\b", line, re.I)
            if m:
                found.add(int(m.group(1)))
                continue
            m = re.match(r"\s*(\d{1,4})\b(?![.,]\d)", line)
            if m and int(m.group(1)) > 0:
                found.add(int(m.group(1)))
        return found

    async def rf_check(self) -> list[int]:
        """Asks the server which broadcasters are on; alerts the owners of claimed frequencies that just came on.
        Returns the frequencies alerted."""
        claims = self.rf_claims()
        if not claims or not self.setting("rf"):
            return []
        try:
            raw = await self.bot.rcon.command(str(self.setting("rf_command") or "rf.listboardcaster"), timeout=8)
        except Exception:                                         # RCON down: try again next time
            return []
        active = {f for f in self.parse_frequencies(raw) if f in claims}
        fresh = sorted(active - self.rf_active)
        self.rf_active = active
        alerted = []
        now = time.time()
        for freq in fresh:
            uid = claims[freq]
            if now - float(self.data.get(f"rflast:{freq}", 0)) < self.setting("cooldown") * 60:
                continue
            self.data.set(f"rflast:{freq}", now)
            player = self.linked_player(uid) or "your base"
            self.count_raid({player})
            line = f"📡 RF raid alarm on frequency {freq} – an explosion at {player}'s base!"
            for target_uid, target_player in self.rf_targets(uid, player):
                await self._alert(target_uid, target_player, line)
            alerted.append(freq)
        return alerted

    def rf_targets(self, uid: int, player: str) -> list[tuple[int, str]]:
        """The frequency's owner and their linked in-game teammates."""
        out = {uid: player}
        found = self.bot.db.team_of(player) if player else None
        for mate in (found[1] if found else []):
            mate_uid = self.linked_user(mate)
            if mate_uid is not None:
                out.setdefault(mate_uid, mate)
        return list(out.items())

    @tasks.loop(seconds=5)
    async def rf_loop(self) -> None:
        if time.time() - self._rf_last >= self.setting("rf_seconds") and self.bot.rcon.connected:
            self._rf_last = time.time()
            await self.rf_check()

    @Plugin.listener()
    async def on_rce_console(self, msg) -> None:
        for line in (getattr(msg, "text", "") or "").splitlines():
            if line.strip():
                await self.handle(line.strip())

    # ------------------------------------------------------------ Discord

    @raidalerts.command(name="on", description="Get a DM when your base is raided (needs a linked player).")
    async def on_cmd(self, interaction: discord.Interaction) -> None:
        if not self.linked_player(interaction.user.id):
            await interaction.response.send_message("Link your in-game player first: /link.", ephemeral=True)
            return
        self.data.set(f"sub:{interaction.user.id}", interaction.user.id)
        note = "" if self.patterns() else " (The admins haven't set up the raid patterns yet.)"
        await interaction.response.send_message("🚨 Raid alerts on – you'll get a DM (keep DMs from this server "
                                                "open)." + note, ephemeral=True)

    @raidalerts.command(name="off", description="No more raid alert DMs.")
    async def off_cmd(self, interaction: discord.Interaction) -> None:
        self.data.delete(f"sub:{interaction.user.id}")
        await interaction.response.send_message("Raid alerts off.", ephemeral=True)

    @rf.command(name="claim", description="Claim your RF raid alarm frequency (explosion sensor + RF broadcaster).")
    async def rf_claim_cmd(self, interaction: discord.Interaction, frequency: app_commands.Range[int, 1, 9999]) -> None:
        if not self.linked_player(interaction.user.id):
            await interaction.response.send_message("Link your in-game player first: /link.", ephemeral=True)
            return
        await interaction.response.send_message(self.claim_rf(interaction.user.id, frequency), ephemeral=True)

    @rf.command(name="release", description="Give your RF frequency back.")
    async def rf_release_cmd(self, interaction: discord.Interaction) -> None:
        freq = self.rf_of(interaction.user.id)
        if freq is not None:
            self.data.delete(f"rf:{freq}")
        await interaction.response.send_message("Released." if freq else "You have no frequency.", ephemeral=True)

    @rf.command(name="mine", description="Your RF raid alarm frequency and how to set it up.")
    async def rf_mine_cmd(self, interaction: discord.Interaction) -> None:
        freq = self.rf_of(interaction.user.id)
        await interaction.response.send_message(
            (f"📡 Your frequency: **{freq}** – explosion sensor → RF broadcaster on {freq}." if freq else
             "No frequency yet – /raidalerts rf claim <1–9999>.")
            + ("" if self.setting("rf") else " (The RF alarm is switched off on this server.)"), ephemeral=True)

    @raidalerts.command(name="rftest", description="The server's RF broadcaster list as the bot sees it (admins).")
    @admin_only()
    async def rftest_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            raw = await self.bot.rcon.command(str(self.setting("rf_command") or "rf.listboardcaster"), timeout=8)
        except Exception as exc:
            await interaction.followup.send(f"The server didn't answer: {exc}", ephemeral=True)
            return
        freqs = sorted(self.parse_frequencies(raw))
        claims = self.rf_claims()
        await interaction.followup.send(
            f"**Raw answer** (`{self.setting('rf_command')}`):\n```\n{(raw or '(empty – no broadcaster on)')[:1400]}\n```"
            f"**Frequencies read:** {', '.join(map(str, freqs)) or 'none'}\n**Claimed:** {len(claims)}"
            + (f" · broadcasting now: {', '.join(str(f) for f in freqs if f in claims)}" if freqs else ""),
            ephemeral=True)

    @raidalerts.command(name="stats", description="Raid statistics: raids this week and the most raided.")
    async def stats_cmd(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7) -> None:
        st = self.raid_stats(days)
        top = "\n".join(f"{i}. **{self.md(r['name'])}** – {r['count']} raid{'s' if r['count'] != 1 else ''}"
                        for i, r in enumerate(st["most_raided"], 1))
        await interaction.response.send_message(
            f"🚨 **Raids** – {st['recent']} in the last {days} day(s), {st['total']} since tracking started"
            + (f"\n**Most raided**\n{top}" if top else ""), ephemeral=True)

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
        if not self.patterns() and not self.setting("rf"):
            out.append("No patterns yet – use /raidalerts scan after a raid to see which lines the server logs.")
        return out

    def status(self) -> dict:
        return {"Opted in": len(self.data.items("sub:")), "Alerts sent": int(self.data.get("alerts", 0)),
                "Raids counted": int(self.data.get("raids_total", 0)), "RF frequencies claimed": len(self.rf_claims()),
                "Lines seen by the scanner": len(self.seen)}
