"""Bot updates – ONLY from the owner panel (or the CLI). Tenants can't update themselves: their auto-updater is off
and the update API is blocked in hosted mode. Every tenant runs the same code folder, so one update installs the new
version for everyone; the running servers then restart one after another (never all at once).

The control plane itself runs from the same folder – restart its service afterwards (systemd: Restart=always) so it
uses the new code too; the panel says so.
"""
from __future__ import annotations

import asyncio
import json
import time

from control_plane.db import DB
from control_plane.settings import Settings


def _updater(settings: Settings):
    from updater import Updater
    return Updater(settings.update_repo, settings.update_branch, base_dir=settings.bot_dir)


def status(db: DB) -> dict:
    row = db.one("SELECT value FROM settings_kv WHERE key = 'update'")
    return json.loads(row["value"]) if row else {}


def _save(db: DB, data: dict) -> None:
    db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('update', ?)", (json.dumps(data),))


async def check(db: DB, settings: Settings, updater=None) -> dict:
    """Is there a newer version? (Doesn't install anything.)"""
    up = updater or _updater(settings)
    current = await asyncio.to_thread(up.current_version)
    try:
        sha, title = await asyncio.to_thread(up.latest_commit)
        latest = {"sha": sha, "title": title}
    except Exception as exc:
        latest = {"error": str(exc)[:200]}
    data = {**status(db), "current": current, "latest": latest, "checked_at": int(time.time()),
            "available": bool(latest.get("sha")) and latest.get("sha") != (current or {}).get("sha")}
    _save(db, data)
    return data


async def install(db: DB, settings: Settings, supervisor, *, owner: str, updater=None) -> dict:
    """Installs the newest version for every tenant, then restarts the running servers one by one."""
    up = updater or _updater(settings)
    result = await asyncio.to_thread(up.check)
    restarted = []
    if result.status == "updated" and supervisor is not None:
        restarted = await supervisor.rolling_restart()
    data = {**status(db), "last_install": {"at": int(time.time()), "by": owner, "status": result.status,
                                           "message": result.message, "sha": result.sha, "title": result.title,
                                           "restarted": len(restarted)},
            "control_plane_restart_needed": result.status == "updated"}
    _save(db, data)
    db.audit(f"owner:{owner}", "update.install", detail=f"{result.status} {result.sha or ''} {result.message}"[:500])
    return data
