"""Encrypted databases (DB_ENCRYPTION): every database file of the bot is encrypted on disk with SQLCipher
(AES-256, also its WAL journal). Without the key a copy of the file – from the bot folder, a backup ZIP, the Google
Drive copy or a stolen disk – is unreadable.

The key is NOT in the bot folder (so it's never in a backup or a copy of the folder):
  * DB_KEY environment variable (64 hex characters) – wins if set (e.g. after moving to a new PC)
  * Windows: %APPDATA%\\RCE-Discord-Bot\\db.key, locked to your Windows account (DPAPI) – useless on another PC/user
  * Linux/macOS: ~/.config/rce-discord-bot/db.key (only your user may read it)
Write the RECOVERY KEY down (panel: Settings → Alerts & backups → Database encryption): without it, a new PC or a
reinstalled Windows can't open the databases or the backups any more.

What it can't do: someone who controls the PC while the bot runs, as your user, can read the key like the bot does.
Turning it on converts the existing databases (encrypted copy, checked, then swapped in); turning it off converts
them back.
"""
from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger("rce.dbcrypt")

HEADER = b"SQLite format 3\x00"
KEY_ENV = "DB_KEY"
KEY_FILE_ENV = "DB_KEY_FILE"
_KEY: str | None = None              # the active key (hex) – None = databases are plain SQLite

try:
    import sqlcipher3.dbapi2 as _sc          # pip install sqlcipher3
except ImportError:                           # pragma: no cover – only needed when encryption is on
    _sc = None

ERRORS: tuple = (sqlite3.Error,) + ((_sc.Error,) if _sc is not None else ())


class KeyError_(Exception):
    """The key is missing/wrong or SQLCipher isn't installed – a readable message."""


def available() -> bool:
    return _sc is not None


def active() -> bool:
    return _KEY is not None


def ensure_installed() -> bool:
    """Installs sqlcipher3 if it's missing (e.g. switched on right after an automatic update)."""
    global _sc, ERRORS
    if _sc is not None:
        return True
    import subprocess
    log.warning("Installing sqlcipher3 for the database encryption …")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "sqlcipher3"],
                       check=True, timeout=300)
        import sqlcipher3.dbapi2 as sc
    except Exception as exc:
        log.error("Installing sqlcipher3 failed: %s", exc)
        return False
    _sc = sc
    ERRORS = (sqlite3.Error, sc.Error)
    return True


def enable(key_hex: str | None) -> None:
    global _KEY
    if key_hex is not None:
        if _sc is None and not ensure_installed():
            raise KeyError_("Database encryption needs the sqlcipher3 package (pip install sqlcipher3).")
        bytes.fromhex(key_hex)
    _KEY = key_hex


# ------------------------------------------------------------ Where the key lives

def key_file() -> Path:
    if os.environ.get(KEY_FILE_ENV):
        return Path(os.environ[KEY_FILE_ENV])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "RCE-Discord-Bot" / "db.key"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "rce-discord-bot" / "db.key"


def _dpapi(data: bytes, protect: bool) -> bytes:              # pragma: no cover – Windows only
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):   # UI_FORBIDDEN
        raise KeyError_("Windows couldn't unlock the database key (another Windows user or PC?) – set DB_KEY to "
                        "your recovery key.")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def load_key() -> str | None:
    """The key (hex) from DB_KEY or the key file, None if there is none yet."""
    env = (os.environ.get(KEY_ENV) or "").strip().lower()
    if env:
        if len(env) != 64 or any(c not in "0123456789abcdef" for c in env):
            raise KeyError_("DB_KEY must be the 64-character recovery key.")
        return env
    path = key_file()
    if not path.exists():
        return None
    return _read_file_key(path)


