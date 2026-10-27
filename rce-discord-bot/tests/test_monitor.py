"""Backups (ZIP content, pruning, safe names), server-down alerts and the web endpoints for both."""
import asyncio
import os
import sqlite3
import time
import zipfile
from types import SimpleNamespace

import backups
from cogs.monitor import Monitor
from storage import Storage

H = {"X-Requested-With": "rce-web"}


def test_backup_zip_and_pruning(tmp_path):
    db = Storage(tmp_path / "data" / "bot.db")
    db.link_player(7, "GamerTag42")
    (tmp_path / "rules.md").write_text("No cheating\n")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=secret\n")
    path = backups.make_backup(tmp_path / "data" / "bot.db", base=tmp_path, label="daily")
    db.close()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"bot.db", "rules.md", "README.txt"} <= names and ".env" not in names
        zf.extract("bot.db", tmp_path / "restore")
    restored = sqlite3.connect(tmp_path / "restore" / "bot.db")
    assert restored.execute("SELECT player FROM linked_players").fetchone()[0] == "GamerTag42"
    restored.close()

    # Old backups go, the newest 3 always stay
    folder = backups.backup_dir(tmp_path)
    old = time.time() - 30 * 86400
    for i in range(5):
        p = folder / f"backup-2020-01-0{i + 1}_000000-daily.zip"
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    assert backups.prune_backups(7, base=tmp_path) == 3
    assert len(backups.list_backups(tmp_path)) == 3
    assert backups.backup_path("../../.env", tmp_path) is None and backups.backup_path(path.name, tmp_path) == path


def test_server_down_alert(monkeypatch):
    sent = []

    class Channel:
        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed.title))
    import discord
    monkeypatch.setattr(discord.abc, "Messageable", Channel)
    cfg = SimpleNamespace(server_down_alert_minutes=10, alert_channel_id=None, admin_log_channel_id=5,
                          alert_role_ids=frozenset({42}), backup_enabled=False)
    rcon = SimpleNamespace(connected=False, last_error="Connection failed: timeout")
    bot = SimpleNamespace(config=cfg, rcon=rcon, rcon_diagnosis=None, get_channel=lambda cid: Channel())
    m = Monitor(bot)
    m.down_since = time.time() - 300
    asyncio.run(m.watch_loop.coro(m))
    assert sent == []                                            # only 5 of 10 minutes
    m.down_since = time.time() - 700
    asyncio.run(m.watch_loop.coro(m))
    asyncio.run(m.watch_loop.coro(m))
    assert sent == [("<@&42>", "🔴 Server offline")]             # pinged once
    rcon.connected = True
    asyncio.run(m.watch_loop.coro(m))
    assert sent[-1] == (None, "🟢 Server back online") and m.down_since is None and not m.alerted


def test_web_backups_and_timed_roles(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"),
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md")).items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)                 # backups go into the server's folder (the working folder)
    from bot import RceBot
    from config import load_config
    from web.server import WebInterface

    async def scenario():
        bot = RceBot(load_config("/nonexistent"))
        bot.db.set_timed_role(1, 7, 55, int(time.time()) + 3600, "Giveaway prize", "ticket #1 (AI)")
        await bot.add_cog(Monitor(bot))
        web = WebInterface(bot, env_path=tmp_path / ".env", secret_path=tmp_path / "k")
        web.users.create("mod", "mod-pass-123", "admin", "owner")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await (await c.post("/api/backups", headers=H)).json()
            assert r["ok"] and r["name"].endswith("-manual.zip")
            listing = await (await c.get("/api/backups")).json()
            assert [b["name"] for b in listing["backups"]] == [r["name"]]
            dl = await c.get(f"/api/backups/{r['name']}")
            assert dl.status == 200 and (await dl.read())[:2] == b"PK"
            assert (await c.get("/api/backups/..%2F.env")).status == 404
            rows = (await (await c.get("/api/timed-roles")).json())["rows"]
            assert rows[0]["user_id"] == "7" and rows[0]["reason"] == "Giveaway prize"

            await c.post("/api/logout", headers=H)                 # admins: timed roles yes, backups no
            await c.post("/api/login", json={"username": "mod", "password": "mod-pass-123"}, headers=H)
            assert (await c.get("/api/timed-roles")).status == 200
            assert (await c.get("/api/backups")).status == 403
            assert (await c.get(f"/api/backups/{r['name']}")).status == 403
        finally:
            await c.close()
            await bot.rcon.close()
            bot.db.close()

    asyncio.run(scenario())
