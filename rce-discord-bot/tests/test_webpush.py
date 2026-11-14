"""Web push for the players' web app (iPhone home screen): encryption, the VAPID signature, which addresses count."""
import base64
import json
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from support import webpush


def _browser():
    """A browser's side of a subscription: its private key and what it hands the server."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    return key, auth, {"endpoint": "https://web.push.apple.com/abc123",
                       "keys": {"p256dh": webpush.b64(public), "auth": webpush.b64(auth)}}


def test_a_browser_can_read_what_the_bot_encrypts():
    key, auth, sub = _browser()
    body = webpush.encrypt(sub, b'{"title":"Raid"}')
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    sender_public, sealed = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    ua_public = webpush.unb64(sub["keys"]["p256dh"])
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + ua_public + sender_public).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    assert AESGCM(cek).decrypt(nonce, sealed, None) == b'{"title":"Raid"}\x02'


def test_vapid_signature_and_claims():
    keys = webpush.new_keys()
    head = webpush.vapid_header(keys, "https://web.push.apple.com/abc", "https://my.server", now=1000)
    token = head.split("t=")[1].split(",")[0]
    assert head.endswith("k=" + keys["public"])
    h, c, sig = token.split(".")
    claims = json.loads(webpush.unb64(c))
    assert claims == {"aud": "https://web.push.apple.com", "exp": 1000 + 12 * 3600, "sub": "https://my.server"}
    raw = webpush.unb64(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.unb64(keys["public"]))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))                 # raises if it doesn't fit


def test_only_the_browsers_push_services_are_accepted():
    _, _, sub = _browser()
    assert webpush.valid(sub)
    for endpoint in ("https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x"):
        assert webpush.valid({**sub, "endpoint": endpoint})
    for endpoint in ("http://web.push.apple.com/x", "https://127.0.0.1/x", "https://evil.example/x",
                     "https://push.apple.com.evil.example/x", "https://web.push.apple.com:8443/x", ""):
        assert not webpush.valid({**sub, "endpoint": endpoint}), endpoint
    assert not webpush.valid({**sub, "keys": {"p256dh": "AAAA", "auth": sub["keys"]["auth"]}})
    assert not webpush.valid("nope") and not webpush.valid({"endpoint": sub["endpoint"]})
    assert base64.urlsafe_b64decode(webpush.b64(b"\xff\xfe") + "==") == b"\xff\xfe"
