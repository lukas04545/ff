"""Runs one bot process per active tenant server – each with its own Discord token (own gateway connection and own
rate limits), own folder, own database and own web port. One crashing or rate-limited bot never touches another.

reconcile() (every few seconds) compares what SHOULD run (provisioning.runnable: server active, customer active,
subscription paid or in grace, token set) with what runs, and starts/stops processes. Crashes restart with
exponential backoff (5 s … 5 min); 5 crashes within 10 minutes = "crashloop": the server is left stopped and the
owner panel shows it until someone looks. Each process gets only its own environment (provisioning.environment),
a memory limit (Linux) and its own log file with the last lines kept for the owner panel.

Scaling later: the same Supervisor runs as an agent on more hosts, each reconciling the servers whose `node` is its
name (see ARCHITECTURE.md); one container per server is a drop-in Launcher.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from control_plane import provisioning
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

log = logging.getLogger("cp.supervisor")
BACKOFF = (5, 15, 30, 60, 120, 300)
CRASHLOOP_COUNT, CRASHLOOP_WINDOW = 5, 600
SHARED_START_GAP = 6          # seconds between two logins of the shared bot (Discord allows 1 identify / 5 s)
STOP_TIMEOUT = 20


@dataclass
class Proc:
    server_id: int
    process: asyncio.subprocess.Process | None = None
    started: float = 0.0
    crashes: deque = field(default_factory=lambda: deque(maxlen=CRASHLOOP_COUNT))
    restarts: int = 0
    next_start: float = 0.0
    state: str = "stopped"            # starting, running, crashed, crashloop, stopped
    tail: deque = field(default_factory=lambda: deque(maxlen=60))
    watcher: asyncio.Task | None = None
    stopping: bool = False
    env_hash: str = ""                # what it was started with – a change (add-on, token, guild) restarts it


class ProcessLauncher:
    """Starts `python bot.py` for a tenant. Swap for a container launcher with the same two methods."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def start(self, env: dict[str, str], home: Path) -> asyncio.subprocess.Process:
        memory = self.settings.memory_mb * 1024 * 1024

        def limits() -> None:                           # runs in the child before exec (Linux/macOS)
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_AS, (memory * 4, memory * 4))   # address space (Python reserves)
            except (ImportError, ValueError, OSError):
                pass
            os.setsid()                                   # own process group: signals don't spill over
        return await asyncio.create_subprocess_exec(
            self.settings.python, str(self.settings.bot_dir / "bot.py"), cwd=str(home), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            preexec_fn=limits if sys.platform != "win32" else None)


