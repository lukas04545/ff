"""Several Rust servers (servers.py, cogs/multiserver.py): adding servers, shared settings, the extra server's own
process and folder, copying kits and one admin abuse protection for every server."""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import servers
from envfile import read_env
from tests.test_web import H, bot, run  # noqa: F401

TOKEN_2 = "MTEx" + "a" * 66
TOKEN_3 = "MTEz" + "b" * 66


@pytest.fixture
def main_dir(tmp_path, monkeypatch):
    """A main server folder as cwd (servers.main_root()) with its .env."""
    monkeypatch.chdir(tmp_path)
    for key in (servers.ENV_HOME, servers.ENV_ID, servers.ENV_SHARED, servers.ENV_PARENT):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "DISCORD_TOKEN=main-token-" + "z" * 60 + "\nGUILD_ID=111\nWEB_PORT=8080\nWEB_PASSWORD=pbkdf2_sha256$1$aa$bb\n"
        "ABUSE_PROTECTION=true\nABUSE_ITEMS=rocket,c4\nABUSE_EXEMPT_PLAYERS=Boss\nABUSE_EXEMPT_ROLE_IDS=55\n"
        "AI_ABUSE_CHANNEL_ID=77\nADMIN_ROLE_IDS=9\nADMIN_USER_IDS=1234\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    return tmp_path


def test_adding_a_server_makes_its_own_folder_and_settings(main_dir):
    with pytest.raises(ValueError, match="name"):
        servers.create_server("", "1.2.3.4", "28016", "pw", TOKEN_2)
    with pytest.raises(ValueError, match="already runs another server"):
        servers.create_server("Two", "1.2.3.4", "28016", "pw", "main-token-" + "z" * 60)
    info = servers.create_server("2x Monthly!", "1.2.3.4", "28016", "rconpw", TOKEN_2, "111")
    assert info["id"] == "2x-monthly" and info["port"] == 8081 and info["rcon"] == "1.2.3.4:28016"
    env = read_env(main_dir / "servers" / "2x-monthly" / ".env")
    assert env["DISCORD_TOKEN"] == TOKEN_2 and env["RCON_PASSWORD"] == "rconpw" and env["WEB_PORT"] == "8081"
    assert env["AUTO_UPDATE"] == "false" and env["WEB_OPEN_BROWSER"] == "false"
    # shared: the abuse protection (same Discord: with its roles/channels) and the owner password
    assert env["ABUSE_ITEMS"] == "rocket,c4" and env["ABUSE_EXEMPT_ROLE_IDS"] == "55" and env["AI_ABUSE_CHANNEL_ID"] == "77"
    assert env["WEB_PASSWORD"] == "pbkdf2_sha256$1$aa$bb"
    assert env["ADMIN_ROLE_IDS"] == "9" and env["ADMIN_USER_IDS"] == "1234"     # same admins, starts right away
    with pytest.raises(ValueError, match="already runs another server"):
        servers.create_server("Three", "1.2.3.4", "28017", "pw", TOKEN_2)
    other = servers.create_server("Other Discord", "5.6.7.8", "28016", "pw", TOKEN_3, "999")
    env3 = read_env(main_dir / "servers" / other["id"] / ".env")
    assert other["port"] == 8082 and env3["ABUSE_ITEMS"] == "rocket,c4"
    assert env3.get("ADMIN_ROLE_IDS", "") == "" and env3["ADMIN_USER_IDS"] == "1234"
    assert env3.get("ABUSE_EXEMPT_ROLE_IDS", "") == "" and env3.get("AI_ABUSE_CHANNEL_ID", "") == ""  # other Discord
    assert [s["id"] for s in servers.all_servers()] == ["main", "2x-monthly", "other-discord"]
    # main changes its abuse settings -> copied over
    (main_dir / ".env").write_text((main_dir / ".env").read_text().replace("rocket,c4", "rocket"), encoding="utf-8")
    assert servers.sync_env("2x-monthly") == {"ABUSE_ITEMS": "rocket"}
    assert servers.sync_env("2x-monthly") == {}
    servers.remove_server("other-discord")
    assert [s["id"] for s in servers.load_registry()] == ["2x-monthly"]
    assert list((main_dir / "servers").glob("_removed-other-discord-*"))                   # folder kept


