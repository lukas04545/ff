"""Full backups: the WHOLE bot folder – code, every database (also the extra servers'), settings (.env), plugins,
uploads – as one ZIP in data/full_backups/. Made every week (and by hand); each is deleted after a week, but the
newest is always kept, so there's always one to go back to.

Emergency restore ("🚨 Emergency restore" in the panel, /emergency-restore in Discord, or `python full_backup.py
restore` when the bot doesn't start at all): first a safety backup of the current state is made ("pre-restore"),
then the bot stops, the backup is put back in place and the bot starts again. The restore itself runs at the very
start of bot.py, before any of the bot's own modules are imported – so it works even if the code is broken.

Standard library only (on purpose).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FOLDER = "data/full_backups"
REQUEST = "data/restore_request.json"
NAME = re.compile(r"^full-\d{4}-\d{2}-\d{2}_\d{6}-[a-z-]+\.zip$")
KEEP_SECONDS = 7 * 86400
EVERY_SECONDS = 7 * 86400
# Not in a full backup: other backups, the Python environment, git, caches, the tunnel program, logs
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", "node_modules", "bin", "logs"}
SKIP_PATHS = ("data/full_backups", "data/backups")
SKIP_SUFFIXES = (".part", ".pyc", "-wal", "-shm", "-journal")


def folder(root: Path | None = None) -> Path:
    return (root or ROOT) / FOLDER


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]) or rel.endswith(SKIP_SUFFIXES):
        return True
    if any(rel == p or rel.startswith(p + "/") for p in SKIP_PATHS):
        return True
    if parts[0] == "servers" and len(parts) > 1 and (parts[1].startswith("_removed-")
                                                      or "/data/backups/" in rel + "/"):
        return True
    return rel == REQUEST


def _db_copy(path: Path, copy: Path) -> None:
    """A consistent copy of a database – through dbcrypt (encrypted databases stay encrypted) when the bot runs."""
    try:
        import dbcrypt
    except ImportError:                                    # stand-alone without the bot's modules
        dbcrypt = None
    if dbcrypt is not None and dbcrypt.active():
        dbcrypt.backup_copy(path, copy)
        return
    src, dest = sqlite3.connect(path), sqlite3.connect(copy)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()


def make(label: str = "weekly", root: Path | None = None) -> Path:
    """Writes a full backup ZIP and returns its path. Databases are copied consistently (SQLite backup)."""
    root = root or ROOT
    target_dir = folder(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    clean = re.sub(r"[^a-z-]", "", label.lower()) or "manual"
    target = target_dir / f"full-{stamp}-{clean}.zip"
    n = 1
    while target.exists():                                    # two in the same second
        n += 1
        target = target_dir / f"full-{stamp}-{clean}{'-' * n}.zip"
    partial = target.with_name(target.name + ".part")
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("FULL_BACKUP.txt", f"RCE Discord bot – full backup ({label}) of {root.name}, "
                                       f"{time.strftime('%Y-%m-%d %H:%M:%S')}.\nRestore: python full_backup.py "
                                       "restore <this file>\n")
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _skip(rel):
                continue
            if path.suffix == ".db":
                copy = Path(tmp) / f"{len(zf.namelist())}.db"
                try:
                    _db_copy(path, copy)                   # consistent even while the bot writes
                    zf.write(copy, rel)
                    continue
                except Exception:                          # not a database after all (or locked): copy the file
                    pass
            try:
                zf.write(path, rel)
            except OSError:
                pass                                       # locked/vanished file: skip it
    partial.replace(target)
    return target


def list_backups(root: Path | None = None) -> list[dict]:
    d = folder(root)
    if not d.is_dir():
        return []
    out = [{"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1), "created": int(p.stat().st_mtime)}
           for p in d.iterdir() if NAME.match(p.name)]
    return sorted(out, key=lambda b: (b["created"], b["name"]), reverse=True)        # newest first


def prune(root: Path | None = None, keep_seconds: int = KEEP_SECONDS) -> list[str]:
    """Deletes full backups older than a week – the newest one always stays."""
    removed = []
    cutoff = time.time() - keep_seconds
    for item in list_backups(root)[1:]:
        if item["created"] < cutoff:
            try:
                (folder(root) / item["name"]).unlink()
                removed.append(item["name"])
            except OSError:
                pass
    return removed


def path_of(name: str, root: Path | None = None) -> Path | None:
    if not NAME.match(name or ""):
        return None
    p = folder(root) / name
    return p if p.is_file() else None


def due(last: float, now: float | None = None) -> bool:
    return (now or time.time()) - (last or 0) >= EVERY_SECONDS - 3600


# ------------------------------------------------------------ Restore

def request_restore(name: str, root: Path | None = None) -> None:
    """Marks a backup to be restored at the next start (bot.py restores it before loading anything)."""
    root = root or ROOT
    if path_of(name, root) is None:
        raise ValueError("No such full backup.")
    (root / REQUEST).parent.mkdir(parents=True, exist_ok=True)
    (root / REQUEST).write_text(json.dumps({"name": name, "at": int(time.time())}), encoding="utf-8")


def restore(zip_path: Path, root: Path | None = None) -> int:
    """Puts every file of the backup back in place. Returns the number of files."""
    root = (root or ROOT).resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename == "FULL_BACKUP.txt":
                continue
            target = (root / info.filename).resolve()
            if root not in target.parents:                 # no path tricks
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            for side in ("-wal", "-shm"):                  # old journal files would mix with the restored db
                Path(str(target) + side).unlink(missing_ok=True)
            tmp = target.with_name(target.name + ".restoring")
            with zf.open(info) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            count += 1
    return count


def run_pending(root: Path | None = None) -> bool:
    """Called first thing by bot.py: restores a requested backup, then starts bot.py again. False = nothing to do."""
    root = root or ROOT
    req = root / REQUEST
    if not req.exists():
        return False
    try:
        name = json.loads(req.read_text(encoding="utf-8")).get("name", "")
    except (OSError, json.JSONDecodeError):
        name = ""
    req.unlink(missing_ok=True)                            # never loops, even if the restore fails
    path = path_of(name, root)
    if path is None:
        print(f"[restore] Backup {name!r} not found – starting normally.", flush=True)
        return False
    print(f"[restore] Restoring {name} …", flush=True)
    try:
        count = restore(path, root)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"[restore] FAILED: {exc} – starting with what's there.", flush=True)
        return False
    print(f"[restore] {count} files restored from {name}. Starting the bot …", flush=True)
    return True


def main(argv: list[str]) -> int:
    """python full_backup.py [list | make | restore [name]] – restore without the bot (stop the bot first!)."""
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "make":
        print("Made", make("manual").name)
        return 0
    items = list_backups()
    if cmd == "restore":
        name = argv[2] if len(argv) > 2 else (items[0]["name"] if items else "")
        path = path_of(name) if NAME.match(name) else (Path(name) if name and Path(name).is_file() else None)
        if path is None:
            print("No full backup found." if not items else f"Unknown backup {name!r}.")
            return 1
        print("Safety backup of the current state:", make("pre-restore").name)
        print(f"Restored {restore(path)} files from {path.name}. Start the bot again.")
        return 0
    for b in items:
        print(f"{b['name']}  {b['size_mb']} MB")
    if not items:
        print("No full backups yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
