"""SaaS add-ons: the shared bot (guild fence + secure "Add to Discord"), own bot as an owner-unlocked add-on, AI as an
owner-unlocked add-on, and updates only from the owner panel."""
import asyncio
import re
import time
from types import SimpleNamespace as NS

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord.http import Route

import saas_fence
from control_plane import features, provisioning, updates
from tests.test_control_plane import TOKEN, FakeLauncher, cp, csrf_of, customer, paid  # noqa: F401

GUILD = 111222333444555666


# ------------------------------------------------------------ The fence (bot side)

def test_fence_drops_other_guilds_events_including_buttons_and_commands():
    seen = []
    parsers = {n: (lambda data, n=n: seen.append((n, data))) for n in
               ("READY", "GUILD_CREATE", "MESSAGE_CREATE", "INTERACTION_CREATE", "USER_UPDATE", "TYPING_START")}
    saas_fence.wrap_parsers(parsers, GUILD)
    parsers["READY"]({"guilds": [{"id": str(GUILD)}, {"id": "999"}]})
    parsers["GUILD_CREATE"]({"id": "999"})                                   # another tenant's guild
    parsers["GUILD_CREATE"]({"id": str(GUILD)})
    parsers["MESSAGE_CREATE"]({"guild_id": "999", "content": "secret of another tenant"})
    parsers["INTERACTION_CREATE"]({"guild_id": "999", "type": 3, "data": {"custom_id": "codechange:keep"}})
    parsers["INTERACTION_CREATE"]({"guild_id": str(GUILD), "type": 2})
    parsers["MESSAGE_CREATE"]({"content": "a DM to the shared bot"})         # no guild: dropped
    parsers["USER_UPDATE"]({"id": "1"})
    names = [n for n, _ in seen]
    assert names == ["READY", "GUILD_CREATE", "INTERACTION_CREATE", "USER_UPDATE"]
    assert seen[0][1]["guilds"] == [{"id": str(GUILD)}]
    assert not any("secret" in str(d) for _, d in seen)


def test_fence_refuses_requests_outside_the_guild():
    mine = NS(guild=NS(id=GUILD))
    theirs = NS(guild=NS(id=999))
    dm = discord.DMChannel.__new__(discord.DMChannel)
    channels = {1: mine, 2: theirs, 3: dm}
    bot = NS(get_channel=channels.get)
    ok = lambda r: saas_fence.allowed(r, bot, GUILD)   # noqa: E731
    assert ok(Route("POST", "/channels/{channel_id}/messages", channel_id=1))
    assert not ok(Route("POST", "/channels/{channel_id}/messages", channel_id=2))      # another tenant's channel
    assert not ok(Route("POST", "/channels/{channel_id}/messages", channel_id=42))     # unknown channel
    assert ok(Route("POST", "/channels/{channel_id}/messages", channel_id=3))          # a DM it opened
    assert ok(Route("GET", "/guilds/{guild_id}/members/{user_id}", guild_id=GUILD, user_id=5))
    assert not ok(Route("PUT", "/guilds/{guild_id}/bans/{user_id}", guild_id=999, user_id=5))
    assert not ok(Route("PUT", "/applications/{application_id}/commands", application_id=7))   # global commands
    assert ok(Route("PUT", "/applications/{application_id}/guilds/{guild_id}/commands", application_id=7,
                    guild_id=GUILD))
    assert not ok(Route("GET", "/users/@me/guilds"))


def test_fence_install(monkeypatch):
    sent = []

    async def request(route, **kw):
        sent.append(route.path)
        return {}
    bot = NS(_connection=NS(parsers={"MESSAGE_CREATE": lambda d: None}), http=NS(request=request),
             get_channel=lambda cid: None)
    assert saas_fence.install(bot) is False                                   # not hosted: nothing changes
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_SHARED_BOT", "1")
    with pytest.raises(RuntimeError):
        saas_fence.install(bot)                                               # never unfenced
    monkeypatch.setenv("SAAS_GUILD_ID", str(GUILD))
    assert saas_fence.install(bot) is True
    with pytest.raises(saas_fence.FenceError):
        asyncio.run(bot.http.request(Route("DELETE", "/guilds/{guild_id}", guild_id=999)))
    asyncio.run(bot.http.request(Route("GET", "/guilds/{guild_id}", guild_id=GUILD)))
    assert sent == ["/guilds/{guild_id}"]


