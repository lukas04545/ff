"""Backups copied to a folder (e.g. Google Drive) + live-backup.zip that is refreshed when something changes."""
import asyncio
import os
import sqlite3
import time
import zipfile
from dataclasses import replace

import backups


def make_base(tmp_path):
    base = tmp_path / "bot"
    (base / "data").mkdir(parents=True)
    db = base / "data" / "bot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    (base / "prices.txt").write_text("wood = 1 / 1000\n", encoding="utf-8")
    (base / ".env").write_text("DISCORD_TOKEN=secret\n", encoding="utf-8")
    return base, db


def test_copy_live_env_and_prune(tmp_path):
    base, db = make_base(tmp_path)
    drive = tmp_path / "My Drive" / "RCE bot"
    zipped = backups.make_backup(db, base=base, label="daily")
    names = zipfile.ZipFile(zipped).namelist()
    assert "bot.db" in names and "prices.txt" in names and ".env" not in names     # no tokens by default
    with_env = backups.make_backup(db, base=base, label="manual", include_env=True)
    assert ".env" in zipfile.ZipFile(with_env).namelist()

    copied = backups.copy_to_folder(zipped, drive)
    assert copied.read_bytes() == zipped.read_bytes() and not list(drive.glob("*.part"))
    live = backups.write_live(db, drive, base=base)
    assert live.name == "live-backup.zip"
    restored = tmp_path / "restored.db"
    restored.write_bytes(zipfile.ZipFile(live).read("bot.db"))
    assert sqlite3.connect(restored).execute("SELECT x FROM t").fetchone() == (1,)   # a consistent copy

    old = [drive / f"backup-2020-01-0{i}_000000-daily.zip" for i in range(1, 5)]
    for p in old:
        p.write_bytes(b"x")
        os.utime(p, (time.time() - 30 * 86400,) * 2)
    removed = backups.prune_folder(drive, keep_days=7)
    assert removed == 2 and live.exists() and copied.exists()                    # newest 3 kept, live untouched
    assert sorted(p.name for p in drive.glob("backup-2020*")) == ["backup-2020-01-03_000000-daily.zip",
                                                                  "backup-2020-01-04_000000-daily.zip"]


def test_change_marker(tmp_path):
    base, db = make_base(tmp_path)
    m1 = backups.change_marker(db, base)
    assert backups.change_marker(db, base) == m1
    time.sleep(0.01)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO t VALUES (2)")
    con.commit()
    con.close()
    assert backups.change_marker(db, base) != m1


def test_monitor_live_backup_only_on_change(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
                     DATABASE_PATH=str(tmp_path / "bot.db")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.monitor import Monitor
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    drive = tmp_path / "drive"
    bot.config = replace(bot.config, backup_copy_dir=drive, backup_live_minutes=10)
    m = Monitor(bot)
    assert asyncio.run(m.live_backup()) is True and (drive / "live-backup.zip").exists()
    assert asyncio.run(m.live_backup()) is False                              # nothing changed -> no rewrite
    bot.db.set_value("x", "1")
    assert asyncio.run(m.live_backup()) is True                               # database changed
    assert m.backup_status()["live_last"] and m.backup_status()["copy_dir"] == str(drive)
    bot.config = replace(bot.config, backup_copy_dir=tmp_path / "file.txt")
    (tmp_path / "file.txt").write_text("not a folder")
    assert asyncio.run(m.live_backup(force=True)) is False and m.live_error  # error reported, no crash
    bot.db.close()
