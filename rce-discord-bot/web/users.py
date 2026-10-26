"""Web interface accounts (other admins log in with username + password).

* The built-in account "owner" uses WEB_PASSWORD from the .env (and the one-time login link).
* Further accounts live in data/web_users.json – passwords only as salted PBKDF2-HMAC-SHA256 hashes
  (600 000 rounds, like OWASP recommends). Older scrypt hashes still work and are upgraded at the next login.
* Roles: "owner" = everything, "admin" = everything except settings, bot restart and user management.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger("rce.web.users")

OWNER = "owner"                       # built-in account (WEB_PASSWORD)
ROLES = ("admin", "owner")
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,32}")
MIN_PASSWORD_LENGTH = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}      # older hashes (before PBKDF2-SHA256)
PBKDF2_ROUNDS = 600_000
SCHEME = "pbkdf2_sha256"


def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    """Salted PBKDF2-HMAC-SHA256 – SHA-256 run many times, so stolen hashes can't be brute-forced quickly."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"{SCHEME}${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == SCHEME and len(parts) == 4:
            rounds = int(parts[1])
            if not 1000 <= rounds <= 10_000_000:
                return False
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(parts[2]), rounds)
            expected = parts[3]
        elif parts[0] == "scrypt" and len(parts) == 3:
            digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(parts[1]), dklen=32, **_SCRYPT)
            expected = parts[2]
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def needs_rehash(stored: str) -> bool:
    parts = stored.split("$")
    return parts[0] != SCHEME or len(parts) != 4 or parts[1] != str(PBKDF2_ROUNDS)


def is_hashed(value: str) -> bool:
    """A stored password hash (hash_password) – not a plain password."""
    parts = str(value or "").split("$")
    return (parts[0] == SCHEME and len(parts) == 4) or (parts[0] == "scrypt" and len(parts) == 3)


def same_secret(given: str, expected: str) -> bool:
    """Constant-time comparison of two secrets via their SHA-256 (also hides the length)."""
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # same work for unknown users (no timing leak)


class UserError(Exception):
    """Message for the web interface."""


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._users: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------ File

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            users = data.get("users", {}) if isinstance(data, dict) else {}
            self._users = {k.lower(): v for k, v in users.items() if isinstance(v, dict) and v.get("hash")}
        except FileNotFoundError:
            self._users = {}
        except (OSError, ValueError) as exc:
            log.error("Could not read %s (%s) – only the owner login works.", self.path, exc)
            self._users = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": self._users}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never a half-written file

    # ------------------------------------------------------------ Queries

    def get(self, username: str) -> dict | None:
        return self._users.get(username.lower())

    def list(self) -> list[dict]:
        return [{"username": u["username"], "role": u["role"], "disabled": bool(u.get("disabled")),
                 "created_at": u.get("created_at"), "created_by": u.get("created_by"),
                 "last_login": u.get("last_login")}
                for u in sorted(self._users.values(), key=lambda u: u["username"].lower())]

    def session_key(self, username: str) -> str | None:
        """Part of the session signature: changes with the password -> old sessions end."""
        user = self.get(username)
        if user is None or user.get("disabled"):
            return None
        return user["hash"]

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.get(username)
        if user is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, user["hash"]) or user.get("disabled"):
            return None
        if needs_rehash(user["hash"]):         # older scrypt hash -> PBKDF2-SHA256
            user["hash"] = hash_password(password)
        user["last_login"] = int(time.time())
        self._save()
        return user

    # ------------------------------------------------------------ Changes

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise UserError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")

    def create(self, username: str, password: str, role: str, created_by: str) -> dict:
        username = username.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise UserError("Username: 3–32 characters, only letters, digits, _ . -")
        if username.lower() == OWNER:
            raise UserError('"owner" is the built-in account (WEB_PASSWORD).')
        if self.get(username):
            raise UserError("This username already exists.")
        if role not in ROLES:
            raise UserError("Role must be admin or owner.")
        self._check_password(password)
        user = {"username": username, "hash": hash_password(password), "role": role,
                "created_at": int(time.time()), "created_by": created_by}
        self._users[username.lower()] = user
        self._save()
        return user

    def update(self, username: str, *, role: str | None = None, password: str | None = None,
               disabled: bool | None = None) -> dict:
        user = self.get(username)
        if user is None:
            raise UserError("User not found.")
        if role is not None:
            if role not in ROLES:
                raise UserError("Role must be admin or owner.")
            user["role"] = role
        if password is not None:
            self._check_password(password)
            user["hash"] = hash_password(password)
        if disabled is not None:
            user["disabled"] = bool(disabled)
        self._save()
        return user

    def delete(self, username: str) -> bool:
        if self._users.pop(username.lower(), None) is None:
            return False
        self._save()
        return True