def test_hosted_config_pins_token_guild_and_ai(tmp_path, monkeypatch):
    from config import load_config
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=from-the-dotenv\nGUILD_ID=999\nDEEPSEEK_API_KEY=own-key\nRCON_HOST=1.2.3.4\n"
                   "RCON_PORT=28016\nRCON_PASSWORD=pw\nADMIN_ROLE_IDS=1\n", encoding="utf-8")
    for k in ("SAAS_AI", "SAAS_GUILD_ID", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("DISCORD_TOKEN", "from-the-control-plane")
    monkeypatch.setenv("SAAS_GUILD_ID", str(GUILD))
    cfg = load_config(env)
    assert cfg.discord_token == "from-the-control-plane" and cfg.guild_id == GUILD
    assert cfg.deepseek_api_key == ""                                          # AI add-on off
    monkeypatch.setenv("SAAS_AI", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "platform-key")
    assert load_config(env).deepseek_api_key == "platform-key"                 # AI add-on on
    import saas_mode
    monkeypatch.setenv("SAAS_SHARED_BOT", "1")
    assert {"GUILD_ID", "DEEPSEEK_API_KEY"} <= saas_mode.locked_settings()
    assert saas_mode.blocked("POST", "/api/update/check") and saas_mode.blocked("POST", "/api/update")


# ------------------------------------------------------------ Shared bot vs own bot (control plane)

def test_shared_bot_by_default_own_bot_only_as_addon(cp):
    cp.settings.shared_bot_token = "SHARED." + "x" * 40
    cid = customer(cp, own_bot=False)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    with pytest.raises(provisioning.ProvisionError, match="add-on"):
        provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="customer")
    assert provisioning.runnable(cp.db, s["id"], cp.settings)                  # not in a Discord yet: panel only
    env = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert env["DISCORD_TOKEN"] == "" and env["SAAS_SHARED_BOT"] == "0" and env["SAAS_WAITING_DISCORD"] == "1"
    assert "SAAS_GUILD_ID" not in env                                          # no Discord connection at all
    provisioning.bind_guild(cp.db, s["id"], str(GUILD), actor="customer")
    assert provisioning.runnable(cp.db, s["id"], cp.settings)
    env = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert env["DISCORD_TOKEN"] == cp.settings.shared_bot_token and env["SAAS_SHARED_BOT"] == "1"
    assert env["SAAS_GUILD_ID"] == str(GUILD) and env["SAAS_AI"] == "0" and env["DEEPSEEK_API_KEY"] == ""
    assert "SAAS_WAITING_DISCORD" not in env                                   # the bot starts (env changed)
    other = customer(cp, "b@x.io", own_bot=False)
    s2 = provisioning.provision_server(cp.db, cp.box, cp.settings, other, "Theirs", actor="t")
    with pytest.raises(provisioning.ProvisionError, match="already connected"):
        provisioning.bind_guild(cp.db, s2["id"], str(GUILD), actor="customer")   # can't take another's Discord
    # the owner switches the add-ons on
    cp.settings.deepseek_api_key = "platform-deepseek"
    features.grant(cp.db, cid, "ai", owner="me")
    features.grant(cp.db, cid, "custom_bot", owner="me")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="customer")
    env = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert env["DISCORD_TOKEN"] == TOKEN and env["SAAS_SHARED_BOT"] == "0" and "SAAS_GUILD_ID" not in env
    assert env["SAAS_AI"] == "1" and env["DEEPSEEK_API_KEY"] == "platform-deepseek"
    provisioning.bind_guild(cp.db, s2["id"], str(GUILD), actor="customer")      # the guild is free again
    # switching the own-bot add-on off: back to the shared bot, token gone, nothing deleted
    assert features.revoke(cp.db, cid, "custom_bot", owner="me") == [s["id"]]
    row = cp.db.one("SELECT bot_mode, status FROM servers WHERE id = ?", (s["id"],))
    assert tuple(row) == ("shared", "awaiting_token")
    assert provisioning.get_secret(cp.db, cp.box, s["id"], "discord_token") is None


