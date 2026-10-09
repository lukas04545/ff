"""Tests für das Webinterface (ohne Discord-Login, ohne RCON-Verbindung)."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

H = {"X-Requested-With": "rce-web"}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    env = dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
               WEB_PASSWORD="geheim123", DATABASE_PATH=str(tmp_path / "bot.db"),
               SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "wissen.md"), ALLOW_RAW_RCON="true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from config import load_config
    from bot import RceBot
    b = RceBot(load_config("/nonexistent"))
    yield b
    b.db.close()


def run(bot, scenario):
    async def main():
        from web.server import WebInterface
        web = WebInterface(bot)
        client = TestClient(TestServer(web.app))
        await client.start_server()
        try:
            await scenario(client)
        finally:
            await client.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_login_csrf_and_auth(bot):
    async def scenario(c):
        assert (await c.get("/")).status == 200
        assert (await c.get("/static/app.js")).status == 200
        assert (await c.get("/api/status")).status == 401
        # Ohne CSRF-Header wird jede schreibende Anfrage abgelehnt – auch der Login
        assert (await c.post("/api/login", json={"password": "geheim123"})).status == 403
        assert (await c.post("/api/login", json={"password": "falsch"}, headers=H)).status == 401
        r = await c.post("/api/login", json={"password": "geheim123"}, headers=H)
        assert r.status == 200
        assert "httponly" in r.headers["Set-Cookie"].lower() and "samesite=strict" in r.headers["Set-Cookie"].lower()
        status = await (await c.get("/api/status")).json()
        assert status["rcon_connected"] is False and status["players"] == []
        # Ohne RCON-Verbindung: verständlicher Fehler statt Absturz
        r = await c.post("/api/command", json={"command": "serverinfo"}, headers=H)
        assert r.status == 503 and "RCON" in (await r.json())["error"]
        await c.post("/api/logout", headers=H)
        assert (await c.get("/api/status")).status == 401
    run(bot, scenario)


def test_login_rate_limit(bot):
    async def scenario(c):
        for _ in range(5):
            await c.post("/api/login", json={"password": "x"}, headers=H)
        r = await c.post("/api/login", json={"password": "geheim123"}, headers=H)
        assert r.status == 429
    run(bot, scenario)


def test_custom_kits_autokits_knowledge(bot):
    async def scenario(c):
        await c.post("/api/login", json={"password": "geheim123"}, headers=H)
        assert (await c.post("/api/custom-kits", json={"name": "Event"}, headers=H)).status == 200
        assert (await c.post("/api/custom-kits", json={"name": "event"}, headers=H)).status == 409
        r = await c.post("/api/custom-kits/Event/items", json={"item": "rifle.ak", "amount": 2}, headers=H)
        assert (await r.json())["known_item"] is True
        r = await c.post("/api/autokits", json={"kit": "event", "kit_type": "custom", "trigger": "quickchat",
                                                "phrase": "d11_quick_chat_orders_slot_6", "cooldown": 30}, headers=H)
        rule_id = (await r.json())["id"]
        assert (await c.post("/api/autokits", json={"kit": "x", "trigger": "quickchat"}, headers=H)).status == 400
        assert (await c.post(f"/api/autokits/{rule_id}/toggle", json={"enabled": False}, headers=H)).status == 200
        kits = await (await c.get("/api/kits")).json()
        assert kits["custom"][0]["items"][0]["shortname"] == "rifle.ak"
        assert kits["rules"][0]["enabled"] == 0 and kits["rules"][0]["kit"] == "Event"
        assert (await c.delete("/api/custom-kits/Event", headers=H)).status == 200
        assert (await (await c.get("/api/kits")).json())["rules"] == []

        assert (await c.put("/api/knowledge", json={"text": "Wipe: Do 18 Uhr"}, headers=H)).status == 200
        assert (await (await c.get("/api/knowledge")).json())["text"] == "Wipe: Do 18 Uhr"
        meta = await (await c.get("/api/meta")).json()
        assert meta["phrases"] and meta["items"]
        assert (await c.get("/api/leaderboard?category=kd")).status == 200
        assert (await c.get("/api/leaderboard?category=drop")).status == 400
    run(bot, scenario)


def test_rules_and_community(bot, tmp_path, monkeypatch):
    async def scenario(c):
        await c.post("/api/login", json={"password": "geheim123"}, headers=H)
        assert (await c.put("/api/rules", json={"text": "Kein Cheaten\nRespekt"}, headers=H)).status == 200
        assert (await (await c.get("/api/rules")).json())["text"] == "Kein Cheaten\nRespekt"
        data = await (await c.get("/api/community")).json()
        assert data["giveaways"] == [] and data["welcome_channel"] is None
        assert (await c.post("/api/giveaways/99/end", headers=H)).status == 404
    monkeypatch.chdir(tmp_path)  # rules.md im Testordner anlegen, nicht im Projekt
    run(bot, scenario)
