"""Weekly recap: once a week the bot posts the week's highlights – top killers, most playtime, best K/D, the most
embarrassing way to die, new players, peak population, bounties and the biggest team. Optional: the same as a daily
recap (every day at the hour) and a monthly recap (on the 1st at the hour).

Counted by the plugin itself from the killfeed and the player list (a leaderboard reset doesn't matter). Optional:
points for the week's top killer and the player with the most playtime.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.kill_sources import KillerType
from utils import admin_only, format_duration

COLOUR = discord.Colour(0x9B59B6)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MEDALS = ("🥇", "🥈", "🥉")


def last_due(now: int, weekday: int, hour: int, tz) -> int:
    """The most recent "weekday at hour" (local time) at or before `now`."""
    local = datetime.fromtimestamp(now, tz)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=(local.weekday() - weekday) % 7)
    if target > local:
        target -= timedelta(days=7)
    return int(target.timestamp())


class WeeklyRecap(Plugin):
    NAME = "Weekly recap"
    ICON = "📰"
    DESCRIPTION = ("Every week a recap post: top killers, most playtime, best K/D, funniest deaths, new players, "
                   "peak population. Content for your Discord – players come back to see if they made it.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("channel", "Channel", "channel", help="Empty = events channel."),
        Setting("day", "Day", "choice", "Sunday", choices=DAYS),
        Setting("hour", "Hour (0–23, wipe time zone)", "int", 18, min=0, max=23),
        Setting("reward", "Points for the top killer and top player", "int", 0, min=0, max=1_000_000,
                help="0 = no reward (only linked players can get it)."),
        Setting("min_kills", "Min. kills for the K/D ranking", "int", 5, min=1, max=1000),
        Setting("daily", "Also a daily recap", "bool", False, help="Every day at the hour above."),
        Setting("monthly", "Also a monthly recap", "bool", False, help="On the 1st of every month at the hour above."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_tick: float | None = None
        self.week: dict | None = None
        self.extra: dict[str, dict] = {}              # "day" / "month" buckets (same shape as the week)

    # ------------------------------------------------------------ Counting

    def _week(self) -> dict:
        if self.week is None:
            stored = self.data.get("week")
            self.week = stored if isinstance(stored, dict) else self._fresh()
        return self.week

    def _fresh(self) -> dict:
        return {"start": self.now(), "kills": {}, "deaths": {}, "playtime": {}, "causes": {}, "players": []}

    def _bucket(self, period: str = "week") -> dict:
        if period == "week":
            return self._week()
        if period not in self.extra:
            stored = self.data.get(f"bucket:{period}")
            self.extra[period] = stored if isinstance(stored, dict) else self._fresh()
        return self.extra[period]

    def _buckets(self) -> list[dict]:
        return [self._week(), self._bucket("day"), self._bucket("month")]

    def _save(self) -> None:
        if self.week is not None:
            self.data.set("week", self.week)
        for period, bucket in self.extra.items():
            self.data.set(f"bucket:{period}", bucket)

    @staticmethod
    def _inc(table: dict, key: str, amount: float = 1) -> None:
        table[key] = table.get(key, 0) + amount

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        for week in self._buckets():
            if ev.victim_type is KillerType.PLAYER:
                self._inc(week["deaths"], ev.victim)
                if ev.killer_type is KillerType.PLAYER and ev.killer.lower() != ev.victim.lower():
                    self._inc(week["kills"], ev.killer)
                elif ev.killer_type is not KillerType.PLAYER:
                    self._inc(week["causes"], ev.killer)

    @Plugin.listener()
    async def on_rce_suicide(self, ev) -> None:
        for week in self._buckets():
            self._inc(week["causes"], "Suicide")

    def add_playtime(self, players: list[str], seconds: float) -> None:
        for week in self._buckets():
            known = set(week["players"])
            for p in players:
                self._inc(week["playtime"], p, seconds)
                if p not in known:
                    week["players"].append(p)
                    known.add(p)

    # ------------------------------------------------------------ The post

    def build(self, now: int | None = None, period: str = "week") -> discord.Embed:
        now = self.now() if now is None else now
        week = self._bucket(period)
        kills, playtime = Counter(week["kills"]), Counter(week["playtime"])
        title = {"day": "Daily recap", "week": "Weekly recap", "month": "Monthly recap"}[period]
        embed = discord.Embed(title=f"📰 {title} – {self.server_name}", colour=COLOUR,
                              description=f"<t:{week['start']}:D> – <t:{now}:D>")

        def ranking(counter: Counter, fmt) -> str:
            return "\n".join(f"{MEDALS[i]} **{self.md(name)}** – {fmt(value)}"
                             for i, (name, value) in enumerate(counter.most_common(3))) or "–"

        embed.add_field(name="⚔️ Top killers", value=ranking(kills, lambda v: f"{int(v)} kills"))
        embed.add_field(name="⏱️ Most playtime", value=ranking(playtime, lambda v: format_duration(v)))
        min_kills = self.setting("min_kills")
        kd = Counter({n: k / max(week["deaths"].get(n, 0), 1) for n, k in kills.items() if k >= min_kills})
        embed.add_field(name="🎯 Best K/D", value=ranking(kd, lambda v: f"{v:.2f}"))
        if week["causes"]:
            cause, n = Counter(week["causes"]).most_common(1)[0]
            embed.add_field(name="🤡 Most embarrassing killer", value=f"**{self.md(cause)}** – {int(n)} victims")
        if week["deaths"]:
            name, n = Counter(week["deaths"]).most_common(1)[0]
            embed.add_field(name="💀 Died the most", value=f"**{self.md(name)}** – {int(n)} deaths")
        stats = [f"👥 **{len(week['players'])}** players"]
        since = datetime.fromtimestamp(week["start"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        new = self.bot.db.players_seen_since(since)
        if new:
            stats.append(f"🆕 **{new}** new")
        peak = max((p[1] for p in self.bot.db.population(week["start"])), default=0)
        if peak:
            stats.append(f"📈 peak **{int(peak)}** online")
        bounties = self.plugin("bounties")
        if bounties is not None:
            done = int(bounties.data.get("claimed_total", 0)) - int(week.get("bounties_at_start", 0))
            if done > 0:
                stats.append(f"🎯 **{done}** bounties collected")
        embed.add_field(name={"day": "📊 Today", "week": "📊 This week", "month": "📊 This month"}[period],
                        value=" · ".join(stats), inline=False)
        teams = self.bot.db.team_leaderboard("members", 1)
        if teams:
            t = teams[0]
            embed.add_field(name="👥 Biggest team", value=f"Team **{self.md(t['leader'] or '?')}** – {t['members']} members")
        reward = self.setting("reward")
        if reward and self.points_enabled:
            embed.set_footer(text=f"Top killer and top player get {self.fmt_points(reward)} (linked players).")
        return embed

    async def post_recap(self, period: str = "week") -> bool:
        week = self._bucket(period)
        if not week["players"] and not week["kills"]:
            self._reset(period)
            return False
        embed = self.build(period=period)
        channel = self.channel(self.setting("channel"), self.bot.config.events_channel_id)
        message = await self.post(channel, embed=embed)
        reward = self.setting("reward") if period == "week" else 0          # the reward stays weekly
        if message is not None and reward:
            winners = {n for n, _ in Counter(week["kills"]).most_common(1)} | \
                      {n for n, _ in Counter(week["playtime"]).most_common(1)}
            for name in winners:
                user_id = self.linked_user(name)
                if user_id is not None:
                    self.give_points(user_id, reward, "weekly recap top player")
        self._reset(period)
        return message is not None

    def _reset(self, period: str = "week") -> None:
        if period != "week":
            self.extra[period] = self._fresh()
            self._save()
            return
        self.week = self._fresh()
        bounties = self.plugin("bounties")
        if bounties is not None:
            self.week["bounties_at_start"] = int(bounties.data.get("claimed_total", 0))
        self._save()

    # ------------------------------------------------------------ Loop

    def due(self, now: int | None = None) -> int | None:
        now = self.now() if now is None else now
        slot = last_due(now, DAYS.index(self.setting("day")), self.setting("hour"), self.tz)
        last = self.data.get("last_slot")
        if last is None:                     # first start: the next slot is the first post
            self.data.set("last_slot", slot)
            return None
        return slot if slot > last else None

    @tasks.loop(minutes=1)
    async def recap_loop(self) -> None:
        now = time.monotonic()
        elapsed, self._last_tick = (min(now - self._last_tick, 120) if self._last_tick else 0), now
        if elapsed and self.bot.rcon.connected and self.bot.online_players:
            self.add_playtime(list(self.bot.online_players), elapsed)
        self._save()
        slot = self.due()
        if slot is not None:
            self.data.set("last_slot", slot)
            await self.post_recap()
        for period in ("day", "month"):
            slot = self.due_extra(period)
            if slot is not None:
                self.data.set(f"last_slot:{period}", slot)
                if self.setting("daily" if period == "day" else "monthly"):
                    await self.post_recap(period)
                else:
                    self._reset(period)

    def due_extra(self, period: str, now: int | None = None) -> int | None:
        """The daily slot (every day at the hour) or the monthly one (the 1st at the hour), once each."""
        now = self.now() if now is None else now
        local = datetime.fromtimestamp(now, self.tz)
        slot = local.replace(hour=self.setting("hour"), minute=0, second=0, microsecond=0)
        if period == "month":
            slot = slot.replace(day=1)
        if slot > local:
            slot = slot - timedelta(days=1) if period == "day" else \
                (slot.replace(day=1) - timedelta(days=1)).replace(day=1)
        slot_ts, last = int(slot.timestamp()), self.data.get(f"last_slot:{period}")
        if last is None:
            self.data.set(f"last_slot:{period}", slot_ts)
            return None
        return slot_ts if slot_ts > last else None

    async def on_unload(self) -> None:
        self._save()

    @app_commands.command(name="recap", description="Preview of the recap so far (only you see it).")
    @app_commands.choices(period=[app_commands.Choice(name="This week", value="week"),
                                  app_commands.Choice(name="Today", value="day"),
                                  app_commands.Choice(name="This month", value="month")])
    @app_commands.guild_only()
    @admin_only()
    async def recap(self, interaction: discord.Interaction, period: str = "week") -> None:
        await interaction.response.send_message(embed=self.build(period=period), ephemeral=True)

    def status(self) -> dict:
        week = self._week()
        return {"Players this week": len(week["players"]), "PvP kills this week": int(sum(week["kills"].values()))}
