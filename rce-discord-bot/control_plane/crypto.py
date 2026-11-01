"""Secrets at rest: AES-256-GCM with a master key that is NOT in the database.

* Master keys come from CP_MASTER_KEYS ("1:<base64 32 bytes>,2:<…>") or the file CP_MASTER_KEY_FILE – keep it
  outside the data folder and out of backups of the database. The highest version encrypts; older versions only
  decrypt (key rotation: add a new version, run `python -m control_plane rotate-keys`, then drop the old one).
* Every ciphertext is bound to its row with associated data ("server:<id>:discord_token") – a ciphertext copied into
  another tenant's row fails to decrypt instead of leaking.
* Fingerprints (HMAC-SHA256 with a key derived from the master key) let the database enforce "one bot token per
  server" and find duplicates without storing the token in a searchable form.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_ENV = "CP_MASTER_KEYS"
KEY_FILE_ENV = "CP_MASTER_KEY_FILE"


class CryptoError(Exception):
    pass


def new_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def parse_keys(text: str) -> dict[int, bytes]:
    keys: dict[int, bytes] = {}
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        version, _, b64 = part.partition(":")
        key = base64.b64decode(b64.strip())
        if len(key) != 32 or not version.strip().isdigit():
            raise CryptoError("Each master key must be 'version:<base64 of 32 bytes>'.")
        keys[int(version)] = key
    return keys


class SecretBox:
    def __init__(self, keys: dict[int, bytes]) -> None:
        if not keys:
            raise CryptoError("No master key configured (CP_MASTER_KEYS or CP_MASTER_KEY_FILE).")
        self.keys = keys
        self.version = max(keys)
        self._fp_key = hmac.new(keys[min(keys)], b"fingerprint", hashlib.sha256).digest()   # stable across rotation

    @classmethod
    def from_env(cls) -> "SecretBox":
        text = os.environ.get(KEYS_ENV, "")
        if not text and os.environ.get(KEY_FILE_ENV):
            text = Path(os.environ[KEY_FILE_ENV]).read_text(encoding="utf-8")
        return cls(parse_keys(text))

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self.keys[self.version]).encrypt(nonce, plaintext.encode(), aad.encode())
        return f"v{self.version}:" + base64.b64encode(nonce + ct).decode()

    def decrypt(self, blob: str, aad: str) -> str:
        try:
            head, _, b64 = blob.partition(":")
            key = self.keys[int(head[1:])]
            raw = base64.b64decode(b64)
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
        except Exception as exc:                          # wrong key, wrong row (aad), tampered data
            raise CryptoError("Can't decrypt this secret (wrong key, moved or tampered).") from exc

    def needs_rotation(self, blob: str) -> bool:
        return not blob.startswith(f"v{self.version}:")

    def fingerprint(self, value: str) -> str:
        return hmac.new(self._fp_key, value.strip().encode(), hashlib.sha256).hexdigest()