def _read_file_key(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KeyError_(f"Can't read the database key file {path}: {exc}") from exc
    if raw.startswith(b"dpapi:"):
        raw = _dpapi(raw[6:], protect=False)
    key = raw.decode("ascii", "ignore").strip().lower()
    if len(key) != 64:
        raise KeyError_(f"The database key file {path} is damaged – set DB_KEY to your recovery key.")
    return key


def load_or_create_key() -> str:
    key = load_key()
    if key is not None:
        return key
    key = secrets.token_hex(32)
    _write_key(key)
    log.warning("Database encryption: a new key was created in %s – save the recovery key from the panel!", key_file())
    return key


def _write_key(key: str) -> None:
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = key.encode()
    if sys.platform == "win32":                           # pragma: no cover
        data = b"dpapi:" + _dpapi(data, protect=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def adopt_env_key() -> str:
    """The key for this start. A recovery key in DB_KEY (e.g. on a new PC) is saved to this PC's key file, so it
    can be removed from the .env again – the key shouldn't stay in the bot folder."""
    env = (os.environ.get(KEY_ENV) or "").strip().lower()
    if os.environ.get("RCE_SAAS") == "1":                  # hosted tenant: its own key, only in its environment
        if not env:
            raise KeyError_("Hosted mode needs DB_KEY from the control plane.")
        return load_key()
    if env:
        key = load_key()                                   # checks the format
        path = key_file()
        try:
            saved = None if not path.exists() else _read_file_key(path)
        except KeyError_:
            saved = None
        if saved != key:
            _write_key(key)
            log.warning("Database key from DB_KEY saved to %s – remove DB_KEY from the .env now.", path)
        return key
    return load_or_create_key()


# ------------------------------------------------------------ Connecting and converting files

def connect(path, **kwargs):
    """sqlite3.connect – or an SQLCipher connection with the active key. Raises KeyError_ on a wrong key."""
    if _KEY is None:
        return sqlite3.connect(path, **kwargs)
    conn = _sc.connect(str(path), **kwargs)
    conn.execute(f"PRAGMA key = \"x'{_KEY}'\"")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except _sc.DatabaseError as exc:
        conn.close()
        raise KeyError_(f"{Path(path).name} can't be opened with this key – wrong key? (set DB_KEY to the recovery "
                        "key)") from exc
    return conn


def row_factory(conn):
    return _sc.Row if _sc is not None and isinstance(conn, _sc.Connection) else sqlite3.Row


def is_plain(path: Path) -> bool | None:
    """True = normal SQLite file, False = encrypted, None = missing/empty."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if not head:
        return None
    return head == HEADER


def _convert(path: Path, key: str, encrypt: bool) -> None:
    """Writes an encrypted (or plain) copy, checks it, then swaps it in. The original journal is merged first."""
    path = Path(path)
    tmp = path.with_name(path.name + ".converting")
    tmp.unlink(missing_ok=True)
    if encrypt:
        plain = sqlite3.connect(path)
        plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        plain.execute("PRAGMA journal_mode = DELETE")
        plain.close()
        src = _sc.connect(str(path))                       # a plain file opens without a key
        src.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{key}'\"", (str(tmp),))
        src.execute("SELECT sqlcipher_export('enc')")
        src.execute("DETACH DATABASE enc")
        src.close()
        check = _sc.connect(str(tmp))
        check.execute(f"PRAGMA key = \"x'{key}'\"")
    else:
        src = _sc.connect(str(path))
        src.execute(f"PRAGMA key = \"x'{key}'\"")
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src.execute("ATTACH DATABASE ? AS plain KEY ''", (str(tmp),))
        src.execute("SELECT sqlcipher_export('plain')")
        src.execute("DETACH DATABASE plain")
        src.close()
        check = sqlite3.connect(tmp)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise KeyError_(f"The converted copy of {path.name} failed its check – nothing was changed.")
    finally:
        check.close()
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)
    os.replace(tmp, path)


def ensure_state(path: Path) -> str:
    """Makes the file match the setting: encrypts a plain file when encryption is on, decrypts an encrypted one
    when it's off (needs the key). Returns "encrypted", "decrypted" or "" (nothing to do)."""
    state = is_plain(Path(path))
    if state is None:
        return ""
    if _KEY is not None and state:
        _convert(Path(path), _KEY, encrypt=True)
        log.warning("Database encrypted: %s", Path(path).name)
        return "encrypted"
    if _KEY is None and state is False:
        key = load_key()
        if key is None or _sc is None:
            raise KeyError_(f"{Path(path).name} is encrypted but no key was found – turn DB_ENCRYPTION back on, or "
                            "set DB_KEY to the recovery key.")
        _convert(Path(path), key, encrypt=False)
        log.warning("Database decrypted (encryption turned off): %s", Path(path).name)
        return "decrypted"
    return ""


def backup_copy(src_path: Path, dest_path: Path) -> None:
    """A consistent copy of a database – encrypted with the same key if encryption is on (a file that isn't
    converted yet is copied as it is)."""
    plain = is_plain(Path(src_path)) is not False
    src = sqlite3.connect(src_path) if plain else connect(src_path)
    dest = sqlite3.connect(dest_path) if plain else connect(dest_path)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
