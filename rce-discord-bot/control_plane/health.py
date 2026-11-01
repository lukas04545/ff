"""Health of every running tenant server, for the owner panel – read through each bot's own GET /saas/health
(Bearer token unique per server). The control plane never opens a tenant's database: it only learns what the
tenant's bot itself reports (Discord ready, guild, RCON connected, players online).
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from control_plane import provisioning
from control_plane.crypto import SecretBox
from control_plane.db import DB


async def check(session: aiohttp.ClientSession, db: DB, box: SecretBox, server_id: int) -> dict:
    s = db.one("SELECT web_port FROM servers WHERE id = ?", (server_id,))
    token = provisioning.get_secret(db, box, server_id, "health_token")
    try:
        async with session.get(f"http://127.0.0.1:{s['web_port']}/saas/health",
                               headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status != 200:
                return {"error": f"health HTTP {resp.status}"}
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        return {"error": f"unreachable: {type(exc).__name__}"}


def store(db: DB, server_id: int, data: dict) -> None:
    db.run("INSERT INTO health (server_id, checked_at, process) VALUES (?, ?, 'unknown') ON CONFLICT(server_id) DO "
           "UPDATE SET checked_at = excluded.checked_at", (server_id, int(time.time())))
    db.run("UPDATE health SET discord_ready = ?, bot_user = ?, guild_id = ?, guild_name = ?, rcon_connected = ?, "
           "players_online = ?, latency_ms = ?, setup_mode = ?, error = ? WHERE server_id = ?",
           (int(bool(data.get("discord_ready"))), data.get("bot_user"), data.get("guild_id"), data.get("guild_name"),
            int(bool(data.get("rcon_connected"))), data.get("players_online"), data.get("latency_ms"),
            int(bool(data.get("setup_mode"))), data.get("error"), server_id))


async def poll_once(db: DB, box: SecretBox, server_ids: list[int]) -> dict[int, dict]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        results = await asyncio.gather(*(check(session, db, box, sid) for sid in server_ids))
    out = dict(zip(server_ids, results))
    for sid, data in out.items():
        store(db, sid, data)
    return out


async def poll_forever(db: DB, box: SecretBox, supervisor, every: float = 30.0) -> None:
    while True:
        running = [sid for sid, p in supervisor.procs.items() if p.state == "running"]
        if running:
            try:
                await poll_once(db, box, running)
            except Exception:
                pass
        await asyncio.sleep(every)