def test_running_servers_restart_when_an_addon_changes(cp, monkeypatch):
    from control_plane import supervisor as sup_mod
    from control_plane.supervisor import Supervisor
    monkeypatch.setattr(sup_mod.os, "killpg", lambda pid, sig: None)
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")

    async def main():
        sup = Supervisor(cp.db, cp.box, cp.settings, FakeLauncher())
        await sup.reconcile()
        assert len(sup.launcher.started) == 1
        await sup.reconcile()
        assert len(sup.launcher.started) == 1                                   # nothing changed: no restart
        features.grant(cp.db, cid, "ai", owner="me")
        await sup.reconcile()
        assert len(sup.launcher.started) == 2                                   # AI on -> restarted with it
        assert await sup.rolling_restart(gap=0) == [s["id"]] and len(sup.launcher.started) == 3
    asyncio.run(main())


# ------------------------------------------------------------ Portal: "Add to Discord" (verified by Discord)

def test_add_to_discord_binds_only_the_server_discord_confirms(cp, monkeypatch):
    from control_plane import web as cpweb
    cp.settings.shared_client_id = "424242"
    confirmed = {"guild": str(GUILD)}

    async def exchange(settings, code):
        assert code == "the-code"
        return confirmed["guild"]
    monkeypatch.setattr(cpweb, "exchange_code", exchange)

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        b = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        await b.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            await a.post("/servers", data={"name": "One", "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            page = await (await a.get("/dashboard")).text()
            assert "Add the bot to your Discord" in page and "Use my own bot" not in page   # no add-on
            sid = cp.db.one("SELECT id FROM servers")["id"]
            r = await a.get(f"/servers/{sid}/discord", allow_redirects=False)
            loc = r.headers["Location"]
            assert loc.startswith("https://discord.com/oauth2/authorize?") and "client_id=424242" in loc
            state = re.search(r"state=([^&]+)", loc)[1].replace("%2E", ".")
            # another customer can't use this link (the state is tied to the customer)
            await b.post("/signup", data={"email": "b@x.io", "password": "password1234"})
            r = await b.get(f"/discord/callback?code=the-code&state={state}", allow_redirects=False)
            assert "expired" in r.headers["Location"] and cp.db.one("SELECT guild_id FROM servers")["guild_id"] is None
            r = await a.get(f"/discord/callback?code=the-code&state={state[:-2]}xx", allow_redirects=False)
            assert "expired" in r.headers["Location"]                                    # tampered
            await a.get(f"/discord/callback?code=the-code&state={state}&guild_id=999")    # URL guild ignored
            assert cp.db.one("SELECT guild_id, status FROM servers")["guild_id"] == str(GUILD)
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            t = csrf_of(await (await a.get("/dashboard")).text())
            r = await a.post(f"/servers/{sid}/token", data={"token": TOKEN, "csrf": t}, allow_redirects=False)
            assert "add-on" in r.headers["Location"]                                     # own bot: add-on only
            features.grant(cp.db, cid, "custom_bot", owner="me")
            assert "Use my own bot" in await (await a.get("/dashboard")).text()
        finally:
            await a.close()
            await b.close()
    asyncio.run(scenario())


# ------------------------------------------------------------ Owner panel: add-ons and updates

def test_owner_switches_addons_and_installs_updates(cp, monkeypatch):
    from web.totp import code_at, new_secret
    from web.users import hash_password
    from control_plane.web import build_owner
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))
    cid = customer(cp, own_bot=False)

    class FakeUpdater:
        def current_version(self):
            return {"sha": "aaaaaaaaaa", "title": "old"}

        def latest_commit(self):
            return "bbbbbbbbbb", "new version"

        def check(self):
            return NS(status="updated", message="", sha="bbbbbbbbbb", title="new version")
    monkeypatch.setattr(updates, "_updater", lambda settings: FakeUpdater())
    restarted = []

    class Sup:
        procs = {}

        async def rolling_restart(self):
            restarted.append(1)
            return [1, 2]

        async def restart(self, sid):
            pass

    async def changed():
        pass

    async def scenario():
        owner = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed, Sup())))
        await owner.start_server()
        try:
            await owner.post("/login", data={"username": "boss", "password": "owner-password-123",
                                             "code": code_at(secret, int(time.time() // 30))})
            t = csrf_of(await (await owner.get(f"/customers/{cid}")).text())
            await owner.post(f"/customers/{cid}/feature/ai/on", data={"csrf": t})
            await owner.post(f"/customers/{cid}/feature/custom_bot/on", data={"csrf": t})
            assert features.active(cp.db, cid) == {"ai", "custom_bot"}
            await owner.post(f"/customers/{cid}/feature/ai/off", data={"csrf": t})
            assert features.active(cp.db, cid) == {"custom_bot"}
            assert (await owner.post(f"/customers/{cid}/feature/ai/on", data={})).status == 403   # CSRF
            await owner.post("/update/check", data={"csrf": t})
            page = await (await owner.get("/")).text()
            assert "update available" in page and "new version" in page
            await owner.post("/update/install", data={"csrf": t})
            page = await (await owner.get("/")).text()
            assert restarted == [1] and "2 server(s) restarted" in page and "still runs the old code" in page
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_customer_can_disconnect_the_bot_from_their_discord(cp, monkeypatch):
    from control_plane import web as cpweb
    left = []

    async def leave(token, guild_id):
        left.append((token, guild_id))
        return True
    monkeypatch.setattr(provisioning, "leave_guild", leave)
    cp.settings.shared_bot_token = "shared-token"

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            await a.post("/servers", data={"name": "One", "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            sid = cp.db.one("SELECT id FROM servers")["id"]
            assert "Disconnect from Discord" not in await (await a.get("/dashboard")).text()  # not connected yet
            cp.db.run("UPDATE servers SET status = 'active' WHERE id = ?", (sid,))
            provisioning.bind_guild(cp.db, sid, str(GUILD), actor="t")
            page = await (await a.get("/dashboard")).text()
            assert "Disconnect from Discord" in page
            r = await a.post(f"/servers/{sid}/discord/disconnect", data={"csrf": csrf_of(page), "leave": "1"},
                             allow_redirects=False)
            assert "disconnected" in r.headers["Location"] and "left" in r.headers["Location"]
            row = cp.db.one("SELECT guild_id, status FROM servers WHERE id = ?", (sid,))
            assert row["guild_id"] is None and row["status"] == "awaiting_token"         # the panel keeps running
            assert left == [("shared-token", str(GUILD))]
            assert provisioning.environment(cp.db, cp.box, cp.settings, sid)["DISCORD_TOKEN"] == ""
            # again: nothing connected any more
            r = await a.post(f"/servers/{sid}/discord/disconnect", data={"csrf": csrf_of(page)}, allow_redirects=False)
            assert "isn" in r.headers["Location"] and len(left) == 1
            assert cp.db.one("SELECT 1 FROM audit_log WHERE action = 'server.discord_disconnected'")
        finally:
            await a.close()
    asyncio.run(scenario())


def test_own_bot_disconnect_deletes_its_token(cp):  # noqa: F811
    cid = customer(cp)
    paid(cp, cid)
    features.grant(cp.db, cid, "custom_bot", owner="me")
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")
    out = provisioning.disconnect_discord(cp.db, cp.box, s["id"], actor="t")
    assert out["token"] == TOKEN and provisioning.get_secret(cp.db, cp.box, s["id"], "discord_token") is None
    assert cp.db.one("SELECT status FROM servers WHERE id = ?", (s["id"],))["status"] == "awaiting_token"


def test_switching_to_another_discord_leaves_the_old_one(cp, monkeypatch):
    from control_plane import web as cpweb
    cp.settings.shared_client_id, cp.settings.shared_bot_token = "424242", "shared-token"
    guilds, left = iter([str(GUILD), "777000777000777000"]), []

    async def exchange(settings, code):
        return next(guilds)

    async def leave(token, guild_id):
        left.append(guild_id)
        return True
    monkeypatch.setattr(cpweb, "exchange_code", exchange)
    monkeypatch.setattr(provisioning, "leave_guild", leave)

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            await a.post("/servers", data={"name": "One", "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            sid = cp.db.one("SELECT id FROM servers")["id"]
            for _ in range(2):
                loc = (await a.get(f"/servers/{sid}/discord", allow_redirects=False)).headers["Location"]
                state = re.search(r"state=([^&]+)", loc)[1].replace("%2E", ".")
                r = await a.get(f"/discord/callback?code=c&state={state}", allow_redirects=False)
                if not left:
                    assert "Bot+added" in r.headers["Location"]
                    assert "Switch to another Discord" in await (await a.get("/dashboard")).text()
            assert "Switched" in r.headers["Location"] and left == [str(GUILD)]
            assert cp.db.one("SELECT guild_id FROM servers")["guild_id"] == "777000777000777000"
        finally:
            await a.close()
    asyncio.run(scenario())


def test_owner_resets_a_customers_rcon(cp):  # noqa: F811
    from envfile import read_env
    from pathlib import Path
    from control_plane import actions
    cid = customer(cp)
    paid(cp, cid)
    s = dict(provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t"))
    s = dict(cp.db.one("SELECT * FROM servers WHERE id = ?", (s["id"],)))
    provisioning.write_env(Path(s["home_dir"]), {"RCON_HOST": "1.1.1.1", "RCON_PORT": "28016", "RCON_PASSWORD": "pw"})
    out = asyncio.run(actions.server(cp.db, cp.settings, None, s["id"], "rcon-reset", actor="owner:boss"))
    assert "fresh" in out
    env = read_env(Path(s["home_dir"]) / ".env")
    assert env["RCON_HOST"] == "" and env["RCON_PASSWORD"] == "" and env["RCON_ENABLED"] == "false"
    assert env["RCON_RESET_AT"]
    assert cp.db.one("SELECT 1 FROM audit_log WHERE action = 'server.rcon_reset'")
    e = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert e["SAAS_SERVER_ID"] == str(s["id"]) and e["SAAS_PORTAL_URL"] == cp.settings.portal_url


def test_customer_switches_between_their_panels(cp, monkeypatch):
    import json as _json
    from control_plane import slots
    from control_plane import web as cpweb
    import saas_mode

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            paid(cp, cid)
            slots.grant(cp.db, cid, 1, owner="boss")
            for name in ("One", "Two"):
                await a.post("/servers", data={"name": name, "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            one, two = [r["id"] for r in cp.db.q("SELECT id FROM servers ORDER BY id")]
            cp.db.run("UPDATE servers SET status = 'active'")
            env = provisioning.environment(cp.db, cp.box, cp.settings, one)
            assert [x["name"] for x in _json.loads(env["SAAS_SIBLINGS"])] == ["One", "Two"]
            assert f"/servers/{two}/panel" in await (await a.get("/dashboard")).text()
            r = await a.get(f"/servers/{two}/panel", allow_redirects=False)
            assert r.status == 200
            token = re.search(r"/saas/login\?t=([^\"&]+)", await r.text())[1]
            monkeypatch.setenv("RCE_SAAS", "1")
            monkeypatch.setenv("SAAS_MANAGE_TOKEN", provisioning.manage_token(
                provisioning.get_secret(cp.db, cp.box, two, "health_token")))
            assert saas_mode.check_owner_login(token) == f"customer:{cid}"          # valid for THAT server's panel
            # someone else's server: no
            b = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
            await b.start_server()
            await b.post("/signup", data={"email": "b@x.io", "password": "password1234"})
            assert (await b.get(f"/servers/{two}/panel?r=1", allow_redirects=False)).status == 404
            # logged out: one same-site hop, then the login page
            await a.close()
            c = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
            await c.start_server()
            r = await c.get(f"/servers/{two}/panel", allow_redirects=False)
            assert r.status == 200 and "r=1" in await r.text()
            r = await c.get(f"/servers/{two}/panel?r=1", allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("/login")
            await b.close()
            await c.close()
        finally:
            await a.close()
    asyncio.run(scenario())


def test_customer_app_overview_and_login_return(cp):  # noqa: F811
    from control_plane import slots
    from control_plane import web as cpweb

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            r = await a.get("/app", allow_redirects=False)
            assert r.status == 303 and r.headers["Location"] == "/login?next=/app"       # log in first
            assert 'name=next value=/app' in await (await a.get("/login?next=/app")).text()
            assert "name=next" not in await (await a.get("/login?next=https://evil.example")).text()
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            paid(cp, cid)
            slots.grant(cp.db, cid, 1, owner="boss")
            await a.post("/servers", data={"name": "EU main", "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            sid = cp.db.one("SELECT id FROM servers")["id"]
            cp.db.run("UPDATE servers SET status = 'active'")
            page = await (await a.get("/app")).text()
            assert "EU main" in page and f"/servers/{sid}/panel" in page and "Full dashboard" in page
            assert "rcepanel://add?kind=shop" in await (await a.get("/dashboard")).text()
            a.session.cookie_jar.clear()
            r = await a.post("/login", data={"email": "a@x.io", "password": "password1234", "next": "/app"},
                             allow_redirects=False)
            assert r.headers["Location"] == "/app"
            r = await a.post("/login", data={"email": "a@x.io", "password": "password1234",
                                             "next": "https://evil.example"}, allow_redirects=False)
            assert r.headers["Location"] == "/dashboard"                                 # no open redirect
        finally:
            await a.close()
    asyncio.run(scenario())