def test_extra_server_gets_a_clean_environment(main_dir, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "main-token-leaked")         # load_dotenv put the main's settings here
    monkeypatch.setenv("STATUS_CHANNEL_ID", "123")
    monkeypatch.setenv("WEB_PANEL_KNOCK", "secret phrase")
    monkeypatch.setenv("RCE_LAUNCHER", "1")
    servers.create_server("Two", "1.2.3.4", "28016", "pw", TOKEN_2)
    env = servers.child_environment("two")
    assert "DISCORD_TOKEN" not in env and "STATUS_CHANNEL_ID" not in env and "WEB_PANEL_KNOCK" not in env
    assert "RCE_LAUNCHER" not in env and "PATH" in env
    assert Path(env["RCE_HOME"]) == (main_dir / "servers" / "two").resolve()
    assert env["RCE_SERVER_ID"] == "two" and Path(env["RCE_SHARED_DATA"]) == (main_dir / "data").resolve()


def test_the_extra_server_runs_in_its_own_folder(main_dir, monkeypatch, tmp_path_factory):
    """A real process: `python bot.py` in servers/<id> with its environment; a settings error stops the restarts."""
    code = tmp_path_factory.mktemp("code")
    (code / "bot.py").write_text(
        "import json, os, sys\n"
        "open('seen.json', 'w').write(json.dumps({'cwd': os.getcwd(), 'id': os.environ.get('RCE_SERVER_ID'),"
        " 'token': os.environ.get('DISCORD_TOKEN')}))\n"
        "sys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(servers, "BASE_DIR", code)
    servers.create_server("Two", "1.2.3.4", "28016", "pw", TOKEN_2)
    manager = servers.ServerManager()
    manager.start_all()
    deadline = time.time() + 20
    while manager.info("two")["state"] != "error" and time.time() < deadline:
        time.sleep(0.1)
    assert manager.info("two")["state"] == "error" and manager.info("two")["last_exit"] == 3
    seen = json.loads((main_dir / "servers" / "two" / "seen.json").read_text())
    assert Path(seen["cwd"]).resolve() == (main_dir / "servers" / "two").resolve()
    assert seen["id"] == "two" and seen["token"] is None             # reads its own .env, not the main's
    manager.stop("two")
    assert servers.load_registry()[0]["enabled"] is False
    manager.stop_all()


def test_copying_kits_to_another_server(main_dir):
    from storage import Storage
    servers.create_server("Two", "1.2.3.4", "28016", "pw", TOKEN_2, "111")
    src = Storage(main_dir / "data" / "bot.db")
    src.create_custom_kit("Raider", "Boom", "owner")
    src.add_custom_kit_item("Raider", "explosive.timed", 2)
    src.add_custom_kit_item("Raider", "rifle.ak", 1)
    src.set_custom_kit_cooldown("Raider", 90)
    src.set_custom_kit_roles("Raider", [42])
    out = servers.copy_custom_kits(src, ["Raider", "Ghost"], "two")
    assert out == {"copied": ["Raider"], "skipped": [], "missing": ["Ghost"]}
    dst = Storage(main_dir / "servers" / "two" / "data" / "bot.db")
    kit = dst.custom_kit("Raider")
    assert kit["description"] == "Boom" and kit["cooldown_minutes"] == 90 and dst.claim_roles(kit) == [42]
    assert [(i["shortname"], i["amount"]) for i in dst.custom_kit_items("Raider")] == [("explosive.timed", 2),
                                                                                       ("rifle.ak", 1)]
    src.add_custom_kit_item("Raider", "ammo.rocket.basic", 4)
    assert servers.copy_custom_kits(src, ["Raider"], "two")["skipped"] == ["Raider"]     # kept unless overwrite
    assert len(dst.custom_kit_items("Raider")) == 2
    assert servers.copy_custom_kits(src, ["Raider"], "two", overwrite=True)["copied"] == ["Raider"]
    assert len(dst.custom_kit_items("Raider")) == 3
    with pytest.raises(ValueError):
        servers.copy_custom_kits(src, ["Raider"], "main")             # to itself
    with pytest.raises(ValueError):
        servers.copy_custom_kits(src, ["Raider"], "nope")
    src.close()
    dst.close()


def test_one_abuse_protection_for_every_server(main_dir, monkeypatch):
    """A punishment on one server removes the abuser's admin on the others."""
    from cogs.multiserver import MultiServer
    servers.create_server("Two", "1.2.3.4", "28016", "pw", TOKEN_2)
    ran, told = [], []

    class Guard:
        async def _run_all(self, templates, name):
            ran.append((tuple(templates), name))
            return [(t.format(name=name), "ok") for t in templates]

    def fake_bot():
        cfg = SimpleNamespace(abuse_protection=True, abuse_remove_admin_commands=('removeadmin "{name}"',))
        return SimpleNamespace(config=cfg, get_cog=lambda n: Guard() if n == "AbuseGuard" else None,
                               audit=told.append)
    on_main = MultiServer(fake_bot())
    monkeypatch.setenv(servers.ENV_ID, "two")
    monkeypatch.setenv(servers.ENV_SHARED, str(main_dir / "data"))
    on_two = MultiServer(fake_bot())
    on_two.broadcast_abuse("BadAdmin", "3 × rocket")
    assert on_two.read_new() == []                                  # its own event
    monkeypatch.delenv(servers.ENV_ID)
    monkeypatch.delenv(servers.ENV_SHARED)
    events = on_main.read_new()
    assert [e["player"] for e in events] == ["BadAdmin"] and events[0]["origin"] == "two"
    asyncio.run(on_main.handle(events[0]))
    assert ran == [(('removeadmin "{name}"',), "BadAdmin")]
    assert "Two" in told[0] and "admin removed on this server too" in told[0]
    assert on_main.read_new() == []                                 # handled once


def test_servers_api_and_copying_kits_from_the_panel(bot, monkeypatch):  # noqa: F811
    tmp = bot.config.database_path.parent
    monkeypatch.chdir(tmp)
    (tmp / ".env").write_text("WEB_PORT=8080\nGUILD_ID=1\n", encoding="utf-8")

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        d = await (await c.get("/api/servers")).json()
        assert d["current"] == "main" and [s["id"] for s in d["servers"]] == ["main"] and d["manage"] is False
        r = await c.post("/api/servers", json={"name": "Two"}, headers=H)
        assert r.status == 400 and "main server" in (await r.json())["error"]       # no manager in this process
        monkeypatch.setattr(servers, "manager", SimpleNamespace(start=lambda sid: None, info=lambda sid: {
            "state": "running"}))
        r = await c.post("/api/servers", json={"name": "Two", "rcon_host": "1.2.3.4", "rcon_port": "28016",
                                               "rcon_password": "pw", "discord_token": TOKEN_2}, headers=H)
        assert r.status == 200, await r.text()
        d = await (await c.get("/api/servers")).json()
        assert [s["name"] for s in d["servers"]] == ["Main server", "Two"] and d["servers"][1]["state"] == "running"
        assert "rconpw" not in json.dumps(d) and TOKEN_2 not in json.dumps(d)            # never secrets
        bot.db.create_custom_kit("Starter", None, "owner")
        bot.db.add_custom_kit_item("Starter", "wood", 1000)
        r = await c.post("/api/kits/copy", json={"kits": ["Starter"], "target": "two"}, headers=H)
        assert (await r.json())["copied"] == ["Starter"]
        from storage import Storage
        dst = Storage(tmp / "servers" / "two" / "data" / "bot.db")
        assert [i["shortname"] for i in dst.custom_kit_items("Starter")] == ["wood"]
        dst.close()
    run(bot, scenario)


def test_extra_server_takes_the_shared_settings_from_the_main(bot, monkeypatch):  # noqa: F811
    tmp = bot.config.database_path.parent
    monkeypatch.setenv(servers.ENV_ID, "two")
    monkeypatch.setenv(servers.ENV_SHARED, str(tmp / "main" / "data"))

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        fields = {f["key"]: f for f in (await (await c.get("/api/settings")).json())["fields"]}
        assert "main server" in fields["ABUSE_ITEMS"]["locked"] and "main server" in fields["WEB_PASSWORD"]["locked"]
        assert "locked" not in fields["RCON_HOST"]
        r = await c.put("/api/settings", json={"values": {"ABUSE_ITEMS": "nothing", "GREETING": ""}}, headers=H)
        assert r.status == 200 and "ABUSE_ITEMS" not in read_env(tmp / ".env")          # ignored here
        r = await c.post("/api/servers/two/start", headers=H)
        assert r.status == 400                                                        # managed on the main
        from cogs.updates import Updates
        updates = Updates(bot)
        assert updates.follower and (await updates.check_now()).status == "skipped"      # the main updates
    run(bot, scenario)


def test_other_panels_open_through_the_main_address(bot, monkeypatch):  # noqa: F811
    """/switch/<id>: the main server passes that server's panel through – one address (domain) for every panel."""
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestServer
    tmp = bot.config.database_path.parent
    monkeypatch.chdir(tmp)
    (tmp / ".env").write_text("WEB_PORT=8080\nGUILD_ID=1\n", encoding="utf-8")
    seen = []

    async def child_app(request):                      # stands in for the extra server's web interface
        seen.append({"path": request.path_qs, "host": request.host, "cf": request.headers.get("CF-Connecting-IP"),
                     "body": await request.text(), "method": request.method})
        resp = aioweb.Response(text=f"child {request.path}", headers={"X-Child": "1"})
        resp.set_cookie("rce_gate", "child-gate")
        return resp

    async def scenario(c):
        child = aioweb.Application()
        child.router.add_route("*", "/{tail:.*}", child_app)
        child_server = TestServer(child)
        await child_server.start_server()
        servers.create_server("Two", "1.2.3.4", "28016", "pw", TOKEN_2)
        update = {"WEB_PORT": str(child_server.port), "WEB_PANEL_PATH": "panel-two-secret"}
        from envfile import update_env
        update_env(servers.env_path("two"), update)
        OUT = {"CF-Connecting-IP": "203.0.113.5"}
        assert (await c.get("/switch/two", headers=OUT, allow_redirects=False)).status == 404    # not logged in
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await c.get("/switch/two", headers=OUT, allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/panel-two-secret"
        assert "rce_server=two" in r.headers["Set-Cookie"]
        r = await c.get("/panel-two-secret", headers=OUT)
        assert r.status == 200 and await r.text() == "child /panel-two-secret" and r.headers["X-Child"] == "1"
        assert seen[-1]["host"] == f"127.0.0.1:{child_server.port}" and seen[-1]["cf"] == "203.0.113.5"
        r = await c.post("/api/kits/give?x=1", json={"kit": "a"}, headers={**H, **OUT})
        assert await r.text() == "child /api/kits/give" and seen[-1]["path"] == "/api/kits/give?x=1"
        assert seen[-1]["method"] == "POST" and json.loads(seen[-1]["body"]) == {"kit": "a"}
        assert (await c.get("/static/app.js", headers=OUT)).headers.get("X-Child") == "1"
        n = len(seen)
        await c.get("/", headers=OUT)                                          # the player website stays the main's
        assert len(seen) == n
        await c.get("/switch/main", headers=OUT, allow_redirects=False)        # back: the main's own panel
        r = await c.get("/api/me", headers=OUT)
        assert r.status == 200 and (await r.json())["user"] == "owner" and len(seen) == n
        await c.get("/switch/two", headers=OUT, allow_redirects=False)
        await child_server.close()                                             # the other server stopped
        r = await c.get("/api/status", headers=OUT)
        assert r.status == 502 and "Two" in (await r.json())["error"]
        r = await c.get("/panel-two-secret", headers=OUT)
        assert r.status == 502 and "/switch/main" in await r.text()
    run(bot, scenario)
