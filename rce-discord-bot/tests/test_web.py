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
        assert (await c.put(f"/api/autokits/{rule_id}/role", json={"required_role_id": "55"}, headers=H)).status == 200
        assert bot.db.kit_rule(rule_id)["required_role_id"] == 55
        assert (await c.put(f"/api/autokits/{rule_id}/role", json={"required_role_id": "x"}, headers=H)).status == 400
        assert (await c.put("/api/autokits/999/role", json={"required_role_id": ""}, headers=H)).status == 404
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
        r = await c.post("/api/custom-kits/import", headers=H, json={
            "text": 'kit add "Builder Kit" "ammo.grenadelauncher.buckshot" 1 1 main', "replace": True})
        assert (await r.json())["kits"][0]["name"] == "Builder Kit"
        assert (await c.post("/api/custom-kits/import", json={"text": "nothing"}, headers=H)).status == 400
        bot.db.link_player(42, "GamerTag42")
        linked = (await (await c.get("/api/kits")).json())["linked"]
        assert linked == [{"user_id": "42", "player": "GamerTag42", "linked_at": linked[0]["linked_at"],
                           "user": "42"}]
        assert (await c.delete("/api/linked-players/42", headers=H)).status == 200
        assert (await c.delete("/api/linked-players/42", headers=H)).status == 404
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


def test_kit_builder(bot):
    sent = []

    async def command(cmd, **kwargs):
        sent.append(cmd)
        return ""
    bot.rcon.command = command

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        meta = await (await c.get("/api/meta")).json()
        assert sum(len(cat["items"]) for cat in meta["catalogue"]) == 566 and meta["kit_add_template"]
        weapons = next(cat for cat in meta["catalogue"] if cat["category"] == "Weapons")
        assert {"short": "rifle.ak", "label": "Assault Rifle", "group": "Guns"} in weapons["items"]

        items = [{"item": "rifle.ak", "amount": 1, "condition": 80, "container": "belt"},
                 {"item": "lumberjack hoodie", "amount": 1, "container": "Wear"},
                 {"item": "ammo.rifle", "amount": 128, "container": "Main"}]
        r = await (await c.post("/api/kit-builder/ingame", json={"kit": "Starter", "items": items}, headers=H)).json()
        assert r["added"] == 3 and sent == [
            'kit add "Starter" "rifle.ak" "1" "80" "Belt"',
            'kit add "Starter" "lumberjack hoodie" "1" "100" "Wear"',
            'kit add "Starter" "ammo.rifle" "128" "100" "Main"']

        r = await (await c.post("/api/kit-builder/custom", json={"kit": "Starter", "items": items}, headers=H)).json()
        assert r["created"] and r["added"] == 3
        assert [i["shortname"] for i in bot.db.custom_kit_items("Starter")] == ["rifle.ak", "lumberjack hoodie", "ammo.rifle"]
        r = await (await c.post("/api/kit-builder/custom", json={"kit": "starter", "items": items[:1]}, headers=H)).json()
        assert not r["created"] and len(bot.db.custom_kit_items("Starter")) == 1  # replaced

        for bad in ({"kit": "", "items": items}, {"kit": "X", "items": []},
                    {"kit": "X", "items": [{"item": "rifle.ak; quit", "amount": 1}]},
                    {"kit": "X", "items": [{"item": "rifle.ak", "container": "Pocket"}]},
                    {"kit": "X", "items": [{"item": "wood"}] * 38}):
            assert (await c.post("/api/kit-builder/ingame", json=bad, headers=H)).status == 400, bad
    run(bot, scenario)


