"""Rate limits (logins, password change, one-time link, webhook, player website, item pictures) and SHA-256
password hashing."""
import asyncio
import hashlib
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.test_web import H, bot  # noqa: F401
from web import ratelimit
from web.ratelimit import RateLimiter
from web.users import UserStore, hash_password, needs_rehash, same_secret, verify_password


def run(bot, scenario):
    """Like tests.test_web.run, but the scenario also gets the WebInterface."""
    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        client = TestClient(TestServer(web.app))
        await client.start_server()
        try:
            await scenario(client, web)
        finally:
            await client.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_rate_limiter_window_and_memory():
    rl = RateLimiter(3, 60, max_keys=10)
    assert [rl.hit("a", now=t) for t in (0, 1, 2)] == [None, None, None]
    assert rl.hit("a", now=3) == pytest.approx(57)          # 4th within the minute: wait until the 1st expires
    assert rl.hit("a", now=61) is None                      # the first hit left the window
    for i in range(50):                                     # thousands of random keys can't fill the memory
        rl.hit(f"k{i}", now=100 + i)
    assert len(rl) <= 11
    rl.clear("a")
    assert rl.retry_after("a") is None


def test_pbkdf2_sha256_hashes_and_upgrade(tmp_path):
    stored = hash_password("correct horse", rounds=1000)
    scheme, rounds, salt, digest = stored.split("$")
    assert scheme == "pbkdf2_sha256" and rounds == "1000" and len(bytes.fromhex(salt)) == 16
    assert digest == hashlib.pbkdf2_hmac("sha256", b"correct horse", bytes.fromhex(salt), 1000).hex()
    assert verify_password("correct horse", stored) and not verify_password("wrong", stored)
    assert hash_password("x", rounds=1000) != hash_password("x", rounds=1000)            # salted
    assert not verify_password("x", "md5$abc") and not verify_password("x", "pbkdf2_sha256$1$aa$bb")
    assert needs_rehash(stored) and not needs_rehash(hash_password("x"))
    assert same_secret("pw", "pw") and not same_secret("pw", "pw2")
    # Accounts with an old scrypt hash still log in and are upgraded to PBKDF2-SHA256
    salt = bytes(16)
    old = "scrypt$" + salt.hex() + "$" + hashlib.scrypt(b"anna-pass-1", salt=salt, dklen=32, n=2 ** 14, r=8,
                                                         p=1).hex()
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": {"anna": {"username": "anna", "hash": old, "role": "admin"}}}))
    store = UserStore(path)
    assert store.authenticate("anna", "wrong") is None and store.get("anna")["hash"] == old
    assert store.authenticate("anna", "anna-pass-1") is not None
    assert json.loads(path.read_text())["users"]["anna"]["hash"].startswith("pbkdf2_sha256$600000$")
    assert store.authenticate("anna", "anna-pass-1") is not None


def test_login_lockout_after_many_failures(bot, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])

    async def scenario(c, web):
        for minute in range(3):                               # 5 wrong passwords per minute, 3 minutes
            for _ in range(5):
                r = await c.post("/api/login", json={"password": "wrong"}, headers=H)
                assert r.status == 401
            assert (await c.post("/api/login", json={"password": "wrong"}, headers=H)).status == 429
            clock[0] += 61
        # 15 failures: locked for 15 minutes – even with the right password and after the minute is over
        r = await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert r.status == 429 and "minutes" in (await r.json())["error"]
        clock[0] += 901
        assert (await c.post("/api/login", json={"password": "secret123"}, headers=H)).status == 200
        # The one-time link is limited too (wrong tokens)
        for _ in range(16):
            r = await c.get("/auth?token=nope", allow_redirects=False)
        clock[0] += 61
        r = await c.get(f"/auth?token={web.login_token}", allow_redirects=False)
        assert r.status == 302 and "rce_session" not in r.cookies
    run(bot, scenario)


def test_password_change_and_webhook_are_limited(bot, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])

    async def scenario(c, web):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await c.post("/api/users", json={"username": "anna", "password": "anna-pass-1", "role": "admin"},
                             headers=H)).status == 200
        c.session.cookie_jar.clear()
        await c.post("/api/login", json={"username": "anna", "password": "anna-pass-1"}, headers=H)
        statuses = []
        for _ in range(6):
            r = await c.post("/api/account/password", json={"current": "bad", "new": "whatever-1"}, headers=H)
            statuses.append(r.status)
        assert statuses == [403] * 5 + [429]
        for _ in range(15):
            assert (await c.post("/hooks/tip4serv/wrongtoken", json={})).status == 404
        assert (await c.post("/hooks/tip4serv/wrongtoken", json={})).status == 429
    run(bot, scenario)


def test_player_website_and_pictures_are_limited(bot, monkeypatch):
    clock = [9000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])

    async def scenario(c, web):
        for _ in range(60):
            assert (await c.get("/leaderboard")).status == 404       # plugins not loaded here: 404, but counted
        r = await c.get("/leaderboard")
        assert r.status == 429 and "Retry-After" in r.headers
        assert (await c.get("/p/website")).status == 429
        statuses = {(await c.get("/img/items/rifle.ak.png")).status for _ in range(300)}
        assert 429 not in statuses and (await c.get("/img/items/rifle.ak.png")).status == 429
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await c.get("/img/items/rifle.ak.png")).status != 429   # logged-in admins: no limit
        clock[0] += 61
        c.session.cookie_jar.clear()
        assert (await c.get("/leaderboard")).status == 404                    # a minute later: allowed again
        assert web._public_total.limit == 1200
    run(bot, scenario)
