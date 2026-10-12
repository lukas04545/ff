"""Wipe manager: next wipe with countdown, announcements and resets.

/wipe info     – next wipe (everyone)
/wipe set      – set the next wipe + repeat (weekly / every 2 weeks / monthly = first same weekday)
/wipe clear    – no wipe planned
/wipe done     – the wipe just happened (runs the resets now)

Announcements: 24 h and 1 h before in Discord (WIPE_ANNOUNCE_CHANNEL_ID, ping WIPE_PING_ROLE_ID at 1 h),
1 h and 10 min before in-game, and "Server wiped!" at the time. At the wipe the bot can reset kit cooldowns
(WIPE_RESET_KITS), the leaderboard (WIPE_RESET_STATS) and points (WIPE_RESET_POINTS) – after a backup.
The wipe itself is done by G-Portal as usual; the bot only keeps everyone informed and starts fresh.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rce.rcon_client import RconError
from utils import admin_only, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.wipe")

KEY_NEXT, KEY_REPEAT, KEY_DONE = "wipe_next", "wipe_repeat", "wipe_announced"
REPEATS = {"none": "once", "weekly": "every week", "biweekly": "every 2 weeks", "monthly": "monthly (first {day})"}
STAGES = (("24h", 86400), ("1h", 3600), ("10m", 600))      # largest first
INPUT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M")
COLOUR = discord.Colour(0x5DADE2)


def get_tz(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown WIPE_TIMEZONE %r – using UTC.", name)
        return timezone.utc


def parse_when(text: str, tz) -> int | None:
    """"2026-10-15 18:00" (or 15.10.2026 18:00) in the given time zone -> Unix time."""
    text = " ".join(text.strip().split())
    for fmt in INPUT_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            continue
    return None


def next_occurrence(ts: int, repeat: str, tz, now: int | None = None) -> int | None:
    """The next wipe after `ts` for a repeat rule (same local time of day, also across daylight saving time)."""
    if repeat not in ("weekly", "biweekly", "monthly"):
        return None
    now = int(time.time()) if now is None else now
    local = datetime.fromtimestamp(ts, tz)
    while True:
        if repeat == "weekly":
            local = (local.replace(tzinfo=None) + timedelta(days=7)).replace(tzinfo=tz)
        elif repeat == "biweekly":
            local = (local.replace(tzinfo=None) + timedelta(days=14)).replace(tzinfo=tz)
        else:  # first same weekday of the next month (like Rust's forced wipe on the first Thursday)
            year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
            first = local.replace(year=year, month=month, day=1)
            local = first + timedelta(days=(local.weekday() - first.weekday()) % 7)
        if local.timestamp() > now:
            return int(local.timestamp())


class Wipe(commands.Cog):
    wipe = app_commands.Group(name="wipe", description="Next wipe, countdown and announcements", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.tz = get_tz(bot.config.wipe_timezone)

    async def cog_load(self) -> None:
        self.wipe_loop.start()

    async def cog_unload(self) -> None:
        self.wipe_loop.cancel()

    # ------------------------------------------------------------ Schedule

    def schedule(self) -> dict:
        db = self.bot.db
        nxt = db.get_value(KEY_NEXT)
        return {"next": int(nxt) if nxt else None, "repeat": db.get_value(KEY_REPEAT) or "none",
                "timezone": str(getattr(self.tz, "key", "UTC"))}

    def repeat_text(self, repeat: str, ts: int | None) -> str:
        day = datetime.fromtimestamp(ts, self.tz).strftime("%A") if ts else "weekday"
        return REPEATS.get(repeat, "once").format(day=day)

    def set_schedule(self, ts: int | None, repeat: str = "none") -> None:
        db = self.bot.db
        db.set_value(KEY_NEXT, str(ts) if ts else "")
        db.set_value(KEY_REPEAT, repeat if repeat in REPEATS else "none")
        db.set_value(KEY_DONE, "")

    def next_text(self) -> str | None:
        """For the status embed and /rust: "<t:…:F> (<t:…:R>)" or None."""
        nxt = self.schedule()["next"]
        return f"<t:{nxt}:F> (<t:{nxt}:R>)" if nxt else None

    # ------------------------------------------------------------ Loop

    @tasks.loop(seconds=30)
    async def wipe_loop(self) -> None:
        await self.tick(int(time.time()))

    @wipe_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, now: int) -> None:
        nxt = self.schedule()["next"]
        if not nxt:
            return
        if now >= nxt:
            await self.do_wipe(by="schedule")
            return
        done = set(filter(None, (self.bot.db.get_value(KEY_DONE) or "").split(",")))
        due = [key for key, seconds in STAGES if nxt - now <= seconds and key not in done]
        if not due:
            return
        # Only the closest stage (e.g. set 30 min before: no "24 hours" post anymore)
        self.bot.db.set_value(KEY_DONE, ",".join(sorted(done | set(due))))
        await self.announce(due[-1], nxt)

    def _channel(self) -> discord.abc.Messageable | None:
        cfg = self.bot.config
        channel_id = cfg.wipe_channel_id or cfg.events_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def _post(self, embed: discord.Embed, *, ping: bool) -> None:
        channel = self._channel()
        if channel is None:
            return
        role_id = self.bot.config.wipe_ping_role_id if ping else None
        try:
            await channel.send(content=f"<@&{role_id}>" if role_id else None, embed=embed,
                               allowed_mentions=discord.AllowedMentions(
                                   roles=[discord.Object(role_id)] if role_id else []))
        except discord.HTTPException as exc:
            log.warning("Wipe announcement failed: %s", exc)

    async def _say(self, text: str) -> None:
        try:
            await self.bot.rcon.command(f"say {text}", expect_response=False)
        except RconError:
            pass

    async def announce(self, stage: str, nxt: int) -> None:
        if stage in ("24h", "1h"):
            embed = discord.Embed(title="🧹 Wipe coming up", colour=COLOUR,
                                  description=f"The server wipes **<t:{nxt}:R>** (<t:{nxt}:F>).")
            await self._post(embed, ping=stage == "1h")
        if stage in ("1h", "10m"):
            minutes = max(1, round((nxt - time.time()) / 60))
            when = f"{minutes} minutes" if minutes < 90 else f"{round(minutes / 60)} hours"
            await self._say(f"<color=#5DADE2>Server wipe in {when}!</color>")
        log.info("Wipe announcement (%s before).", stage)

    async def do_wipe(self, *, by: str) -> list[str]:
        """The wipe is happening now: resets (after a backup), announcement, next date. Returns what was reset."""
        cfg, db = self.bot.config, self.bot.db
        done: list[str] = []
        if cfg.wipe_reset_kits or cfg.wipe_reset_stats or cfg.wipe_reset_points:
            monitor = self.bot.get_cog("Monitor")
            if monitor is not None and cfg.backup_enabled:
                await monitor.backup_now("pre-wipe")
        if cfg.wipe_reset_kits:
            n = db.reset_kit_claims() + db.reset_all_custom_claims()
            done.append(f"kit cooldowns ({n})")
        if cfg.wipe_reset_stats:
            db.reset_stats()
            done.append("leaderboard")
        if cfg.wipe_reset_points:
            db.reset_points()
            done.append("points")
        schedule = self.schedule()
        nxt = next_occurrence(schedule["next"] or int(time.time()), schedule["repeat"], self.tz)
        self.set_schedule(nxt, schedule["repeat"] if nxt else "none")
        embed = discord.Embed(title="🧹 Server wiped!", colour=COLOUR,
                              description="Fresh start – good luck out there!")
        if done:
            embed.add_field(name="Reset", value=", ".join(done))
        if nxt:
            embed.add_field(name="Next wipe", value=f"<t:{nxt}:F> (<t:{nxt}:R>)")
        await self._post(embed, ping=True)
        self.bot.audit(f"🧹 Wipe ({safe_md(by)}): {', '.join(done) or 'nothing reset'}"
                       + (f" · next <t:{nxt}:F>" if nxt else ""))
        return done

    # ------------------------------------------------------------ Commands

    @wipe.command(name="info", description="When is the next wipe?")
    async def info(self, interaction: discord.Interaction) -> None:
        s = self.schedule()
        if not s["next"]:
            text = self.bot.config.rust_server_wipe or "No wipe planned yet."
            await interaction.response.send_message(f"🧹 {safe_md(text)}", ephemeral=True)
            return
        embed = discord.Embed(title="🧹 Next wipe", colour=COLOUR,
                              description=f"**<t:{s['next']}:F>**\n<t:{s['next']}:R>")
        embed.set_footer(text=f"Repeats: {self.repeat_text(s['repeat'], s['next'])}")
        await interaction.response.send_message(embed=embed)

    @wipe.command(name="set", description="Sets the next wipe (time in WIPE_TIMEZONE).")
    @app_commands.describe(when="e.g. 2026-10-15 18:00 or 15.10.2026 18:00", repeat="Repeat after this wipe")
    @app_commands.choices(repeat=[app_commands.Choice(name=v.format(day="same weekday"), value=k)
                                  for k, v in REPEATS.items()])
    @admin_only()
    async def set_cmd(self, interaction: discord.Interaction, when: str,
                      repeat: app_commands.Choice[str] | None = None) -> None:
        ts = parse_when(when, self.tz)
        if ts is None:
            await interaction.response.send_message("⚠️ Use the format `2026-10-15 18:00` (or `15.10.2026 18:00`).",
                                                    ephemeral=True)
            return
        if ts <= time.time():
            await interaction.response.send_message("⚠️ That time is in the past.", ephemeral=True)
            return
        rep = repeat.value if repeat else "none"
        self.set_schedule(ts, rep)
        self.bot.audit(f"🧹 {safe_md(str(interaction.user))} set the next wipe: <t:{ts}:F> ({self.repeat_text(rep, ts)})")
        await interaction.response.send_message(
            f"✅ Next wipe: <t:{ts}:F> (<t:{ts}:R>) – {self.repeat_text(rep, ts)}.", ephemeral=True)

    @wipe.command(name="clear", description="No wipe planned (stops the countdown).")
    @admin_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        self.set_schedule(None)
        await interaction.response.send_message("✅ Wipe schedule cleared.", ephemeral=True)

    @wipe.command(name="done", description="The wipe just happened – run the resets and announce it now.")
    @admin_only()
    async def done(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        reset = await self.do_wipe(by=str(interaction.user))
        await interaction.followup.send(f"✅ Wipe done. Reset: {', '.join(reset) or 'nothing (see settings)'}.",
                                        ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Wipe(bot))