def test_admin_accounts(bot):
    async def scenario(c):
        async def login(username, password):
            return await c.post("/api/login", json={"username": username, "password": password}, headers=H)

        # Owner (WEB_PASSWORD) – username defaults to "owner"
        assert (await c.post("/api/login", json={"password": "secret123"}, headers=H)).status == 200
        me = await (await c.get("/api/me")).json()
        assert me["user"] == "owner" and me["role"] == "owner"

        r = await c.post("/api/users", json={"username": "Mod_Anna", "password": "anna-pass-1", "role": "admin"},
                         headers=H)
        assert r.status == 200
        for bad in ({"username": "owner", "password": "longenough1"}, {"username": "mod_anna", "password": "x" * 9},
                    {"username": "ab", "password": "longenough1"}, {"username": "Bob", "password": "short"},
                    {"username": "Bob", "password": "longenough1", "role": "god"}):
            assert (await c.post("/api/users", json=bad, headers=H)).status == 400, bad
        users = (await (await c.get("/api/users")).json())["users"]
        assert [(u["username"], u["role"], u["created_by"]) for u in users] == [("Mod_Anna", "admin", "owner")]
        stored = (bot.config.database_path.parent / "web_users.json").read_text()
        assert "anna-pass-1" not in stored and "pbkdf2_sha256$600000$" in stored  # only hashes on disk

        # Admin: everything except settings / users / restart
        await c.post("/api/logout", headers=H)
        assert (await login("mod_anna", "wrong-password")).status == 401
        assert (await login("mod_anna", "anna-pass-1")).status == 200   # username is case-insensitive
        me = await (await c.get("/api/me")).json()
        assert me["user"] == "Mod_Anna" and me["role"] == "admin"
        assert (await c.get("/api/kits")).status == 200
        for method, path in (("GET", "/api/settings"), ("GET", "/api/users"), ("POST", "/api/restart"),
                             ("PUT", "/api/settings"), ("DELETE", "/api/users/Mod_Anna")):
            assert (await c.request(method, path, json={}, headers=H)).status == 403, path

        # Own password change: wrong current password refused, then works and keeps this session
        r = await c.post("/api/account/password", json={"current": "nope", "new": "anna-pass-2"}, headers=H)
        assert r.status == 403
        r = await c.post("/api/account/password", json={"current": "anna-pass-1", "new": "anna-pass-2"}, headers=H)
        assert r.status == 200 and (await c.get("/api/me")).status == 200
        old_cookie = c.session.cookie_jar.filter_cookies(c.make_url("/"))["rce_session"].value

        # Owner disables the account -> the admin's session ends immediately
        await c.post("/api/logout", headers=H)
        await login("owner", "secret123")
        assert (await c.put("/api/users/Mod_Anna", json={"disabled": True}, headers=H)).status == 200
        c.session.cookie_jar.update_cookies({"rce_session": old_cookie}, response_url=c.make_url("/"))
        assert (await c.get("/api/me")).status == 401
        assert (await login("Mod_Anna", "anna-pass-2")).status == 401

        # Re-enable + promote to owner; deletion
        await login("owner", "secret123")
        assert (await c.put("/api/users/Mod_Anna", json={"disabled": False, "role": "owner"}, headers=H)).status == 200
        assert (await c.put("/api/users/ghost", json={"role": "admin"}, headers=H)).status == 404
        await c.post("/api/logout", headers=H)
        await login("Mod_Anna", "anna-pass-2")
        assert (await c.get("/api/users")).status == 200                      # owners may manage users
        assert (await c.delete("/api/users/Mod_Anna", headers=H)).status == 400  # not yourself
        await login("owner", "secret123")
        assert (await c.delete("/api/users/Mod_Anna", headers=H)).status == 200
        assert (await login("Mod_Anna", "anna-pass-2")).status == 401

        # Brute force protection per account
        for _ in range(5):
            await login("someone", "guess")
        assert (await login("someone", "guess")).status == 429
    run(bot, scenario)


def test_neon_theme_files(bot):
    """The neon look (neon.css + animated background bg.js) is served and loaded by the page; light mode is left alone."""
    async def scenario(c):
        page = await (await c.get("/")).text()
        assert "/static/neon.css" in page and "/static/bg.js" in page
        assert page.index("/static/style.css") < page.index("/static/neon.css")      # neon overrides the base theme
        css = await c.get("/static/neon.css")
        text = await css.text()
        assert css.status == 200 and "--neon: #ff7a1a" in text and "@keyframes border-spin" in text
        # every rule is scoped to "not light", so the light theme keeps its look
        assert ':not([data-theme="light"])' in text and ':root[data-theme="light"] #bg { display: none; }' in text
        assert "prefers-reduced-motion: reduce" in text                              # calm mode switches animations off
        js = await c.get("/static/bg.js")
        src = await js.text()
        assert js.status == 200 and 'canvas.id = "bg"' in src
        assert "visibilitychange" in src and "prefers-reduced-motion" in src and "requestAnimationFrame" in src
        app = await (await c.get("/static/app.js")).text()
        assert 'THEME_NAMES = { dark: "neon", light: "light" }' in app                # theme button: neon <-> light
    run(bot, scenario)
