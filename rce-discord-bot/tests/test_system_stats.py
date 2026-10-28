"""System page: CPU from two readings, memory, the bot's processes; the API needs a login."""
import asyncio
import os

import web.system_stats as ss
from tests.test_web_security import H, bot  # noqa: F401


def test_cpu_percent_from_two_readings_and_memory(monkeypatch):
    readings = [(100.0, 1000.0), (130.0, 1100.0)]              # 30 of 100 ticks idle -> 70 % busy
    monkeypatch.setattr(ss, "cpu_times", lambda: readings.pop(0) if len(readings) > 1 else readings[0])
    monkeypatch.setattr(ss, "memory", lambda: (8 * 1024 ** 3, 2 * 1024 ** 3))
    m = ss.SystemMonitor()
    first = m.sample(now=1)
    assert first["cpu"] is None and first["mem_used"] == 6 * 1024 ** 3     # no CPU yet: needs two readings
    second = m.sample(now=6)
    assert second["cpu"] == 70.0 and len(m.history) == 2
    snap = m.snapshot({"Main": os.getpid(), "2x": None})
    assert snap["latest"]["cpu"] == 70.0 and snap["cores"] == os.cpu_count()
    assert snap["processes"][1] == {"name": "2x", "pid": None, "memory": None}


def test_unknown_system_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(ss, "cpu_times", lambda: None)
    monkeypatch.setattr(ss, "memory", lambda: None)
    snap = ss.SystemMonitor().snapshot({})
    assert snap["supported"] is False and snap["history"] == []


def test_this_linux_box_is_read():
    if not ss.sys.platform.startswith("linux"):
        return
    total, avail = ss.memory()
    assert total > avail > 0 and ss.process_memory(os.getpid()) > 1024 ** 2 and ss.cpu_times()[1] > 0


def test_system_api_needs_login(bot):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        w = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            assert (await c.get("/api/system")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/system")).json()
            assert d["processes"][0]["pid"] == os.getpid() and "history" in d and d["cores"]
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())
