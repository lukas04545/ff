"""Challenges: daily and weekly tasks with point rewards – tracked automatically from the killfeed and the player
list (linked players only get the reward; others collect it once they link).

Kinds: PvP kills · playtime (minutes) · days joined · kills without dying (streak) · animals/NPCs killed
/challenges   your progress (and rewards waiting for a link)
"""
from __future__ import annotations

import hashlib
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.kill_sources import KillerType

COLOUR = discord.Colour(0xF39C12)
# kind: (text, daily target, weekly target)
KINDS = {
    "kills": ("Kill {n} players", 5, 30),
    "playtime": ("Play {n} minutes", 60, 600),
    "streak": ("Get {n} kills in a row without dying", 3, 8),
    "pve": ("Kill {n} animals or NPCs", 10, 60),
    "join": ("Join the server on {n} different days", 1, 5),
}


def period_key(kind: str, now: float | None = None) -> str:
    t = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", t) if kind == "daily" else time.strftime("%G-W%V", t)


class Challenges(Plugin):
    NAME = "Challenges"
    ICON = "🏅"
    DESCRIPTION = "Daily and weekly challenges (kills, playtime, streaks, PvE, joins) with point rewards – automatic."
    SETTINGS = (
        Setting("daily_count", "Daily challenges", "int", 3, min=0, max=5),
        Setting("weekly_count", "Weekly challenges", "int", 2, min=0, max=5),
        Setting("daily_reward", "Reward per daily challenge", "int", 100, min=0, max=1_000_000),
        Setting("weekly_reward", "Reward per weekly challenge", "int", 750, min=0, max=10_000_000),
        Setting("ingame", "Announce completions in game", "bool", True),
        Setting("channel", "Announcement channel", "channel", help="Empty = not announced on Discord."),
    )

    # ------------------------------------------------------------ Which challenges are active

    def active(self, kind: str, now: float | None = None) -> list[dict]:
        """The challenges of this day/week – the same for everyone, picked from the period's name."""
        key = period_key(kind, now)
        count = self.setting("daily_count" if kind == "daily" else "weekly_count")
        rng = random.Random(int(hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:8], 16))
        picks = rng.sample(sorted(KINDS), k=min(count, len(KINDS)))
        out = []
        for name in picks:
            text, daily, weekly = KINDS[name]
            target = daily if kind == "daily" else weekly
            out.append({"id": f"{kind}:{key}:{name}", "kind": kind, "type": name, "target": target,
                        "text": text.format(n=target),
                        "reward": self.setting("daily_reward" if kind == "daily" else "weekly_reward")})
        return out

    def all_active(self, now: float | None = None) -> list[dict]:
        return self.active("daily", now) + self.active("weekly", now)

    # ------------------------------------------------------------ Progress

    def _key(self, player: str, cid: str) -> str:
        return f"p:{player.lower()}:{cid}"

    def progress(self, player: str, cid: str) -> int:
        return int(self.data.get(self._key(player, cid), 0))

    async def bump(self, player: str, ctype: str, amount: int = 1, *, absolute: bool = False) -> list[dict]:
        """Counts progress for every active challenge of this type; returns the ones just completed."""
        done = []
        for ch in self.all_active():
            if ch["type"] != ctype:
                continue
            before = self.progress(player, ch["id"])
            if before >= ch["target"]:
                continue
            now = max(before, amount) if absolute else before + amount
            self.data.set(self._key(player, ch["id"]), min(now, ch["target"]))
            if now >= ch["target"]:
                done.append(ch)
                await self._complete(player, ch)
        return done

    async def _complete(self, player: str, ch: dict) -> None:
        uid = self.linked_user(player)
        if uid is not None:
            self.give_points(uid, ch["reward"], f"challenge: {ch['text']}")
        else:                                               # paid once they link (/challenges)
            pending = self.data.get(f"pending:{player.lower()}", [])
            self.data.set(f"pending:{player.lower()}", pending + [ch["reward"]])
        self.data.set("completed", int(self.data.get("completed", 0)) + 1)
        label = "Daily" if ch["kind"] == "daily" else "Weekly"
        if self.setting("ingame"):
            await self.say(f"<color=#f39c12>{label} challenge done!</color> {player}: {ch['text']} – "
                           f"{self.fmt_points(ch['reward'])}" + ("" if uid else " (link on Discord to collect)"))
        if self.setting("channel"):
            await self.post(self.channel(self.setting("channel")),
                            f"🎯 **{self.md(player)}** finished the {label.lower()} challenge *{ch['text']}* – "
                            f"{self.fmt_points(ch['reward'])}")

    # ------------------------------------------------------------ Tracking

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        if ev.killer_type is KillerType.PLAYER and ev.victim_type is KillerType.PLAYER and \
                ev.killer.lower() != ev.victim.lower():
            await self.bump(ev.killer, "kills")
            streak = int(self.data.get(f"streak:{ev.killer.lower()}", 0)) + 1
            self.data.set(f"streak:{ev.killer.lower()}", streak)
            await self.bump(ev.killer, "streak", streak, absolute=True)
        elif ev.killer_type is KillerType.PLAYER and ev.victim_type is not KillerType.PLAYER:
            await self.bump(ev.killer, "pve")
        if ev.victim_type is KillerType.PLAYER:
            self.data.set(f"streak:{ev.victim.lower()}", 0)

    @Plugin.listener()
    async def on_rce_player_join(self, player: str) -> None:
        today = period_key("daily")
        if self.data.get(f"joined:{player.lower()}") != today:
            self.data.set(f"joined:{player.lower()}", today)
            await self.bump(player, "join")

    @tasks.loop(minutes=1)
    async def playtime_loop(self) -> None:
        for player in self.online_players():
            await self.bump(player, "playtime")

    # ------------------------------------------------------------ Discord

    def collect_pending(self, user_id: int) -> int:
        player = self.linked_player(user_id)
        if not player:
            return 0
        pending = self.data.get(f"pending:{player.lower()}", [])
        if not pending:
            return 0
        self.data.delete(f"pending:{player.lower()}")
        total = sum(int(x) for x in pending)
        self.give_points(user_id, total, "challenges finished before linking")
        return total

    @app_commands.command(name="challenges", description="Today's and this week's challenges and your progress.")
    @app_commands.guild_only()
    async def challenges_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id)
        got = self.collect_pending(interaction.user.id)
        embed = discord.Embed(title="🎯 Challenges", colour=COLOUR)
        for kind, label in (("daily", "Today"), ("weekly", "This week")):
            lines = []
            for ch in self.active(kind):
                n = self.progress(player, ch["id"]) if player else 0
                mark = "✅" if n >= ch["target"] else "▫️"
                lines.append(f"{mark} {ch['text']} – **{n}/{ch['target']}** · {self.fmt_points(ch['reward'])}")
            if lines:
                embed.add_field(name=label, value="\n".join(lines), inline=False)
        if not player:
            embed.set_footer(text="Link your player with /customkit link to track your progress")
        content = f"🎁 Collected {self.fmt_points(got)} from challenges you finished before linking." if got else None
        await interaction.response.send_message(content=content, embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        return [] if self.points_enabled else ["Points & shop are switched off – rewards need points."]

    def status(self) -> dict:
        return {"Active challenges": len(self.all_active()), "Completed": int(self.data.get("completed", 0))}
