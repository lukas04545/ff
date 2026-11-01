"""Flood (DDoS) shield for the web server and the slash-command flood guard."""
from support.command_guard import CommandGuard
from tests.test_web import H, bot, run  # noqa: F401
from web.shield import Shield


def test_per_ip_limit_strikes_and_ban():
    s = Shield(per_ip=20, total=10_000, ban_minutes=15)
    t = 1000.0
    assert all(s.check("1.1.1.1", now=t) is None for _ in range(20))
    status, _, retry = s.check("1.1.1.1", now=t)
    assert status == 429 and retry >= 1
    assert s.check("2.2.2.2", now=t) is None                                 # others unaffected
    assert all(s.check("1.1.1.1", trusted=True, now=t) is None for _ in range(50))   # admins / this PC
    for _ in range(5):                                                       # keeps flooding -> banned
        s.check("1.1.1.1", now=t)
    assert s.banned("1.1.1.1", now=t) and s.bans
    assert s.check("1.1.1.1", now=t + 30)[0] == 429                          # still banned after the window
    assert s.unban("1.1.1.1") and s.check("1.1.1.1", now=t + 31) is None
    assert all(s.check("3.3.3.3", asset=True, now=t) is None for _ in range(100))    # pictures: 8×


def test_under_attack_mode_and_busy_cap():
    events = []
    s = Shield(per_ip=50, total=100, max_inflight=3)
    s.on_event = events.append
    t = 5000.0
    for i in range(101):
        s.check(f"10.0.{i // 250}.{i % 250}", now=t)
    assert s._attacking(t) and any("Under attack" in e for e in events)
    assert all(s.check("9.9.9.9", now=t) is None for _ in range(10))
    assert s.check("9.9.9.9", now=t)[0] == 429                               # a fifth of the limit now
    s.inflight = 3
    assert s.check("8.8.8.8", now=t)[0] == 503 and s.check("8.8.8.8", trusted=True, now=t) is None


def test_command_guard():
    g = CommandGuard(limit=5)
    t = 100.0
    assert all(g.check(1, now=t) is None for _ in range(5))
    assert "Slow down" in g.check(1, now=t) and g.check(2, now=t) is None
    g.check(1, now=t)
    assert "paused" in g.check(1, now=t) and "paused" in g.check(1, now=t + 60)     # third time: paused
    assert g.check(1, now=t + 400) is None


def test_web_server_blocks_floods_but_not_admins(bot):  # noqa: F811
    async def scenario(c):
        visitor = {"CF-Connecting-IP": "203.0.113.50"}
        codes = [(await c.get("/robots.txt", headers=visitor)).status for _ in range(5)]
        assert 429 not in codes
        shield = c._shield
        shield.per_ip = 10
        from web.ratelimit import RateLimiter
        shield._ip = RateLimiter(10, 10)
        codes = [(await c.get("/robots.txt", headers=visitor)).status for _ in range(15)]
        assert codes.count(429) >= 4
        r = await c.get("/robots.txt", headers=visitor)
        assert r.status == 429 and r.headers["Retry-After"]
        assert (await c.get("/robots.txt")).status != 429                     # this PC: never limited
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        st = await (await c.get("/api/shield")).json()
        assert st["enabled"] and st["blocked"] >= 5 and st["exposed"] is False
        assert (await c.post("/api/shield", json={"ip": "198.51.100.7", "minutes": 5}, headers=H)).status == 200
        st = await (await c.get("/api/shield")).json()
        assert any(b["ip"] == "198.51.100.7" for b in st["bans"])
        jar = c.session.cookie_jar
        cookies = list(jar)
        jar.clear()                                                           # a visitor without the admin login
        assert (await c.get("/robots.txt", headers={"CF-Connecting-IP": "198.51.100.7"})).status == 429
        for morsel in cookies:
            jar.update_cookies({morsel.key: morsel.value})
        await c.post("/api/shield", json={"ip": "198.51.100.7", "unban": True}, headers=H)
        jar.clear()
        assert (await c.get("/robots.txt", headers={"CF-Connecting-IP": "198.51.100.7"})).status != 429
    run_with_shield(bot, scenario)


def run_with_shield(bot, scenario):  # noqa: F811
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from web.server import WebInterface

    async def main():
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        client = TestClient(TestServer(web.app))
        client._shield = web.shield
        await client.start_server()
        try:
            await scenario(client)
        finally:
            await client.close()
            await bot.rcon.close()
    asyncio.run(main())
