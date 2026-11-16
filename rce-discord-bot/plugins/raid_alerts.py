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
  /raidalerts rf claim               your team's frequency: picked at random, one per team (or per player without
                                     a team), never given to anyone else
  /raidalerts rf new · /raidalerts rf release · /raidalerts rf mine   ·   /raidalerts rftest (admins): the server's raw answer
"""
from __future__ import annotations

import re
import secrets
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
                help="Players get a random frequency for their team with /raidalerts rf claim; a powered broadcaster on it = alert."),
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
        self.rf_seen: set[int] = set()              # every frequency broadcasting at the last check
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

    RF_RESERVED = range(4760, 4791)       # left out when a frequency is picked (often used by the game itself)

    def rf_claims(self) -> dict[int, dict]:
        """frequency -> {"uid": who set it up, "team": the in-game team it belongs to (None = that player alone),
        "at": when}. One frequency per team (or per player without a team), each frequency only once."""
        out = {}
        for key, value in self.data.items("rf:").items():
            row = dict(value) if isinstance(value, dict) else {"uid": int(value), "team": None, "at": 0}
            out[int(key[3:])] = {"uid": int(row.get("uid") or 0), "team": row.get("team"), "at": int(row.get("at") or 0)}
        return out

    def _team(self, user_id: int) -> tuple[int | None, list[str]]:
        """(team id, members) of the player's in-game team – (None, []) without one."""
        player = self.linked_player(user_id)
        found = self.bot.db.team_of(player) if player else None
        return (int(found[0]["team_id"]), list(found[1])) if found else (None, [])

    def rf_of(self, user_id: int) -> int | None:
        """The frequency of the player's team (or their own without a team). Keeps "one per team" true as teams
        change: a player's own frequency becomes their new team's; if the team has one already, theirs is freed."""
        claims = self.rf_claims()
        team, members = self._team(user_id)
        if team is None:
            for freq, row in claims.items():
                if row["uid"] == user_id and (row["team"] is None or self.bot.db.team_by_id(int(row["team"])) is None):
                    if row["team"] is not None:                  # their old team is gone: it's theirs alone again
                        self.data.set(f"rf:{freq}", {**row, "team": None})
                    return freq
            return None
        mates = {uid for uid in (self.linked_user(m) for m in members) if uid is not None} | {user_id}
        mine = sorted((row["at"], freq) for freq, row in claims.items()
                      if row["team"] == team or (row["team"] is None and row["uid"] in mates))
        if not mine:
            return None
        keep = next((f for _, f in mine if claims[f]["team"] == team), mine[0][1])
        for _, freq in mine:
            if freq != keep:
                self.data.delete(f"rf:{freq}")                   # a second one in the same team: freed
        if claims[keep]["team"] != team:
            self.data.set(f"rf:{keep}", {**claims[keep], "team": team})
        return keep

    def _free_frequency(self) -> int | None:
        taken = set(self.rf_claims()) | self.rf_seen
        for _ in range(200):
            freq = secrets.randbelow(9000) + 1000                # 1000–9999: nothing easy to guess
            if freq not in taken and freq not in self.RF_RESERVED:
                return freq
        return next((f for f in range(1000, 10000) if f not in taken and f not in self.RF_RESERVED), None)

    RF_HOWTO = ("In your base: an **explosion sensor** → power → an **RF broadcaster** set to {freq}. When "
                "something explodes there, {who} get an alert. Keep the number to yourselves.")

    def setup_rf(self, user_id: int, *, new: bool = False) -> str:
        """Gives the player's team (or the player alone) its frequency – a random one nobody else has. With
        new=True the old one is dropped and another is picked."""
        if not self.linked_player(user_id):
            return "Link your in-game player first: /link."
        team, _ = self._team(user_id)
        who = "you and your team" if team is not None else "you"
        freq = self.rf_of(user_id)
        if freq is not None and not new:
            return f"📡 {'Your team already has' if team is not None else 'You already have'} the frequency " \
                   f"**{freq}**. " + self.RF_HOWTO.format(freq=freq, who=who)
        if freq is not None:
            self.data.delete(f"rf:{freq}")
            self.data.delete(f"rflast:{freq}")
        freq = self._free_frequency()
        if freq is None:
            return "No free frequency is left right now – ask an admin."
        self.data.set(f"rf:{freq}", {"uid": int(user_id), "team": team, "at": self.now()})
        return (f"📡 Your {'team\'s ' if team is not None else ''}raid alarm frequency is **{freq}** – picked at random "
                f"and only yours. " + self.RF_HOWTO.format(freq=freq, who=who))

    def release_rf(self, user_id: int) -> bool:
        freq = self.rf_of(user_id)
        if freq is None:
            return False
        self.data.delete(f"rf:{freq}")
        self.data.delete(f"rflast:{freq}")
        return True

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
        self.rf_seen = self.parse_frequencies(raw)             # every frequency in use (never handed out again)
        active = {f for f in self.rf_seen if f in claims}
        fresh = sorted(active - self.rf_active)
        self.rf_active = active
        alerted = []
        now = time.time()
        for freq in fresh:
            row = claims[freq]
            uid = row["uid"]
            if now - float(self.data.get(f"rflast:{freq}", 0)) < self.setting("cooldown") * 60:
                continue
            self.data.set(f"rflast:{freq}", now)
            player = self.linked_player(uid) or "your base"
            targets = self.rf_targets(uid, player, row["team"])
            self.count_raid({name for _, name in targets} or {player})
            line = (f"📡 RF raid alarm on frequency {freq} – an explosion at your team's base!" if row["team"] is not None
                    else f"📡 RF raid alarm on frequency {freq} – an explosion at {player}'s base!")
            for target_uid, target_player in targets:
                await self._alert(target_uid, target_player, line)
            alerted.append(freq)
        return alerted

    def rf_targets(self, uid: int, player: str, team: int | None = None) -> list[tuple[int, str]]:
        """Who is alerted: every linked member of the frequency's team – or its owner (and their team mates)."""
        out: dict[int, str] = {}
        found = self.bot.db.team_by_id(int(team)) if team is not None else None
        if found is None:
            out[uid] = player
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

    @rf.command(name="claim", description="Get your team's RF raid alarm frequency (explosion sensor + RF broadcaster).")
    async def rf_claim_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.setup_rf(interaction.user.id), ephemeral=True)

    @rf.command(name="new", description="Get a NEW random frequency (the old one stops working).")
    async def rf_new_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.setup_rf(interaction.user.id, new=True), ephemeral=True)

    @rf.command(name="release", description="Give your (team's) RF frequency back.")
    async def rf_release_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Released." if self.release_rf(interaction.user.id)
                                                else "You have no frequency.", ephemeral=True)

    @rf.command(name="mine", description="Your (team's) RF raid alarm frequency and how to set it up.")
    async def rf_mine_cmd(self, interaction: discord.Interaction) -> None:
        freq = self.rf_of(interaction.user.id)
        team, _ = self._team(interaction.user.id)
        await interaction.response.send_message(
            (f"📡 Your {'team\'s ' if team is not None else ''}frequency: **{freq}** – explosion sensor → RF "
             f"broadcaster on {freq}." if freq else "No frequency yet – /raidalerts rf claim gives you one.")
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
