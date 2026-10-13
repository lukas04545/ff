"""Achievements: badges for milestones (kills, playtime, /daily streaks, wipes played, bounties, invites).

Only for linked players (/customkit link). Every unlocked badge gives points, is announced in-game and in a
channel, and shows up in /achievements and /profile. Unlocked badges stay, even if the leaderboard is reset.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0xF1C40F)


@dataclass(frozen=True)
class Badge:
    id: str
    icon: str
    name: str
    metric: str        # kills / hours / streak / wipes / bounties / invites
    goal: int


BADGES = (
    Badge("kills10", "🔪", "First blood", "kills", 10),
    Badge("kills100", "⚔️", "Hunter", "kills", 100),
    Badge("kills500", "💀", "Predator", "kills", 500),
    Badge("kills1000", "☠️", "Legend of the island", "kills", 1000),
    Badge("hours10", "⏱️", "Settler", "hours", 10),
    Badge("hours50", "🏠", "Regular", "hours", 50),
    Badge("hours100", "🏰", "Veteran", "hours", 100),
    Badge("hours500", "👑", "No-lifer", "hours", 500),
    Badge("streak7", "🔥", "Daily devotion (7-day /daily streak)", "streak", 7),
    Badge("wipes5", "🧹", "Wipe survivor (5 wipes)", "wipes", 5),
    Badge("wipes10", "🗓️", "Old guard (10 wipes)", "wipes", 10),
    Badge("bounty1", "🎯", "Bounty hunter", "bounties", 1),
    Badge("bounty10", "💰", "Head collector (10 bounties)", "bounties", 10),
    Badge("invite1", "🤝", "Recruiter", "invites", 1),
    Badge("invite5", "📣", "Ambassador (5 invites)", "invites", 5),
)
METRIC_LABELS = {"kills": "kills", "hours": "hours played", "streak": "day streak", "wipes": "wipes played",
                 "bounties": "bounties collected", "invites": "players invited"}


class Achievements(Plugin):
    NAME = "Achievements"
    ICON = "🏅"
    DESCRIPTION = ("Badges for milestones – kills, hours played, /daily streaks, wipes played, bounties and invites. "
                   "Each one pays points and is announced. Keeps players chasing the next goal.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("points", "Points per badge", "int", 50, min=0, max=100_000),
        Setting("channel", "Announcement channel", "channel", help="Empty = events channel. Off: choose none and "
                                                                     "switch off in-game too."),
        Setting("ingame", "Announce in-game", "bool", True),
    )

    achievements = app_commands.Group(name="achievements", description="Badges for milestones", guild_only=True)

    # ------------------------------------------------------------ Metrics

    def badges(self) -> list[Badge]:
        """Badges that can be earned right now (bounty/invite badges only while those plugins run)."""
        return [b for b in BADGES if not (b.metric == "bounties" and not self.plugin("bounties"))
                and not (b.metric == "invites" and not self.plugin("invites"))]

    def unlocked(self, user_id: int) -> list[str]:
        return list(self.data.get(f"u:{user_id}", []))

    def metrics(self, user_id: int) -> dict[str, int]:
        db = self.bot.db
        player = self.linked_player(user_id)
        stats = db.player(player) if player else None
        wallet = db.wallet(user_id)
        bounties, invites = self.plugin("bounties"), self.plugin("invites")
        return {"kills": int(stats["kills"]) if stats else 0,
                "hours": int(stats["playtime_seconds"] // 3600) if stats else 0,
                "streak": int(wallet["daily_streak"] or 0),
                "wipes": len(self.data.get(f"w:{user_id}", [])),
                "bounties": bounties.claimed_count(user_id) if bounties else 0,
                "invites": invites.rewarded_count(user_id) if invites else 0}

    async def check_user(self, user_id: int, *, announce: bool = True) -> list[Badge]:
        have = set(self.unlocked(user_id))
        values = self.metrics(user_id)
        new = [b for b in self.badges() if b.id not in have and values[b.metric] >= b.goal]
        if not new:
            return []
        self.data.set(f"u:{user_id}", sorted(have | {b.id for b in new}))
        points = self.setting("points")
        player = self.linked_player(user_id) or "?"
        for badge in new:
            if points:
                self.give_points(user_id, points, f"badge {badge.name}")
            if not announce:
                continue
            reward = f" (+{self.fmt_points(points)})" if points and self.points_enabled else ""
            embed = discord.Embed(colour=COLOUR, description=f"🏅 <@{user_id}> (**{self.md(player)}**) unlocked "
                                                             f"{badge.icon} **{badge.name}**{reward}!")
            await self.post(self.channel(self.setting("channel"), self.bot.config.events_channel_id), embed=embed)
            if self.setting("ingame"):
                await self.say(f"<color=#f1c40f>{player}</color> unlocked the badge {badge.name}!")
        return new

    @tasks.loop(minutes=5)
    async def check_loop(self) -> None:
        # First run: badges players already earned before the plugin was on are given quietly (no announcement flood)
        first = not self.data.get("seeded")
        given = 0
        for row in self.bot.db.linked_players():
            given += len(await self.check_user(int(row["user_id"]), announce=not first))
        if first:
            self.data.set("seeded", True)
            if given:
                self.audit(f"Achievements started: {given} badges players had already earned were given quietly.")

    @tasks.loop(minutes=2)
    async def wipe_loop(self) -> None:
        """Counts a wipe as played for every linked player online at least once during it."""
        wipe_no = int(self.data.get("wipe_no", 0))
        for player in self.online_players():
            user_id = self.linked_user(player)
            if user_id is None:
                continue
            played = self.data.get(f"w:{user_id}", [])
            if wipe_no not in played:
                self.data.set(f"w:{user_id}", (played + [wipe_no])[-200:])

    @Plugin.listener()
    async def on_rce_wiped(self, next_wipe) -> None:
        self.data.set("wipe_no", int(self.data.get("wipe_no", 0)) + 1)

    # ------------------------------------------------------------ Commands

    def overview(self, user_id: int) -> discord.Embed:
        have, values = set(self.unlocked(user_id)), self.metrics(user_id)
        badges = self.badges()
        lines = []
        for b in badges:
            if b.id in have:
                lines.append(f"{b.icon} **{b.name}** ✅")
            else:
                value = values[b.metric]
                lines.append(f"🔒 {b.name} – {min(value, b.goal)}/{b.goal} {METRIC_LABELS[b.metric]}")
        earned = sum(1 for b in badges if b.id in have)
        embed = discord.Embed(title=f"🏅 Achievements – {earned}/{len(badges)}", colour=COLOUR,
                              description="\n".join(lines))
        if not self.linked_player(user_id):
            embed.set_footer(text="Link your in-game player with /customkit link to earn badges.")
        return embed

    @achievements.command(name="show", description="Your badges and progress (or someone else's).")
    @app_commands.describe(member="Member (default: you)")
    async def show(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        embed = self.overview(member.id)
        embed.title = f"{embed.title} · {member.display_name}"
        await interaction.response.send_message(embed=embed, ephemeral=member.id == interaction.user.id)

    # ------------------------------------------------------------ Web / profile

    def status(self) -> dict:
        counts = [len(v) for v in self.data.items("u:").values()]
        return {"Badges unlocked": sum(counts), "Members with badges": len([c for c in counts if c])}

    def profile_fields(self, member) -> list[tuple[str, str]]:
        have = set(self.unlocked(member.id))
        if not have:
            return []
        icons = " ".join(b.icon for b in BADGES if b.id in have)
        return [("🏅 Badges", f"{len(have)} {icons}"[:1024])]
