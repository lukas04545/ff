"""Hosted tenants: the host's pages and settings aren't in their panel, AI only with the add-on, and every server's
knowledge base (and what the AI learns from tickets) stays in its own folder."""
import asyncio
import os
import time
from pathlib import Path

from tests.test_control_plane import cp  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401


def _hosted(monkeypatch, ai: bool):
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_AI", "1" if ai else "0")
    monkeypatch.setenv("SAAS_MANAGER_AI", "0")


def test_no_ai_addon_no_ai_anything(bot, monkeypatch):  # noqa: F811
    _hosted(monkeypatch, ai=False)

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        me = await (await c.get("/api/me")).json()
        assert me["hosted"] is True
        assert {"servers", "makeplugin", "system", "knowledge"} <= set(me["hidden_views"])
        data = await (await c.get("/api/settings")).json()
        keys = {f["key"] for f in data["fields"]}
        assert not any(k.startswith(("AI_", "DEEPSEEK_", "TICKET_AI_", "KNOWLEDGE_")) for k in keys)
        assert not {"DISCORD_TOKEN", "WEB_PORT", "AUTO_UPDATE", "DATABASE_PATH"} & keys     # the host's settings
        assert "updates" not in {g["id"] for g in data["groups"]}
        assert "RCON_HOST" in keys                                                       # their own settings stay
        assert (await c.get("/api/knowledge")).status in (403, 404)
        assert (await c.get("/api/system")).status in (403, 404)
        await c.put("/api/settings", json={"values": {"AI_CHAT_NAME": "Sneaky"}}, headers=H)
        env = bot.config.database_path.parent / ".env"
        assert "Sneaky" not in (env.read_text() if env.exists() else "")              # can't switch it on either
    run(bot, scenario)


def test_with_the_ai_addon_the_ai_settings_are_there(bot, monkeypatch):  # noqa: F811
    _hosted(monkeypatch, ai=True)

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        me = await (await c.get("/api/me")).json()
        assert "knowledge" not in me["hidden_views"] and "servers" in me["hidden_views"]
        keys = {f["key"] for f in (await (await c.get("/api/settings")).json())["fields"]}
        assert "AI_CHAT_NAME" in keys and "TICKET_AI_LEARN" in keys
        assert "DEEPSEEK_API_KEY" not in keys                                            # the platform's key: hidden
        assert (await c.get("/api/knowledge")).status == 200
    run(bot, scenario)


def test_knowledge_and_learning_stay_in_each_servers_folder(tmp_path, monkeypatch):
    """The knowledge file is relative: every tenant process runs in its own folder (RCE_HOME), so it reads and the
    AI writes what it learns from tickets only there."""
    from config import Config  # noqa: F401
    from support import learning
    import config as cfg_module
    assert not Path(cfg_module.os.getenv("SUPPORT_KNOWLEDGE_FILE", "support_knowledge.md")).is_absolute()
    a, b = tmp_path / "c1" / "alpha", tmp_path / "c2" / "beta"
    for home, fact in ((a, "Wipe is on Thursday"), (b, "Wipe is on Monday")):
        home.mkdir(parents=True)
        monkeypatch.chdir(home)
        path = Path("support_knowledge.md")                          # what load_config gives a tenant
        learning.append_facts(path, [{"question": "When is wipe?", "answer": fact}], 1, max_chars=100_000)
    assert "Thursday" in (a / "support_knowledge.md").read_text() and "Monday" not in (a / "support_knowledge.md").read_text()
    assert "Monday" in (b / "support_knowledge.md").read_text() and "Thursday" not in (b / "support_knowledge.md").read_text()
    import saas_mode
    assert "SUPPORT_KNOWLEDGE_FILE" in saas_mode.LOCKED_SETTINGS                # a tenant can't point it elsewhere
    import bot as bot_module
    src = Path(bot_module.__file__).read_text()
    assert "os.chdir(os.environ.get(servers.ENV_HOME) or BASE_DIR)" in src     # each tenant runs in its own folder


def test_owner_panel_pages(cp):  # noqa: F811
    from control_plane.web import build_owner
    from web.totp import code_at, new_secret
    from web.users import hash_password
    from aiohttp.test_utils import TestClient, TestServer
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            await c.post("/login", data={"username": "boss", "password": "owner-password-123",
                                         "code": code_at(secret, int(time.time() // 30))})
            home = await (await c.get("/")).text()
            assert "Monthly revenue" in home and "class=group" not in home and "group pink" in home
            for path, text in (("/customers", "Customers"), ("/servers", "All servers"), ("/system", "Memory"),
                               ("/plans", "Packages"), ("/domains", "own domains")):
                page = await (await c.get(path)).text()
                assert text in page and "class=on" not in page or "class='on'" in page, path
        finally:
            await c.close()
    asyncio.run(scenario())
