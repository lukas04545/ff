"""Tests for the web interface (no Discord login, no RCON connection)."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

H = {"X-Requested-With": "rce-web"}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    env = dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
               WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"),
               SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "knowledge.md"), ALLOW_RAW_RCON="true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from config import load_config
    from bot import RceBot
    b = RceBot(load_config("/nonexistent"))
    yield b
    b.db.close()


def run(bot, scenario, **kwargs):
    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        kwargs.setdefault("env_path", tmp / ".env")
        kwargs.setdefault("secret_path", tmp / "web_secret.key")
        web = WebInterface(bot, **kwargs)
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
        # Without the CSRF header every write request is rejected – including login
        assert (await c.post("/api/login", json={"password": "secret123"})).status == 403
        assert (await c.post("/api/login", json={"password": "wrong"}, headers=H)).status == 401
        r = await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert r.status == 200
        assert "httponly" in r.headers["Set-Cookie"].lower() and "samesite=strict" in r.headers["Set-Cookie"].lower()
        status = await (await c.get("/api/status")).json()
        assert status["rcon_connected"] is False and status["players"] == []
        # Without an RCON connection: a clear error instead of a crash
        r = await c.post("/api/command", json={"command": "serverinfo"}, headers=H)
        assert r.status == 503 and "RCON" in (await r.json())["error"]
        await c.post("/api/logout", headers=H)
        assert (await c.get("/api/status")).status == 401
    run(bot, scenario)


def test_login_rate_limit(bot):
    async def scenario(c):
        for _ in range(5):
            await c.post("/api/login", json={"password": "x"}, headers=H)
        r = await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert r.status == 429
    run(bot, scenario)


def test_custom_kits_autokits_knowledge(bot):
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
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
        # Self-service claim settings
        r = await c.put("/api/custom-kits/Event/claim", json={"role_ids": ["55", "66"], "cooldown": "1d 12h"},
                        headers=H)
        assert (await r.json())["cooldown_minutes"] == 2160
        kit = (await (await c.get("/api/kits")).json())["custom"][0]
        assert kit["claim_role_ids"] == "55,66" and kit["cooldown_minutes"] == 2160
        assert (await c.put("/api/custom-kits/Event/claim", json={"role_ids": [], "cooldown": "soon"},
                            headers=H)).status == 400
        assert (await c.put("/api/custom-kits/Event/claim", json={"role_ids": ["x"], "cooldown": "0"},
                            headers=H)).status == 400
        assert (await c.delete("/api/custom-kits/Event", headers=H)).status == 200
        assert (await (await c.get("/api/kits")).json())["rules"] == []

        assert (await c.put("/api/knowledge", json={"text": "Wipe: Thu 6 pm"}, headers=H)).status == 200
        assert (await (await c.get("/api/knowledge")).json())["text"] == "Wipe: Thu 6 pm"
        meta = await (await c.get("/api/meta")).json()
        assert meta["phrases"] and meta["items"]
        assert (await c.get("/api/leaderboard?category=kd")).status == 200
        assert (await c.get("/api/leaderboard?category=drop")).status == 400
    run(bot, scenario)


def test_rules_and_community(bot, tmp_path, monkeypatch):
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await c.put("/api/rules", json={"text": "No cheating\nRespect"}, headers=H)).status == 200
        assert (await (await c.get("/api/rules")).json())["text"] == "No cheating\nRespect"
        data = await (await c.get("/api/community")).json()
        assert data["giveaways"] == [] and data["welcome_channel"] is None
        assert (await c.post("/api/giveaways/99/end", headers=H)).status == 404
    monkeypatch.chdir(tmp_path)  # create rules.md in the test folder, not in the project
    run(bot, scenario)
