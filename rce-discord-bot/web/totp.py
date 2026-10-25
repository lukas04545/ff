"""Two-factor login for the control panel: time-based one-time codes (TOTP, RFC 6238) – the 6-digit codes of
Google Authenticator, Microsoft Authenticator, Authy, 1Password, Bitwarden …

Secrets are stored per account in data/web_2fa.json (next to web_secret.key, readable only by the bot's user where
the OS allows it). A code is valid for its 30-second step and one step before/after (clock drift); each code works
only once (replays are refused).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret() -> str:
    """A random 160-bit secret, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def matching_step(secret: str, code: str, now: float | None = None, window: int = 1) -> int | None:
    """The time step the code belongs to (now ± window), else None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = int((time.time() if now is None else now) // STEP)
    for counter in range(step - window, step + window + 1):
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    """The link authenticator apps understand (tap it on a phone, or type the secret in by hand)."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


class TwoFactorStore:
    """username (lower case) -> secret; plus the last used step per account (no code twice)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._secrets = {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._secrets = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._secrets, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def enabled(self, username: str) -> bool:
        return username.lower() in self._secrets

    def enable(self, username: str, secret: str) -> None:
        with self._lock:
            self._secrets[username.lower()] = secret
            self._save()

    def disable(self, username: str) -> None:
        with self._lock:
            if self._secrets.pop(username.lower(), None) is not None:
                self._save()

    def verify(self, username: str, code: str, now: float | None = None) -> bool:
        """Checks a login code – each code only once."""
        secret = self._secrets.get(username.lower())
        if secret is None:
            return False
        step = matching_step(secret, code, now)
        with self._lock:
            if step is None or step <= self._used.get(username.lower(), -1):
                return False
            self._used[username.lower()] = step
        return True
