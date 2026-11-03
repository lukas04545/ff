"""Passkeys (WebAuthn): sign in with Face ID, a fingerprint, Windows Hello, a phone or a security key.

Used by the control panel (web/server.py) and the owner panel (control_plane/web.py). No extra packages: a small
CBOR reader and the `cryptography` package check the browser's answers.

What is checked
  * registration: clientDataJSON (type webauthn.create, our one-time challenge, our origin), authenticator data
    (hash of our RP ID, user present + user verified, attested credential), the public key (ES256, RS256 or
    Ed25519). Attestation isn't required ("none") – we trust the account that's logged in, not the device maker.
  * sign-in: clientDataJSON (webauthn.get, challenge, origin), RP ID hash, user present + verified, the signature
    over authenticatorData + SHA-256(clientDataJSON) with the stored key, and the signature counter (a counter that
    goes backwards means a cloned key: refused).
Challenges are random, single use and expire after CHALLENGE_TTL seconds. A passkey is bound to the address it was
made on (its RP ID = the host name): one made on panel.example.com doesn't work on another address.
Stored: credential id, public key, counter, a name, dates – no secret (the private key never leaves the device).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CHALLENGE_TTL = 300
MAX_PER_USER = 10
ALGORITHMS = (-7, -8, -257)          # ES256, EdDSA, RS256 (COSE ids) – what we accept, in order of preference
FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class PasskeyError(ValueError):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = str(text or "")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise PasskeyError("Bad encoding.") from exc


# ------------------------------------------------------------------ CBOR (just what WebAuthn uses)

def cbor_decode(data: bytes, pos: int = 0, depth: int = 0) -> tuple[Any, int]:
    """(value, next position). Integers, byte/text strings, arrays, maps, true/false/null."""
    if depth > 16 or pos >= len(data):
        raise PasskeyError("Bad CBOR.")
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise PasskeyError("Bad CBOR.")
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise PasskeyError("Unsupported CBOR.")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise PasskeyError("Bad CBOR.")
        raw = data[pos:pos + arg]
        return (bytes(raw) if major == 2 else raw.decode("utf-8")), pos + arg
    if major == 4:
        out = []
        for _ in range(arg):
            item, pos = cbor_decode(data, pos, depth + 1)
            out.append(item)
        return out, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            key, pos = cbor_decode(data, pos, depth + 1)
            out[key], pos = cbor_decode(data, pos, depth + 1)
        return out, pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise PasskeyError("Unsupported CBOR.")


# ------------------------------------------------------------------ Keys

def _public_key(cose: dict):
    alg, kty = cose.get(3), cose.get(1)
    if alg == -7 and kty == 2 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
            raise PasskeyError("Bad key.")
        return alg, ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                                   ec.SECP256R1()).public_key()
    if alg == -8 and kty == 1 and cose.get(-1) == 6 and isinstance(cose.get(-2), bytes):
        return alg, Ed25519PublicKey.from_public_bytes(cose[-2])
    if alg == -257 and kty == 3 and isinstance(cose.get(-1), bytes) and isinstance(cose.get(-2), bytes):
        n, e = int.from_bytes(cose[-1], "big"), int.from_bytes(cose[-2], "big")
        if n.bit_length() < 2048:
            raise PasskeyError("RSA key too short.")
        return alg, rsa.RSAPublicNumbers(e, n).public_key()
    raise PasskeyError("This kind of passkey isn't supported.")


def _verify_signature(cose_bytes: bytes, signature: bytes, signed: bytes) -> None:
    alg, key = _public_key(cbor_decode(cose_bytes)[0])
    try:
        if alg == -7:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == -8:
            key.verify(signature, signed)
        else:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise PasskeyError("The passkey's signature doesn't match.") from None


# ------------------------------------------------------------------ Checks

def _client_data(raw_b64: str, kind: str, challenge: bytes, origins: set[str]) -> bytes:
    raw = unb64u(raw_b64)
    try:
        data = json.loads(raw)
    except ValueError:
        raise PasskeyError("Bad client data.") from None
    if not isinstance(data, dict) or data.get("type") != kind:
        raise PasskeyError("Wrong kind of answer.")
    if not secrets.compare_digest(str(data.get("challenge", "")), b64u(challenge)):
        raise PasskeyError("The request expired – try again.")
    if data.get("origin") not in origins:
        raise PasskeyError("Wrong address – a passkey only works on the address it was made on.")
    if data.get("crossOrigin"):
        raise PasskeyError("Not allowed from another website.")
    return raw


def _auth_data(raw: bytes, rp_id: str) -> tuple[int, int, bytes]:
    """(flags, sign count, rest) after checking the RP ID hash and user presence + verification."""
    if len(raw) < 37:
        raise PasskeyError("Bad authenticator data.")
    if not secrets.compare_digest(raw[:32], hashlib.sha256(rp_id.encode()).digest()):
        raise PasskeyError("This passkey is for another address.")
    flags = raw[32]
    if not flags & FLAG_UP or not flags & FLAG_UV:
        raise PasskeyError("The device didn't confirm it's you (PIN, fingerprint or face).")
    return flags, struct.unpack(">I", raw[33:37])[0], raw[37:]


def registration_options(*, rp_id: str, rp_name: str, user_handle: bytes, username: str, challenge: bytes,
                         exclude: list[str]) -> dict:
    """navigator.credentials.create({publicKey}) – binary fields base64url (the page decodes them)."""
    return {"rp": {"id": rp_id, "name": rp_name[:64]},
            "user": {"id": b64u(user_handle), "name": username, "displayName": username},
            "challenge": b64u(challenge),
            "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in ALGORITHMS],
            "timeout": CHALLENGE_TTL * 1000, "attestation": "none",
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                       "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": cid} for cid in exclude]}


def authentication_options(*, rp_id: str, challenge: bytes) -> dict:
    """navigator.credentials.get({publicKey}) – no allow list: the device offers its passkeys for this address."""
    return {"challenge": b64u(challenge), "rpId": rp_id, "timeout": CHALLENGE_TTL * 1000,
            "userVerification": "required", "allowCredentials": []}


def verify_registration(answer: dict, *, challenge: bytes, origins: set[str], rp_id: str) -> dict:
    """The new passkey: {"id", "public_key" (COSE, base64url), "sign_count"}."""
    response = answer.get("response") if isinstance(answer, dict) else None
    if not isinstance(response, dict):
        raise PasskeyError("Bad answer.")
    _client_data(response.get("clientDataJSON", ""), "webauthn.create", challenge, origins)
    att, _ = cbor_decode(unb64u(response.get("attestationObject", "")))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise PasskeyError("Bad attestation.")
    flags, count, rest = _auth_data(att["authData"], rp_id)
    if not flags & FLAG_AT or len(rest) < 18:
        raise PasskeyError("No passkey in the answer.")
    id_len = int.from_bytes(rest[16:18], "big")
    cred_id = rest[18:18 + id_len]
    if not cred_id or len(cred_id) != id_len or id_len > 1023:
        raise PasskeyError("Bad passkey id.")
    cose_start = 18 + id_len
    cose, end = cbor_decode(rest, cose_start)
    if not isinstance(cose, dict):
        raise PasskeyError("Bad key.")
    _public_key(cose)                                         # a key we can use
    if answer.get("id") and answer["id"] != b64u(cred_id):
        raise PasskeyError("Passkey id mismatch.")
    return {"id": b64u(cred_id), "public_key": b64u(rest[cose_start:end]), "sign_count": count}


def verify_authentication(answer: dict, *, challenge: bytes, origins: set[str], rp_id: str, credential: dict,
                          user_handle: bytes | None = None) -> int:
    """Checks a sign-in with a stored passkey. Returns the new signature counter."""
    response = answer.get("response") if isinstance(answer, dict) else None
    if not isinstance(response, dict):
        raise PasskeyError("Bad answer.")
    client_raw = _client_data(response.get("clientDataJSON", ""), "webauthn.get", challenge, origins)
    auth_raw = unb64u(response.get("authenticatorData", ""))
    _flags, count, _rest = _auth_data(auth_raw, rp_id)
    handle = response.get("userHandle")
    if user_handle is not None and handle and not secrets.compare_digest(unb64u(handle), user_handle):
        raise PasskeyError("This passkey belongs to another account.")
    _verify_signature(unb64u(credential["public_key"]), unb64u(response.get("signature", "")),
                      auth_raw + hashlib.sha256(client_raw).digest())
    old = int(credential.get("sign_count") or 0)
    if (count or old) and count <= old:
        raise PasskeyError("This passkey looks copied (its counter went backwards) – it was refused.")
    return count


# ------------------------------------------------------------------ Challenges and storage

class Challenges:
    """One-time challenges in memory: id -> (challenge, purpose, username, expires)."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, str, float]] = {}
        self._lock = threading.Lock()

    def new(self, purpose: str, username: str = "") -> tuple[str, bytes]:
        now, cid, challenge = time.time(), secrets.token_urlsafe(18), secrets.token_bytes(32)
        with self._lock:
            for key in [k for k, v in self._items.items() if v[3] < now]:
                self._items.pop(key, None)
            if len(self._items) > 5000:                       # flood: drop the oldest
                for key in sorted(self._items, key=lambda k: self._items[k][3])[:1000]:
                    self._items.pop(key, None)
            self._items[cid] = (challenge, purpose, username, now + CHALLENGE_TTL)
        return cid, challenge

    def take(self, cid: str, purpose: str, username: str = "") -> bytes:
        with self._lock:
            item = self._items.pop(str(cid or ""), None)
        if item is None or item[1] != purpose or item[3] < time.time() or (username and item[2] != username):
            raise PasskeyError("The request expired – try again.")
        return item[0]


