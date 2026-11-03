"""RCON repeater: loops console commands – every few seconds, minutes or hours, until stopped.

Two kinds of loops:
  * saved loops (panel: Plugins → RCON repeater), one per line:   EVERY | COMMANDS | NAME (optional)
      EVERY     "10s", "5m", "2h" (at least MIN_SECONDS)
      COMMANDS  console commands, several separated by ";;"
    Examples:
      30s | server.save
      5m | say Join our Discord! ;; say Vote for the server | Ads
    They run while the plugin is on; switch a single line off by starting it with #.
  * quick loops from Discord: /repeat start <command> <every> [times] · /repeat stop · /repeat list
    (admins). They run until stopped, for a number of times, or until the bot restarts.
Use the commands your server's console accepts – the bot only sends them on time.
"""
from __future__ import annotations

import re
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only

MIN_SECONDS = 5
MAX_QUICK = 10                       # quick loops at once
INTERVAL = re.compile(r"^(?:every\s+)?(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)$", re.I)
EXAMPLE = """# EVERY | COMMANDS (;; between several) | NAME
# 30s | server.save | Save"""


def seconds_of(text: str) -> int | None:
    m = INTERVAL.match(str(text or "").strip())
    if not m:
        return None
    unit = m[2][0].lower()
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600}[unit]


def parse(text: str) -> tuple[list[dict], list[str]]:
    """(loops, errors). Loop: {"name", "every" (s), "commands" [..]}."""
    loops, errors = [], []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[1]:
            errors.append(f"Line {n}: needs EVERY | COMMANDS")
            continue
        every = seconds_of(parts[0])
        if every is None:
            errors.append(f"Line {n}: EVERY must be like 10s, 5m or 2h")
            continue
        if every < MIN_SECONDS:
            errors.append(f"Line {n}: at least every {MIN_SECONDS} seconds")
            continue
        commands = [c.strip() for c in parts[1].split(";;") if c.strip()][:10]
        name = (parts[2] if len(parts) > 2 and parts[2] else f"Line {n}")[:60]
        loops.append({"name": name, "every": every, "commands": commands})
    return loops, errors


