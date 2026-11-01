"""Weekly full backups of the whole bot (code + every database + settings) and the emergency restore."""
import asyncio
import os
import sqlite3
import time
import zipfile
from dataclasses import replace

import pytest

import full_backup
from tests.test_web import H, bot, run  # noqa: F401


@pytest.fixture
def root(tmp_path):
    (tmp_path / "cogs").mkdir()
    (tmp_path / "cogs" / "kits.py").write_text("VERSION = 1\n")
    (tmp_path / "bot.py").write_text("print('bot')\n")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=abc\n")
    (tmp_path / "data").mkdir()
    db = sqlite3.connect(tmp_path / "data" / "bot.db")
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()
    db.close()
    (tmp_path / "servers" / "two" / "data").mkdir(parents=True)
    (tmp_path / "servers" / "two" / "data" / "bot.db").write_bytes(b"")
    for skip in (".venv/lib/x.py", "__pycache__/a.pyc", "bin/cloudflared", "data/backups/backup-old.zip",
                 "servers/_removed-old-1/data/bot.db", ".git/HEAD"):
        (tmp_path / skip).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / skip).write_text("skip")
    return tmp_path


def test_full_backup_has_everything_but_the_junk(root):
    path = full_backup.make("weekly", root)
    names = set(zipfile.ZipFile(path).namelist())
    assert {"cogs/kits.py", "bot.py", ".env", "data/bot.db", "servers/two/data/bot.db"} <= names
    assert not any(n.startswith((".venv/", "__pycache__/", "bin/", "data/backups/", "data/full_backups/",
                                 "servers/_removed-", ".git/")) for n in names)
    copy = root / "copy.db"
    copy.write_bytes(zipfile.ZipFile(path).read("data/bot.db"))
    assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchall() == [(1,)]     # a real, consistent database
    assert full_backup.list_backups(root)[0]["name"] == path.name


def test_a_week_old_backup_is_deleted_but_the_newest_stays(root):
    old = full_backup.make("weekly", root)
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    assert full_backup.prune(root) == []                                              # it's the only one: stays
    new = full_backup.make("weekly", root)
    assert full_backup.prune(root) == [old.name] and full_backup.list_backups(root)[0]["name"] == new.name
    assert full_backup.due(time.time() - 7 * 86400) and not full_backup.due(time.time() - 86400)


def test_emergency_restore_at_start_puts_everything_back(root):
    good = full_backup.make("weekly", root)
    (root / "cogs" / "kits.py").write_text("this is broken(\n")                        # broken code …
    (root / "data" / "bot.db").write_bytes(b"garbage")                                 # … and a ruined database
    with pytest.raises(ValueError):
        full_backup.request_restore("full-nope.zip", root)
    full_backup.request_restore(good.name, root)
    assert full_backup.run_pending(root) is True
    assert (root / "cogs" / "kits.py").read_text() == "VERSION = 1\n"
    assert sqlite3.connect(root / "data" / "bot.db").execute("SELECT x FROM t").fetchall() == [(1,)]
    assert full_backup.run_pending(root) is False                                     # request used up – no loop
    evil = root / "data" / "full_backups" / "full-2020-01-01_000000-evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../outside.txt", "x")
    full_backup.restore(evil, root)
    assert not (root.parent / "outside.txt").exists()                                 # no path tricks


def test_panel_needs_restore_typed_and_saves_the_state_first(bot, monkeypatch, tmp_path):  # noqa: F811
    from cogs.full_backups import FullBackups
    monkeypatch.setattr(full_backup, "ROOT", tmp_path)
    (tmp_path / "cogs").mkdir()
    (tmp_path / "cogs" / "x.py").write_text("A = 1\n")

    async def scenario(c):
        cog = FullBackups(bot)
        await bot.add_cog(cog)
        cog.weekly_loop.cancel()
        closed = []
        monkeypatch.setattr(bot, "close", lambda: closed.append(1) or asyncio.sleep(0))
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await c.post("/api/backups/full", headers=H)
        name = (await r.json())["name"]
        listing = await (await c.get("/api/backups/full")).json()
        assert listing["main"] and listing["backups"][0]["name"] == name
        r = await c.post(f"/api/backups/full/{name}/restore", json={"confirm": "yes"}, headers=H)
        assert r.status == 400 and not (tmp_path / full_backup.REQUEST).exists()
        r = await c.post(f"/api/backups/full/{name}/restore", json={"confirm": "RESTORE"}, headers=H)
        body = await r.json()
        assert r.status == 200 and "pre-restore" in body["safety"] and bot.update_restart is True
        assert (tmp_path / full_backup.REQUEST).exists()
        await asyncio.sleep(1.2)
        assert closed == [1]
    run(bot, scenario)
