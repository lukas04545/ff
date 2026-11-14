"""Web Push (RFC 8030 / 8291 / 8292) – notifications for the players' web app, e.g. on an iPhone's home screen.

A browser hands the web app a "subscription" (an address at Apple / Google / Mozilla plus two keys). To notify it,
the bot encrypts the text for that browser (aes128gcm) and posts it to the address, signed with the server's own
VAPID key – no account at any push service is needed. Only the push services below are ever contacted.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# the push services of the browsers (a subscription with any other address is refused – the bot never posts to
# an address a visitor made up)
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")


class PushError(Exception):
    def __init__(self, message: str, *, gone: bool = False) -> None:
        super().__init__(message)
        self.gone = gone                      # the subscription doesn't exist any more: forget it


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    text = str(text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_keys() -> dict:
    """A VAPID key pair: {"private": …, "public": …} (base64url; the public one goes to the browsers)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private": b64(private), "public": b64(public)}


def _private(keys: dict) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64(keys["private"]), "big"), ec.SECP256R1())


def valid(sub) -> bool:
    """A subscription the bot accepts: https, one of the browsers' push services, both keys present."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        return False
    url = urlsplit(str(sub.get("endpoint") or ""))
    host = (url.hostname or "").lower()
    if url.scheme != "https" or url.port not in (None, 443) or len(str(sub["endpoint"])) > 1000:
        return False
    if not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        return False
    try:
        return len(unb64(sub["keys"].get("p256dh", ""))) == 65 and len(unb64(sub["keys"].get("auth", ""))) == 16
    except (ValueError, TypeError):
        return False


def encrypt(sub: dict, payload: bytes, *, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The body of a push message for this subscription (RFC 8291, aes128gcm, one record)."""
    ua_public = unb64(sub["keys"]["p256dh"])
    auth = unb64(sub["keys"]["auth"])
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public = sender.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    sealed = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # 0x02: the last (only) record
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + sealed


def vapid_header(keys: dict, endpoint: str, subject: str, now: float | None = None) -> str:
    """Authorization for one push service (a JWT signed with the VAPID key, good for 12 hours)."""
    url = urlsplit(endpoint)
    head = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(now or time.time()) + 12 * 3600,
                             "sub": subject}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(_private(keys).sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{claims}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={keys['public']}"


async def send(sub: dict, message: dict, keys: dict, subject: str, *, urgent: bool = False,
               session: aiohttp.ClientSession | None = None) -> None:
    """Sends one notification. Raises PushError (gone=True: the browser unsubscribed – forget the subscription)."""
    if not valid(sub):
        raise PushError("Not a push address of a known browser.", gone=True)
    body = encrypt(sub, json.dumps(message, separators=(",", ":")).encode()[:3000])
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "3600",
               "Urgency": "high" if urgent else "normal",
               "Authorization": vapid_header(keys, sub["endpoint"], subject)}
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        async with session.post(sub["endpoint"], data=body, headers=headers, allow_redirects=False) as resp:
            if resp.status in (404, 410):
                raise PushError("The subscription ended.", gone=True)
            if resp.status >= 400:
                raise PushError(f"The push service answered {resp.status}: {(await resp.text())[:200]}",
                                gone=False)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise PushError(f"The push service isn't reachable ({type(exc).__name__}).") from None
    finally:
        if own:
            await session.close()
