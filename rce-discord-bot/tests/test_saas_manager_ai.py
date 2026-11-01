"""Management AI in hosted mode: always for the owner (owner panel → the server's own bot), in a server's Discord only
where the owner switched it on."""
import asyncio
import re
import time
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from control_plane import provisioning
from tests.test_control_plane import TOKEN, cp, csrf_of, customer, paid  # noqa: F401
from tests.test_manager_ai import _call, env, mgr  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401


def test_owner_runs_it_without_discord_and_decides_on_risky_tools(mgr, monkeypatch):
    from cogs.manager_ai import ManagerAI
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_OWNER_AI_KEY", "platform-key")
    script = []

    async def chat(messages, **kw):
        assert any(m["content"].startswith("Platform owner:") for m in messages if m["role"] == "user")
        return script.pop(0) if script else {"content": "Done.", "tool_calls": []}
    monkeypatch.setattr(ManagerAI, "ai", property(lambda self: NS(chat=chat)))
    script += [{"content": "", "tool_calls": [_call("run_console_command", command="env.time 12")]},
               {"content": "Not allowed.", "tool_calls": []}]
    out = asyncio.run(mgr.cog.run_for_owner("set the time to noon", allow_risky=False))
    assert out["ok"] and "env.time 12" not in mgr.sent and out["asked"][0].startswith("✖️")
    script += [{"content": "", "tool_calls": [_call("run_console_command", command="env.time 12")]},
               {"content": "Time set.", "tool_calls": []}]
    out = asyncio.run(mgr.cog.run_for_owner("set the time to noon", [{"role": "user", "content": "hi"}],
                                            allow_risky=True))
    assert out["answer"] == "Time set." and "env.time 12" in mgr.sent and out["done"]
    assert "edit_code_file" not in [t["function"]["name"] for t in mgr.cog.tools(NS(id=0))]   # never code in SaaS


def test_discord_channel_only_on_chosen_servers(mgr, monkeypatch):
    import saas_mode
    from cogs.manager_ai import ManagerAI
    asked = []

    async def chat(messages, **kw):
        asked.append(1)
        return {"content": "ok", "tool_calls": []}
    monkeypatch.setattr(ManagerAI, "ai", property(lambda self: NS(chat=chat)))
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGER_AI", "0")
    asyncio.run(mgr.cog.on_message(mgr.message("list the kits")))
    assert asked == [] and mgr.replies == []                                    # not chosen: silent
    assert saas_mode.MANAGER_KEYS <= saas_mode.locked_settings()               # and its settings are locked
    monkeypatch.setenv("SAAS_MANAGER_AI", "1")
    asyncio.run(mgr.cog.on_message(mgr.message("list the kits")))
    assert asked == [1] and not (saas_mode.MANAGER_KEYS & saas_mode.locked_settings())


def test_manage_endpoint_needs_its_token(bot, monkeypatch):  # noqa: F811
    from cogs.manager_ai import ManagerAI
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGE_TOKEN", "m" * 64)
    calls = []

    async def scenario(c):
        cog = ManagerAI(bot)
        await bot.add_cog(cog)

        async def fake(text, history=None, *, allow_risky=False):
            calls.append((text, allow_risky))
            return {"ok": True, "answer": "done"}
        cog.run_for_owner = fake
        assert (await c.post("/saas/manage", json={"message": "x"})).status == 404
        assert (await c.post("/saas/manage", json={"message": "x"},
                             headers={"Authorization": "Bearer wrong"})).status == 404
        r = await c.post("/saas/manage", json={"message": "list binds", "allow_risky": True},
                         headers={"Authorization": "Bearer " + "m" * 64})
        assert (await r.json())["answer"] == "done" and calls == [("list binds", True)]
    run(bot, scenario)


def test_control_plane_switch_and_owner_chat(cp, monkeypatch):
    from web.totp import code_at, new_secret
    from web.users import hash_password
    from control_plane import manage_ai
    from control_plane.web import build_owner
    cp.settings.deepseek_api_key = "platform-key"
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")
    env_ = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert env_["SAAS_MANAGER_AI"] == "0" and env_["SAAS_OWNER_AI_KEY"] == "platform-key"
    assert env_["SAAS_MANAGE_TOKEN"] == provisioning.manage_token(env_["SAAS_HEALTH_TOKEN"])
    assert env_["SAAS_MANAGE_TOKEN"] != env_["SAAS_HEALTH_TOKEN"] and env_["SAAS_AI"] == "0"
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))
    asked = []

    async def fake_ask(db, box, server_id, message, history, *, allow_risky):
        asked.append((server_id, message, allow_risky, len(history)))
        return {"ok": True, "answer": f"answer {len(asked)}", "done": ["🎭 2 emote bind(s)"], "asked": []}
    monkeypatch.setattr(manage_ai, "ask", fake_ask)

    async def changed():
        pass

    async def scenario():
        owner = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await owner.start_server()
        try:
            await owner.post("/login", data={"username": "boss", "password": "owner-password-123",
                                             "code": code_at(secret, int(time.time() // 30))})
            t = csrf_of(await (await owner.get("/")).text())
            await owner.post(f"/servers/{s['id']}/manager-ai/on", data={"csrf": t})
            assert provisioning.environment(cp.db, cp.box, cp.settings, s["id"])["SAAS_MANAGER_AI"] == "1"
            page = await (await owner.get(f"/servers/{s['id']}/ai")).text()
            t = csrf_of(page)
            await owner.post(f"/servers/{s['id']}/ai", data={"csrf": t, "message": "bind kits"})
            await owner.post(f"/servers/{s['id']}/ai", data={"csrf": t, "message": "now roles", "allow_risky": "1"})
            page = await (await owner.get(f"/servers/{s['id']}/ai")).text()
            assert "answer 2" in page and "emote bind" in page
            assert asked == [(s["id"], "bind kits", False, 0), (s["id"], "now roles", True, 2)]
            assert (await owner.post(f"/servers/{s['id']}/ai", data={"message": "x"})).status == 403   # CSRF
            audit = await (await owner.get("/audit")).text()
            assert "manager_ai.request" in audit and "server.manager_ai.on" in audit
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_health_says_why_a_server_waits_in_setup_mode(tmp_path, monkeypatch):
    from web.server import WebInterface, WebSettings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_HEALTH_TOKEN", "h" * 64)

    async def scenario():
        w = WebInterface(None, web_settings=WebSettings(password="pw"), setup_error="Configuration incomplete: "
                         "RCON_PORT missing", env_path=tmp_path / ".env", secret_path=tmp_path / "s.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            data = await (await c.get("/saas/health", headers={"Authorization": "Bearer " + "h" * 64})).json()
            assert data["setup_mode"] is True and "RCON_PORT missing" in data["error"]
        finally:
            await c.close()
    asyncio.run(scenario())
