"""Auto events & timed commands: console commands run on a schedule – with an in-game/Discord announcement and a
warning a few minutes before for events.

One entry per line:   WHEN | NAME | COMMANDS | ANNOUNCEMENT (optional)
  WHEN      "every 90m" / "every 2h"  or  daily times "18:00" / "12:00,20:00"  (server time zone, see Wipe)
  COMMANDS  console commands, several separated by ";;"
Examples:
  every 30m | Save | server.save
  20:00 | Heli event | <your heli command> | 🚁 A patrol helicopter is on its way!
  every 2h | Airdrop | <your airdrop command> | 📦 Airdrop incoming!
Use the commands your server's console accepts – the bot only runs them on time.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only

EVERY = re.compile(r"^every\s+(\d+)\s*([mh])$", re.I)
CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
EXAMPLE = """# WHEN | NAME | COMMANDS (;; between several) | ANNOUNCEMENT
every 30m | Save | server.save"""


def parse(text: str) -> tuple[list[dict], list[str]]:
    """(entries, errors). Entry: {"name", "every" (s) or "times" ["HH:MM"], "commands" [..], "announce"}."""
    entries, errors = [], []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not parts[1] or not parts[2]:
            errors.append(f"Line {n}: needs WHEN | NAME | COMMANDS")
            continue
        when, name, commands = parts[0], parts[1][:60], [c.strip() for c in parts[2].split(";;") if c.strip()]
        entry = {"name": name, "commands": commands[:10], "announce": parts[3][:300] if len(parts) > 3 else ""}
        if m := EVERY.match(when):
            seconds = int(m[1]) * (3600 if m[2].lower() == "h" else 60)
            if seconds < 300:
                errors.append(f"Line {n}: at least every 5 minutes")
                continue
            entry["every"] = seconds
        else:
            times = [t.strip() for t in when.split(",") if t.strip()]
            if not times or not all(CLOCK.match(t) and int(t.split(":")[0]) < 24 and int(t.split(":")[1]) < 60
                                    for t in times):
                errors.append(f"Line {n}: WHEN must be like 'every 90m' or '18:00' / '12:00,20:00'")
                continue
            entry["times"] = times
        entries.append(entry)
    return entries, errors


class AutoEvents(Plugin):
    NAME = "Auto events & timed commands"
    ICON = "⏰"
    DESCRIPTION = ("Runs console commands on a schedule – every N minutes or at set times – with an announcement and "
                   "a warning before (events like airdrops or heli) or silently (timed commands like a save).")
    SETTINGS = (
        Setting("entries", "Schedule", "textarea", EXAMPLE,
                help="One per line: WHEN | NAME | COMMANDS | ANNOUNCEMENT. WHEN = 'every 90m', 'every 2h', '18:00' or "
                     "'12:00,20:00' (server time zone). Several commands: separate with ;; . Lines with an "
                     "announcement are announced (in game and in the channel) – also a warning before."),
        Setting("warn_minutes", "Warn before events (minutes)", "int", 5, min=0, max=60, help="0 = no warning."),
        Setting("channel", "Announcement channel", "channel", help="Empty = only in game."),
    )

    events = app_commands.Group(name="events", description="Auto events & timed commands", guild_only=True)

    def entries(self) -> list[dict]:
        return parse(self.setting("entries"))[0]

    def next_run(self, entry: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if "every" in entry:
            last = float(self.data.get(f"last:{entry['name']}", 0)) or now
            return last + entry["every"] if last + entry["every"] > now else now
        local = datetime.fromtimestamp(now, self.tz)
        best = None
        for t in entry["times"]:
            h, m = map(int, t.split(":"))
            cand = local.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand.timestamp() <= now - 59:
                cand += timedelta(days=1)
            best = cand.timestamp() if best is None else min(best, cand.timestamp())
        return best

    async def run(self, entry: dict, now: float | None = None) -> list[str]:
        """Runs an entry now (announcement first). Returns errors."""
        errors = []
        if entry["announce"]:
            await self.say(entry["announce"])
            if self.setting("channel"):
                await self.post(self.channel(self.setting("channel")), f"⏰ **{self.md(entry['name'])}** – "
                                                                       f"{self.md(entry['announce'])}")
        for cmd in entry["commands"]:
            try:
                await self.bot.rcon.command(cmd, expect_response=False)
            except RconError as exc:
                errors.append(f"{cmd}: {exc}")
        self.data.set(f"last:{entry['name']}", time.time() if now is None else now)
        self.data.set("runs", int(self.data.get("runs", 0)) + 1)
        if errors:
            self.audit(f"Auto event **{self.md(entry['name'])}** had errors: " + "; ".join(errors)[:300])
        return errors

    async def tick(self, now: float | None = None) -> list[str]:
        """Runs what's due (and warns). Returns the names that ran."""
        now = time.time() if now is None else now
        ran = []
        for entry in self.entries():
            name = entry["name"]
            if "every" in entry and not self.data.get(f"last:{name}"):
                self.data.set(f"last:{name}", now)          # first start: count from now, don't fire right away
                continue
            due = self.next_run(entry, now)
            warn = self.setting("warn_minutes") * 60
            if entry["announce"] and warn and due - warn <= now < due - warn + 60 \
                    and self.data.get(f"warned:{name}") != int(due):
                self.data.set(f"warned:{name}", int(due))
                await self.say(f"⏰ {name} in {self.setting('warn_minutes')} minutes!")
            fired_for = self.data.get(f"fired:{name}")
            if due <= now and fired_for != int(due):
                self.data.set(f"fired:{name}", int(due))
                await self.run(entry, now)
                ran.append(name)
        return ran

    @tasks.loop(seconds=30)
    async def schedule_loop(self) -> None:
        if self.bot.rcon.connected:
            await self.tick()

    # ------------------------------------------------------------ Discord

    @events.command(name="list", description="The schedule and when each runs next.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        entries, errors = parse(self.setting("entries"))
        lines = [f"⏰ **{self.md(e['name'])}** – next <t:{int(self.next_run(e))}:R>"
                 + (" · announced" if e["announce"] else " · silent") for e in entries]
        lines += [f"⚠️ {self.md(x)}" for x in errors]
        await interaction.response.send_message(embed=discord.Embed(
            title="⏰ Auto events & timed commands", colour=discord.Colour.blurple(),
            description="\n".join(lines) or "Nothing scheduled – set it up on the Plugins page."), ephemeral=True)

    @events.command(name="run", description="Runs a scheduled event now (admins).")
    @admin_only()
    async def run_cmd(self, interaction: discord.Interaction, name: str) -> None:
        entry = next((e for e in self.entries() if e["name"].lower() == name.strip().lower()), None)
        if entry is None:
            await interaction.response.send_message("No such event.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        errors = await self.run(entry)
        self.audit(f"{interaction.user} ran **{self.md(entry['name'])}** now")
        await interaction.followup.send("✅ Done." if not errors else "⚠️ " + "; ".join(errors)[:1500], ephemeral=True)

    @run_cmd.autocomplete("name")
    async def _name_ac(self, interaction, current: str):
        return [app_commands.Choice(name=e["name"], value=e["name"]) for e in self.entries()
                if current.lower() in e["name"].lower()][:25]

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        return parse(self.setting("entries"))[1]

    def status(self) -> dict:
        return {"Scheduled": len(self.entries()), "Runs so far": int(self.data.get("runs", 0))}
