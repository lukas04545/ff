"""Several Rust servers (servers.py): what the servers share while they run.

* One admin abuse protection for all: when a server punishes an abuser (cogs/abuse.py), it writes that to a shared
  list (abuse_events.jsonl in the main server's data/). Every other server reads it and runs the "remove admin"
  commands for that player on its own Rust server too.
* Extra servers only: every 15 seconds the main server's abuse protection settings and owner password are copied
  into this server's settings and applied right away. (That an extra server ends with the main bot: servers.py.)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

import servers
from utils import safe_md, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.multiserver")

BUS_FILE = "abuse_events.jsonl"
BUS_KEEP = 500                  # events kept in the file (older ones are dropped)
MAX_AGE = 600                   # seconds: older events are not acted on (e.g. a server that was off for a while)


class MultiServer(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.bus = servers.shared_data() / BUS_FILE
        self._offset = self.bus.stat().st_size if self.bus.exists() else 0     # only what happens from now on
        self._seen: set[str] = set()
        self._synced_state: tuple = ()

    async def cog_load(self) -> None:
        self.bus_loop.start()
        if servers.is_child():
            self.sync_loop.start()
        else:
            self.commands_loop.start()

    async def cog_unload(self) -> None:
        for loop in (self.bus_loop, self.sync_loop, self.commands_loop):
            loop.cancel()

    # ------------------------------------------------------------ Main server: one Discord bot for several servers

    def _commands_state(self) -> tuple:
        """Changes when a server joins/leaves the main bot or its slash commands change."""
        import discord_router
        state = []
        for sid in discord_router.shared_bot_servers():
            path = discord_router.commands_dir() / f"{sid}.json"
            state.append((sid, servers.server_name(sid), path.stat().st_mtime if path.exists() else 0))
        return tuple(state)

    @tasks.loop(seconds=20)
    async def commands_loop(self) -> None:
        import discord_router
        state = self._commands_state()
        if state == self._synced_state:
            return
        if state and discord_router.setup(self.bot) is None:
            return
        try:
            synced = await self.bot.sync_commands()
        except Exception as exc:              # Discord unreachable – next round tries again
            log.warning("Registering the slash commands for every server failed: %s", exc)
            return
        self._synced_state = state
        if state:
            log.info("Slash commands registered for %d server(s) on this Discord bot (%d commands).",
                     len(state) + 1, len(synced))

    @commands_loop.before_loop
    async def _before_commands(self) -> None:
        await self.bot.wait_until_ready()
        # the start already registered what was there (a server whose commands come later changes the state)
        self._synced_state = self._commands_state() if getattr(self.bot, "router", None) is not None else ()

    # ------------------------------------------------------------ Abuse: one protection for every server

    def broadcast_abuse(self, player: str, what: str) -> None:
        """Called after a punishment on this server – the other servers remove the player's admin too."""
        if not servers.load_registry():
            return                                  # only one server: nobody to tell
        event = {"id": uuid.uuid4().hex, "origin": servers.server_id(), "player": player, "what": what,
                 "time": int(time.time())}
        self._seen.add(event["id"])
        try:
            self.bus.parent.mkdir(parents=True, exist_ok=True)
            with open(self.bus, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
            self._trim()
        except OSError as exc:
            log.warning("Could not tell the other servers about the abuse: %s", exc)

    def _trim(self) -> None:
        lines = self.bus.read_text(encoding="utf-8").splitlines()
        if len(lines) > BUS_KEEP * 2:
            tmp = self.bus.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-BUS_KEEP:]) + "\n", encoding="utf-8")
            os.replace(tmp, self.bus)

    def read_new(self) -> list[dict]:
        try:
            size = self.bus.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0                         # the file was trimmed
        if size == self._offset:
            return []
        with open(self.bus, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        complete = chunk.rfind(b"\n") + 1            # a line still being written waits for the next round
        self._offset += complete
        events = []
        for line in chunk[:complete].decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("id") in self._seen:
                continue
            self._seen.add(event.get("id"))
            if event.get("origin") != servers.server_id() and time.time() - int(event.get("time") or 0) < MAX_AGE:
                events.append(event)
        return events

    async def handle(self, event: dict) -> None:
        cfg = self.bot.config
        guard = self.bot.get_cog("AbuseGuard")
        player = str(event.get("player") or "")
        if not cfg.abuse_protection or guard is None or not player:
            return
        name = sanitize_player_name(player)
        results = await guard._run_all(cfg.abuse_remove_admin_commands, name)
        where = servers.server_name(str(event.get("origin")))
        failed = [c for c, r in results if r.startswith("failed:")]
        log.warning("Admin abuse on %s by %s – admin removed here too%s", where, player,
                    f" ({len(failed)} command(s) failed)" if failed else "")
        self.bot.audit(f"🚨 **{safe_md(player)}** abused admin on **{safe_md(where)}** "
                       f"({safe_md(str(event.get('what') or ''))}) – admin removed on this server too"
                       + (f" – ⚠️ failed: {', '.join(c.split(' ')[0] for c in failed)}" if failed else ""))

    @tasks.loop(seconds=5)
    async def bus_loop(self) -> None:
        for event in self.read_new():
            try:
                await self.handle(event)
            except Exception:
                log.exception("Handling an abuse event from another server failed")

    @bus_loop.before_loop
    async def _before_bus(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ Extra servers: shared settings, main bot alive

    @tasks.loop(seconds=15)
    async def sync_loop(self) -> None:
        changed = servers.sync_env(servers.server_id())
        if not changed:
            return
        from config import ConfigError, load_config
        web = getattr(self.bot, "web", None)
        if "WEB_PASSWORD" in changed and web is not None:
            web.ws.password = web.password_hash = changed["WEB_PASSWORD"]
        if set(changed) - {"WEB_PASSWORD"}:
            try:
                await self.bot.apply_config(load_config(".env"))
            except ConfigError as exc:
                log.warning("Shared settings from the main server could not be applied: %s", exc)
                return
        log.info("Took over from the main server: %s", ", ".join(sorted(changed)))


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(MultiServer(bot))
