"""Switching the bot to another game server: saved RCON servers in Settings → RCON (one click, no restart)."""
from types import SimpleNamespace as NS

from tests.test_web import H, bot, run  # noqa: F401


def test_save_and_switch_game_servers(bot, monkeypatch):  # noqa: F811
    import web.server as ws
    tested = []

    async def diagnose(host, port, password):
        tested.append((host, port, password))
        return NS(ok=True, steps=[])
    monkeypatch.setattr(ws, "diagnose_rcon", diagnose)
    env = bot.config.database_path.parent / ".env"
    env.write_text("RCON_HOST=1.1.1.1\nRCON_PORT=28016\nRCON_PASSWORD=first\n")

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await (await c.get("/api/settings/rcon-servers")).json())["servers"] == []
        r = await c.post("/api/settings/rcon-servers", json={"name": "EU main"}, headers=H)
        assert r.status == 200
        # the customer moves to another server (Settings → RCON) and saves that one too
        env.write_text("RCON_HOST=2.2.2.2\nRCON_PORT=28020\nRCON_PASSWORD=second\nRCON_ENABLED=false\n")
        await c.post("/api/settings/rcon-servers", json={"name": "Test server"}, headers=H)
        d = await (await c.get("/api/settings/rcon-servers")).json()
        assert [(s["name"], s["current"]) for s in d["servers"]] == [("EU main", False), ("Test server", True)]
        assert "first" not in str(d) and "second" not in str(d)                   # passwords never leave the bot
        eu = d["servers"][0]["id"]
        r = await c.post(f"/api/settings/rcon-servers/{eu}/use", headers=H)
        assert r.status == 200 and (await r.json())["rcon_test"]["ok"]
        saved = env.read_text()
        assert "RCON_HOST=1.1.1.1" in saved and "RCON_PASSWORD=first" in saved and "RCON_ENABLED=true" in saved
        assert tested[-1] == ("1.1.1.1", "28016", "first")
        assert (bot.rcon.host, bot.rcon.port, bot.rcon.enabled) == ("1.1.1.1", 28016, True)   # live, no restart
        assert (await c.delete(f"/api/settings/rcon-servers/{eu}", headers=H)).status == 200
        assert len((await (await c.get("/api/settings/rcon-servers")).json())["servers"]) == 1
        assert (await c.post("/api/settings/rcon-servers/deadbeef/use", headers=H)).status == 404
    run(bot, scenario)


def test_rcon_off_needs_no_details(monkeypatch, tmp_path):
    from config import ConfigError, load_config
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="", RCON_PORT="", RCON_PASSWORD="", ADMIN_ROLE_IDS="1",
                     DATABASE_PATH=str(tmp_path / "b.db"), RCON_ENABLED="false").items():
        monkeypatch.setenv(k, v)
    cfg = load_config("/nonexistent")
    assert not cfg.rcon_enabled and cfg.rcon_host == "" and cfg.rcon_port == 0
    monkeypatch.setenv("RCON_ENABLED", "true")                       # on again: the details are needed
    import pytest
    with pytest.raises(ConfigError):
        load_config("/nonexistent")


def test_migrate_page_and_reset_by_the_platform_owner(bot, monkeypatch):  # noqa: F811
    env = bot.config.database_path.parent / ".env"
    env.write_text("RCON_HOST=1.1.1.1\nRCON_PORT=28016\nRCON_PASSWORD=first\n")
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_SHARED_BOT", "1")
    monkeypatch.setenv("SAAS_PORTAL_URL", "https://portal.example.com/")
    monkeypatch.setenv("SAAS_SERVER_ID", "12")

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        d = await (await c.get("/api/settings/migrate")).json()
        assert d["switch_discord_url"] == "https://portal.example.com/servers/12/discord"
        assert d["dashboard_url"] == "https://portal.example.com/dashboard" and d["guilds"] == []
        assert d["rcon"]["host"] == "1.1.1.1" and d["rcon"]["password_set"] and "first" not in str(d)
        await c.post("/api/settings/rcon-servers", json={"name": "Old"}, headers=H)
        # the platform owner resets RCON (control_plane.provisioning.reset_rcon writes this)
        env.write_text("RCON_HOST=\nRCON_PORT=\nRCON_PASSWORD=\nRCON_ENABLED=false\nRCON_RESET_AT=123\n")
        d = await (await c.get("/api/settings/migrate")).json()
        assert d["rcon"] == {**d["rcon"], "host": "", "enabled": False, "password_set": False}
        assert (await (await c.get("/api/settings/rcon-servers")).json())["servers"] == []    # old passwords gone
        assert (await c.get("/api/settings/migrate", headers={})).status == 200
    run(bot, scenario)


def test_hosted_panel_knows_the_customers_other_servers(bot, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_PORTAL_URL", "https://portal.example.com")
    monkeypatch.setenv("SAAS_SERVER_ID", "5")
    monkeypatch.setenv("SAAS_SIBLINGS", '[{"id": 5, "name": "EU"}, {"id": 8, "name": "US"}]')

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        me = await (await c.get("/api/me")).json()
        assert me["this_server"] == 5
        assert me["my_servers"][1] == {"id": 8, "name": "US", "url": "https://portal.example.com/servers/8/panel"}
    run(bot, scenario)


def test_turn_the_bot_off_and_on(bot, monkeypatch):  # noqa: F811
    env = bot.config.database_path.parent / ".env"
    env.write_text("RCON_HOST=1.1.1.1\nRCON_PORT=28016\nRCON_PASSWORD=pw\n")
    restarts = []
    monkeypatch.setattr(type(bot), "request_restart", lambda self: restarts.append(1))

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await (await c.get("/api/me")).json())["paused"] is False
        r = await c.post("/api/settings/power", json={"on": False}, headers=H)
        assert (await r.json())["changed"] and restarts == [1]
        assert "BOT_PAUSED=true" in env.read_text()
        assert (await (await c.get("/api/me")).json())["paused"] is True
        r = await c.post("/api/settings/power", json={"on": False}, headers=H)
        assert not (await r.json())["changed"] and restarts == [1]                  # already off
        await c.post("/api/settings/power", json={"on": True}, headers=H)
        assert "BOT_PAUSED=false" in env.read_text()
    run(bot, scenario)


def test_a_switched_off_bot_starts_the_panel_only(tmp_path, monkeypatch):
    import bot as botmod
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BOT_PAUSED=true\n")
    assert botmod.bot_paused()
    (tmp_path / ".env").write_text("BOT_PAUSED=false\n")
    assert not botmod.bot_paused()


def test_the_ai_cant_switch_the_bot_off():
    import web.server as ws
    assert "/api/settings/power".startswith(ws.AI_BLOCKED_WRITES)