class Supervisor:
    def __init__(self, db: DB, box: SecretBox, settings: Settings, launcher=None) -> None:
        self.db, self.box, self.settings = db, box, settings
        self.launcher = launcher or ProcessLauncher(settings)
        self.procs: dict[int, Proc] = {}
        self._loop_task: asyncio.Task | None = None
        self._last_shared_start = 0.0

    # ------------------------------------------------------------ Reconcile

    def wanted(self) -> set[int]:
        rows = self.db.q("SELECT id FROM servers WHERE status = 'active' AND node = 'local'")
        return {r["id"] for r in rows if provisioning.runnable(self.db, r["id"], self.settings)}

    def _env(self, sid: int) -> tuple[dict, str]:
        env = provisioning.environment(self.db, self.box, self.settings, sid)
        return env, hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()

    async def reconcile(self) -> dict:
        want, now = self.wanted(), time.time()
        started, stopped = [], []
        for sid in list(self.procs):
            if sid not in want and self.procs[sid].state in ("starting", "running", "crashed"):
                await self.stop(sid)
                stopped.append(sid)
        for sid in want:
            p = self.procs.setdefault(sid, Proc(sid))
            if p.state == "running" and p.env_hash and self._env(sid)[1] != p.env_hash:
                await self.stop(sid)                     # add-on switched / token or guild changed: new setup
                p.state = "stopped"
            if p.state in ("stopped", "crashed") and now >= p.next_start:
                shared = self._env(sid)[0].get("SAAS_SHARED_BOT") == "1"
                if shared and time.time() - self._last_shared_start < SHARED_START_GAP:
                    continue                             # next reconcile (a few seconds) starts it
                await self.start(sid)
                if shared:
                    self._last_shared_start = time.time()
                started.append(sid)
        self._write_health()
        return {"started": started, "stopped": stopped}

    async def run_forever(self, every: float = 5.0) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            await asyncio.sleep(every)

    # ------------------------------------------------------------ One process

    async def start(self, sid: int) -> None:
        p = self.procs.setdefault(sid, Proc(sid))
        s = self.db.one("SELECT home_dir, slug FROM servers WHERE id = ?", (sid,))
        env, p.env_hash = self._env(sid)
        p.stopping = False
        try:
            p.process = await self.launcher.start(env, Path(s["home_dir"]))
        except Exception as exc:
            log.error("server %s (%s) didn't start: %s", sid, s["slug"], exc)
            self._crashed(p, f"start failed: {exc}")
            return
        p.state, p.started = "running", time.time()
        p.watcher = asyncio.create_task(self._watch(p, Path(s["home_dir"])))
        log.info("server %s (%s) started, pid %s", sid, s["slug"], p.process.pid)

    async def _watch(self, p: Proc, home: Path) -> None:
        proc = p.process
        logfile = home / "logs" / "bot.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        out = open(logfile, "ab")
        try:
            if proc.stdout is not None:
                async for line in proc.stdout:
                    out.write(line)
                    p.tail.append(line.decode("utf-8", "replace").rstrip()[:300])
                    if out.tell() > 20 * 1024 * 1024:            # rotate at 20 MB
                        out.close()
                        os.replace(logfile, logfile.with_suffix(".log.1"))
                        out = open(logfile, "ab")
        finally:
            out.close()
        code = await proc.wait()
        if p.stopping:
            p.state = "stopped"
            return
        self._crashed(p, f"exited with code {code}")

    def _crashed(self, p: Proc, why: str) -> None:
        now = time.time()
        p.crashes.append(now)
        p.restarts += 1
        if len(p.crashes) >= CRASHLOOP_COUNT and now - p.crashes[0] < CRASHLOOP_WINDOW:
            p.state = "crashloop"
            self.db.audit("system", "server.crashloop", server_id=p.server_id, detail=why)
            log.error("server %s is crash-looping (%s) – left stopped", p.server_id, why)
        else:
            p.state = "crashed"
            p.next_start = now + BACKOFF[min(len(p.crashes) - 1, len(BACKOFF) - 1)]
            log.warning("server %s %s – restart in %ss", p.server_id, why, int(p.next_start - now))
        self._write_health(p.server_id, error=why)

    async def stop(self, sid: int) -> None:
        p = self.procs.get(sid)
        if p is None or p.process is None or p.process.returncode is not None:
            if p is not None:
                p.state = "stopped"
            return
        p.stopping = True
        try:
            os.killpg(p.process.pid, signal.SIGTERM) if sys.platform != "win32" else p.process.terminate()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(p.process.wait(), STOP_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                os.killpg(p.process.pid, signal.SIGKILL) if sys.platform != "win32" else p.process.kill()
            except ProcessLookupError:
                pass
        p.state = "stopped"

    async def restart(self, sid: int) -> None:
        await self.stop(sid)
        p = self.procs.setdefault(sid, Proc(sid))
        p.crashes.clear()
        p.state, p.next_start = "stopped", 0           # also clears a crashloop (owner pressed "restart")
        await self.reconcile()

    async def rolling_restart(self, gap: float = SHARED_START_GAP) -> list[int]:
        """After an update: every running server restarts one after another (new code), never all at once."""
        done = []
        for sid, p in list(self.procs.items()):
            if p.state == "running":
                await self.stop(sid)
                p.state, p.next_start = "stopped", 0
                await self.start(sid)
                done.append(sid)
                await asyncio.sleep(gap)
        return done

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(sid) for sid in list(self.procs)))

    # ------------------------------------------------------------ Health rows (the owner panel reads these)

    def _write_health(self, only: int | None = None, error: str | None = None) -> None:
        for sid, p in self.procs.items():
            if only is not None and sid != only:
                continue
            self.db.run("INSERT INTO health (server_id, checked_at, process, pid, restarts, error) VALUES "
                        "(?, ?, ?, ?, ?, ?) ON CONFLICT(server_id) DO UPDATE SET process = excluded.process, "
                        "pid = excluded.pid, restarts = excluded.restarts, "
                        "error = COALESCE(excluded.error, health.error)",
                        (sid, int(time.time()), p.state, p.process.pid if p.process else None, p.restarts, error))
