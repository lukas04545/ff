"""Passkeys: a software authenticator (P-256 key) makes and uses passkeys on the control panel and the owner panel."""
import asyncio
import hashlib
import json
import os
import struct

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from web.passkeys import PasskeyError, b64u, cbor_decode, unb64u, verify_authentication
from tests.test_control_plane import cp, csrf_of  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401


def cbor(v) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if v is None:
        return b"\xf6"
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())


class Authenticator:
    """A passkey device: one P-256 key, user verification always done."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.handle = None

    def _auth_data(self, rp_id, flags, extra=b""):
        self.count += 1
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + extra

    def create(self, opts, origin):
        nums = self.key.public_key().public_numbers()
        cose = cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        self.handle = opts["user"]["id"]
        auth = self._auth_data(opts["rp"]["id"], 0x45,
                               bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose)
        client = json.dumps({"type": "webauthn.create", "challenge": opts["challenge"], "origin": origin}).encode()
        return {"id": b64u(self.cred_id), "type": "public-key", "response": {
            "clientDataJSON": b64u(client), "attestationObject": b64u(cbor({"fmt": "none", "attStmt": {},
                                                                            "authData": auth}))}}

    def get(self, opts, origin, *, flags=0x05):
        auth = self._auth_data(opts["rpId"], flags)
        client = json.dumps({"type": "webauthn.get", "challenge": opts["challenge"], "origin": origin}).encode()
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "type": "public-key", "response": {
            "clientDataJSON": b64u(client), "authenticatorData": b64u(auth), "signature": b64u(sig),
            "userHandle": self.handle}}


def test_cbor_reader():
    value = {"a": [1, -5, b"\x01", True, None], 3: {"x": "ü"}}
    assert cbor_decode(cbor(value))[0] == value
    with pytest.raises(PasskeyError):
        cbor_decode(b"\x5f")


def test_control_panel_passkey(bot):  # noqa: F811
    dev = Authenticator()

    async def scenario(c):
        origin = f"http://{c.server.host}:{c.server.port}"
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        o = await (await c.post("/api/passkeys/options", headers=H)).json()
        assert o["publicKey"]["rp"]["id"] == c.server.host
        answer = dev.create(o["publicKey"], origin)
        r = await c.post("/api/passkeys", json={"challenge_id": o["challenge_id"], "answer": answer, "name": "Phone"},
                         headers=H)
        assert r.status == 200, await r.text()
        # the same challenge can't be used twice
        r = await c.post("/api/passkeys", json={"challenge_id": o["challenge_id"], "answer": answer}, headers=H)
        assert r.status == 400
        keys = (await (await c.get("/api/passkeys")).json())["passkeys"]
        assert [k["name"] for k in keys] == ["Phone"]
        await c.post("/api/logout", headers=H)
        assert (await c.get("/api/me")).status == 401
        # sign in with it – no password
        o = await (await c.post("/api/login/passkey/options", headers=H)).json()
        r = await c.post("/api/login/passkey", json={"challenge_id": o["challenge_id"],
                                                     "answer": dev.get(o["publicKey"], origin)}, headers=H)
        assert r.status == 200, await r.text()
        assert (await (await c.get("/api/me")).json())["user"] == "owner"
        await c.post("/api/logout", headers=H)
        # wrong address, no user verification, a replayed counter: refused
        o = await (await c.post("/api/login/passkey/options", headers=H)).json()
        r = await c.post("/api/login/passkey", json={"challenge_id": o["challenge_id"],
                                                     "answer": dev.get(o["publicKey"], "https://evil.example")}, headers=H)
        assert r.status == 401
        o = await (await c.post("/api/login/passkey/options", headers=H)).json()
        r = await c.post("/api/login/passkey", json={"challenge_id": o["challenge_id"],
                                                     "answer": dev.get(o["publicKey"], origin, flags=0x01)}, headers=H)
        assert r.status == 401 and "confirm" in (await r.json())["error"]
        dev.count = 0
        o = await (await c.post("/api/login/passkey/options", headers=H)).json()
        r = await c.post("/api/login/passkey", json={"challenge_id": o["challenge_id"],
                                                     "answer": dev.get(o["publicKey"], origin)}, headers=H)
        assert r.status == 401 and "copied" in (await r.json())["error"]
        assert (await c.get("/api/me")).status == 401
    run(bot, scenario)


def test_signature_must_match():
    dev, other = Authenticator(), Authenticator()
    opts = {"rp": {"id": "panel.test", "name": "x"}, "user": {"id": b64u(b"u" * 16)}, "challenge": b64u(b"c" * 32)}
    created = dev.create(opts, "https://panel.test")
    from web.passkeys import verify_registration
    cred = verify_registration(created, challenge=b"c" * 32, origins={"https://panel.test"}, rp_id="panel.test")
    get_opts = {"rpId": "panel.test", "challenge": b64u(b"d" * 32)}
    answer = other.get(get_opts, "https://panel.test")
    with pytest.raises(PasskeyError, match="signature"):
        verify_authentication(answer, challenge=b"d" * 32, origins={"https://panel.test"}, rp_id="panel.test",
                              credential=cred, user_handle=unb64u(opts["user"]["id"]))


def test_owner_panel_passkey(cp):  # noqa: F811
    from control_plane import websettings
    from control_plane.web import build_owner
    dev = Authenticator()

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        origin = f"http://{c.server.host}:{c.server.port}"
        try:
            assert "passkey.js" in await (await c.get("/login")).text()
            assert "RCE_PASSKEY" in await (await c.get("/passkey.js")).text()
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await c.get("/settings")).text()
            assert "Passkeys" in page and "No passkeys yet" in page
            t = csrf_of(page)
            assert (await c.post("/passkeys/options", data={})).status == 403               # CSRF
            o = await (await c.post("/passkeys/options", data={"csrf": t})).json()
            r = await c.post("/passkeys", data={"csrf": t, "challenge_id": o["challenge_id"], "name": "Laptop",
                                                "answer": json.dumps(dev.create(o["publicKey"], origin))})
            assert r.status == 200, await r.text()
            assert "Laptop" in await (await c.get("/settings")).text()
            c.session.cookie_jar.clear()
            assert (await c.get("/settings", allow_redirects=False)).status == 303
            o = await (await c.post("/login/passkey/options", data={})).json()
            r = await c.post("/login/passkey", data={"challenge_id": o["challenge_id"],
                                                     "answer": json.dumps(dev.get(o["publicKey"], origin))})
            assert r.status == 200 and (await r.json())["next"] == "/"
            assert (await c.get("/settings", allow_redirects=False)).status == 200       # signed in
            assert cp.db.one("SELECT 1 FROM audit_log WHERE action = 'owner.login' AND detail LIKE '%passkey%'")
        finally:
            await c.close()
    asyncio.run(scenario())