class PasskeyStore:
    """username (lower case) -> {"handle": base64url user handle, "keys": [credential, …]} – a JSON file, or
    load/save callables (the control plane keeps it in its database)."""

    def __init__(self, path: Path | None = None, *, load=None, save=None) -> None:
        self.path = Path(path) if path else None
        self._load, self._save_fn = load, save
        self._lock = threading.Lock()
        self._data = self._read()

    def _read(self) -> dict:
        try:
            if self._load is not None:
                raw = self._load()
            elif self.path is not None:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                raw = {}
        except (OSError, ValueError):
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _write(self) -> None:
        if self._save_fn is not None:
            self._save_fn(self._data)
            return
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def handle(self, username: str) -> bytes:
        """The account's stable WebAuthn user handle (random, made on first use)."""
        with self._lock:
            entry = self._data.get(username.lower())
            if entry is None:
                entry = self._data[username.lower()] = {"handle": b64u(secrets.token_bytes(16)), "keys": []}
                self._write()
            return unb64u(entry["handle"])

    def keys(self, username: str) -> list[dict]:
        return [dict(k) for k in self._data.get(username.lower(), {}).get("keys", [])]

    def count(self, username: str) -> int:
        return len(self._data.get(username.lower(), {}).get("keys", []))

    def find(self, cred_id: str) -> tuple[str, dict, bytes] | None:
        """(username, credential, user handle) of a credential id."""
        for user, entry in self._data.items():
            for key in entry.get("keys", []):
                if secrets.compare_digest(str(key.get("id", "")), str(cred_id or "")):
                    return user, dict(key), unb64u(entry["handle"])
        return None

    def add(self, username: str, cred: dict, name: str, rp_id: str) -> dict:
        with self._lock:
            entry = self._data.setdefault(username.lower(), {"handle": b64u(secrets.token_bytes(16)), "keys": []})
            if len(entry["keys"]) >= MAX_PER_USER:
                raise PasskeyError(f"At most {MAX_PER_USER} passkeys per account – remove one first.")
            if self.find(cred["id"]):
                raise PasskeyError("This passkey is already saved.")
            key = {"id": cred["id"], "public_key": cred["public_key"], "sign_count": int(cred["sign_count"]),
                   "name": (name.strip() or "Passkey")[:60], "rp_id": rp_id, "created": int(time.time()),
                   "last_used": None}
            entry["keys"].append(key)
            self._write()
            return dict(key)

    def used(self, username: str, cred_id: str, sign_count: int) -> None:
        with self._lock:
            for key in self._data.get(username.lower(), {}).get("keys", []):
                if key.get("id") == cred_id:
                    key["sign_count"], key["last_used"] = int(sign_count), int(time.time())
            self._write()

    def remove(self, username: str, cred_id: str) -> bool:
        with self._lock:
            entry = self._data.get(username.lower())
            if not entry:
                return False
            before = len(entry["keys"])
            entry["keys"] = [k for k in entry["keys"] if k.get("id") != cred_id]
            self._write()
            return len(entry["keys"]) < before

    def remove_user(self, username: str) -> None:
        with self._lock:
            if self._data.pop(username.lower(), None) is not None:
                self._write()

    @staticmethod
    def public(key: dict) -> dict:
        return {"id": key["id"], "name": key.get("name") or "Passkey", "created": key.get("created"),
                "last_used": key.get("last_used"), "rp_id": key.get("rp_id")}