def fmt_every(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class RconRepeater(Plugin):
    NAME = "RCON repeater"
    ICON = "🔁"
    DESCRIPTION = ("Loops console commands – every few seconds, minutes or hours, until stopped. Saved loops here, "
                   "quick ones from Discord with /repeat start.")
    SETTINGS = (
        Setting("loops", "Saved loops", "textarea", EXAMPLE,
                help=f"One per line: EVERY | COMMANDS | NAME. EVERY = 10s, 5m, 2h (at least {MIN_SECONDS}s). Several "
                     "commands: separate with ;; . A line starting with # is off."),
        Setting("log_runs", "Write every run to the admin log", "bool", False,
                help="Off = only errors are logged (a loop every few seconds would flood it)."),
    )

    repeat = app_commands.Group(name="repeat", description="RCON repeater: loop console commands", guild_only=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.quick: dict[int, dict] = {}       # id -> {"command", "every", "left" (None = endless), "next", "by"}
        self._next_id = 1
        self._due: dict[str, float] = {}       # saved loop key -> next run

    # ------------------------------------------------------------ Loops

    def saved(self) -> list[dict]:
        return parse(self.setting("loops"))[0]

    @staticmethod
    def _key(loop: dict) -> str:
        return f"{loop['every']}|{';;'.join(loop['commands'])}"

    async def _send(self, label: str, commands: list[str]) -> list[str]:
        errors = []
        for cmd in commands:
            try:
                await self.bot.rcon.command(cmd, expect_response=False)
            except RconError as exc:
                errors.append(f"{cmd}: {exc}")
        self.data.set("runs", int(self.data.get("runs", 0)) + 1)
        if errors:
            self.audit(f"🔁 RCON repeater **{self.md(label)}**: " + "; ".join(errors)[:300])
        elif self.setting("log_runs"):
            self.audit(f"🔁 RCON repeater **{self.md(label)}** ran: " + self.md(" ;; ".join(commands))[:300])
        return errors

    def start_quick(self, command: str, every: int, times: int | None, by: str = "", now: float | None = None) -> int:
        if len(self.quick) >= MAX_QUICK:
            raise ValueError(f"At most {MAX_QUICK} quick loops at once – stop one first (/repeat stop).")
        if every < MIN_SECONDS:
            raise ValueError(f"At least every {MIN_SECONDS} seconds.")
        command = command.strip()
        if not command or "\n" in command:
            raise ValueError("One console command, please.")
        now = time.time() if now is None else now
        qid, self._next_id = self._next_id, self._next_id + 1
        self.quick[qid] = {"command": command[:500], "every": every, "left": times or None, "next": now, "by": by}
        return qid

    async def tick(self, now: float | None = None) -> list[str]:
        """Runs what's due. Returns the labels that ran."""
        now = time.time() if now is None else now
        ran = []
        keys = set()
        for loop in self.saved():
            key = self._key(loop)
            keys.add(key)
            due = self._due.setdefault(key, now)        # a new or changed line starts right away
            if due <= now:
                self._due[key] = now + loop["every"]
                await self._send(loop["name"], loop["commands"])
                ran.append(loop["name"])
        for key in [k for k in self._due if k not in keys]:
            del self._due[key]                           # removed from the settings
        for qid, q in list(self.quick.items()):
            if q["next"] <= now:
                q["next"] = now + q["every"]
                await self._send(f"#{qid}", [q["command"]])
                ran.append(f"#{qid}")
                if q["left"] is not None:
                    q["left"] -= 1
                    if q["left"] <= 0:
                        self.quick.pop(qid, None)
        return ran

    @tasks.loop(seconds=1)
    async def repeat_loop(self) -> None:
        if self.bot.rcon.connected:
            await self.tick()

    # ------------------------------------------------------------ Discord

    @repeat.command(name="start", description="Loop a console command until you stop it.")
    @app_commands.describe(command="The console command", every="How often, e.g. 10s, 5m, 1h",
                           times="Stop after this many runs (empty = until /repeat stop)")
    @admin_only()
    async def start_cmd(self, interaction: discord.Interaction, command: str, every: str,
                        times: app_commands.Range[int, 1, 100_000] | None = None) -> None:
        seconds = seconds_of(every)
        if seconds is None:
            await interaction.response.send_message("**every** must be like 10s, 5m or 1h.", ephemeral=True)
            return
        try:
            qid = self.start_quick(command, seconds, times, by=str(interaction.user))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.audit(f"🔁 {self.md(str(interaction.user))} started RCON loop #{qid}: `{command[:200]}` every "
                   f"{fmt_every(seconds)}" + (f", {times}×" if times else ""))
        await interaction.response.send_message(
            f"🔁 Loop **#{qid}** started: `{command[:200]}` every {fmt_every(seconds)}"
            + (f", {times} times." if times else " until /repeat stop."), ephemeral=True)

    @repeat.command(name="stop", description="Stop a quick loop (or all of them).")
    @app_commands.describe(loop="The loop number from /repeat list (empty = all quick loops)")
    @admin_only()
    async def stop_cmd(self, interaction: discord.Interaction, loop: int | None = None) -> None:
        if loop is None:
            n = len(self.quick)
            self.quick.clear()
            text = f"⏹️ Stopped {n} quick loop(s)." if n else "No quick loops are running."
        elif self.quick.pop(loop, None) is not None:
            text = f"⏹️ Loop #{loop} stopped."
        else:
            text = f"There's no loop #{loop} (/repeat list)."
        if text.startswith("⏹️"):
            self.audit(f"🔁 {self.md(str(interaction.user))}: {text}")
        await interaction.response.send_message(text + (" Saved loops: Plugins → RCON repeater." if loop is None
                                                        and self.saved() else ""), ephemeral=True)

    @repeat.command(name="list", description="Running loops.")
    @admin_only()
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        lines = [f"**#{qid}** `{self.md(q['command'][:120])}` every {fmt_every(q['every'])}"
                 + (f" · {q['left']} left" if q["left"] else "") for qid, q in self.quick.items()]
        lines += [f"📌 **{self.md(s['name'])}** `{self.md(' ;; '.join(s['commands'])[:120])}` every "
                  f"{fmt_every(s['every'])}" for s in self.saved()]
        await interaction.response.send_message("\n".join(lines) or "No loops – /repeat start, or add saved ones in "
                                                "Plugins → RCON repeater.", ephemeral=True)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        return parse(self.setting("loops"))[1]

    def status(self) -> dict:
        return {"Saved loops": len(self.saved()), "Quick loops running": len(self.quick),
                "Runs": int(self.data.get("runs", 0))}
