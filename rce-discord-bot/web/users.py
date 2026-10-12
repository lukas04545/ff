"""Web interface accounts (other admins log in with username + password).

* The built-in account "owner" uses WEB_PASSWORD from the .env (and the one-time login link).
* Further accounts live in data/web_users.json – passwords only as scrypt hashes.
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
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), dklen=32, **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


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
