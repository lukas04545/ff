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
