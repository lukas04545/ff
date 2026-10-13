"""Backups of everything the bot can't get back by itself: database, web logins, rules and knowledge base.

A backup is a ZIP in data/backups/ (daily and before every automatic update). The .env with the tokens is
NOT included unless BACKUP_INCLUDE_ENV is on – backups can be downloaded in the web interface.

Copy folder (BACKUP_COPY_DIR, e.g. a Google Drive for desktop folder like "G:\\My Drive\\RCE bot"): every backup ZIP is
copied there too, and a "live-backup.zip" is refreshed every BACKUP_LIVE_MINUTES when something changed. Files
are written next to the target first and then swapped in, so a sync tool never uploads a half-written file.

Restore: stop the bot, extract the ZIP into the bot folder (bot.db goes to data/bot.db), start the bot.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

log = logging.getLogger("rce.backups")

BASE_DIR = Path(__file__).resolve().parent
NAME = re.compile(r"^backup-\d{4}-\d{2}-\d{2}_\d{6}-[a-z-]+\.zip$")
EXTRA_FILES = ("data/web_users.json", "rules.md", "support_knowledge.md", "prices.txt")
LIVE_NAME = "live-backup.zip"
README = """RCE Discord bot – backup

Restore: stop the bot, put bot.db into the data/ folder (data/bot.db) and the other files into the bot folder,
then start the bot again. Your .env (tokens, passwords) is only included if BACKUP_INCLUDE_ENV was on.
"""


def backup_dir(base: Path | None = None) -> Path:
    return (base or BASE_DIR) / "data" / "backups"


def make_backup(db_path: Path, *, base: Path | None = None, label: str = "daily",
                include_env: bool = False) -> Path:
    """Creates a backup ZIP and returns its path. Uses its own DB connection (safe from any thread)."""
    base = base or BASE_DIR
    target_dir = backup_dir(base)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    target = target_dir / f"backup-{stamp}-{re.sub(r'[^a-z-]', '', label.lower()) or 'manual'}.zip"
    _write_zip(db_path, target, base=base, include_env=include_env)
    log.info("Backup saved: %s", target.name)
    return target


def _write_zip(db_path: Path, target: Path, *, base: Path, include_env: bool) -> None:
    db_path = db_path if db_path.is_absolute() else base / db_path
    extra = EXTRA_FILES + ((".env",) if include_env else ())
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "bot.db"
        if db_path.exists():
            src, dest = sqlite3.connect(db_path), sqlite3.connect(copy)
            try:
                src.backup(dest)       # consistent copy even while the bot writes
            finally:
                dest.close()
                src.close()
        partial = target.with_suffix(".zip.part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", README)
            if copy.exists():
                zf.write(copy, "bot.db")
            for rel in extra:
                if (base / rel).is_file():
                    zf.write(base / rel, rel)
        partial.replace(target)


def copy_to_folder(zip_path: Path, folder: Path) -> Path:
    """Copies a backup ZIP into the copy folder (temp name first, then renamed – sync-safe)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / zip_path.name
    partial = folder / (zip_path.name + ".part")
    shutil.copyfile(zip_path, partial)
    partial.replace(target)
    return target


def prune_folder(folder: Path, keep_days: int, min_keep: int = 3) -> int:
    """Old backup copies in the copy folder (only files this module names; live-backup.zip stays)."""
    if not folder.is_dir():
        return 0
    items = sorted((p for p in folder.iterdir() if NAME.match(p.name)), key=lambda p: p.name, reverse=True)
    cutoff, removed = time.time() - keep_days * 86400, 0
    for p in items[min_keep:]:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def write_live(db_path: Path, folder: Path, *, base: Path | None = None, include_env: bool = False) -> Path:
    """Refreshes live-backup.zip in the copy folder (Google Drive keeps older versions in its history)."""
    base = base or BASE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LIVE_NAME
    _write_zip(db_path, target, base=base, include_env=include_env)
    return target


def change_marker(db_path: Path, base: Path | None = None) -> tuple:
    """Changes when the database or one of the backed-up files changed (cheap: sizes and modification times)."""
    base = base or BASE_DIR
    db_path = db_path if db_path.is_absolute() else base / db_path
    marks = []
    for p in (db_path, db_path.with_name(db_path.name + "-wal"), *(base / rel for rel in EXTRA_FILES)):
        try:
            st = p.stat()
            marks.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            marks.append((p.name, None, None))
    return tuple(marks)


def list_backups(base: Path | None = None) -> list[dict]:
    folder = backup_dir(base)
    if not folder.is_dir():
        return []
    items = [{"name": p.name, "size": p.stat().st_size, "created": int(p.stat().st_mtime)}
             for p in folder.iterdir() if NAME.match(p.name)]
    return sorted(items, key=lambda b: b["name"], reverse=True)


def prune_backups(keep_days: int, *, base: Path | None = None, min_keep: int = 3) -> int:
    """Deletes backups older than keep_days, but always keeps the newest min_keep. Returns the number deleted."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for item in list_backups(base)[min_keep:]:
        if item["created"] < cutoff:
            try:
                (backup_dir(base) / item["name"]).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def backup_path(name: str, base: Path | None = None) -> Path | None:
    """Path of an existing backup by its file name (only names this module creates – no path tricks)."""
    if not NAME.match(name):
        return None
    path = backup_dir(base) / name
    return path if path.is_file() else None
