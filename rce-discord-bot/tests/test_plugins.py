"""Plugin system: discovery, on/off, settings, own plugin files, broken plugins, web API and public pages."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

H = {"X-Requested-With": "rce-web"}
DEFAULT_ON = {"starter_kit", "lowpop_bonus", "early_bird", "bounties", "weekly_recap", "achievements", "wipe_poll"}
ALL = DEFAULT_ON | {"invites", "public_page", "filling_ping", "comeback"}

HELLO = '''
from plugin_api import Plugin, Setting

class Hello(Plugin):
    NAME = "Hello"
    DESCRIPTION = "Says hello."
    SETTINGS = (Setting("text", "Message", default="Hi {player}!"), Setting("times", "Times", "int", 1, min=1, max=3))

    @Plugin.listener()
    async def on_rce_player_join(self, player):
        for _ in range(self.setting("times")):
            await self.say(self.setting("text").format(player=player))
'''


@pytest.fixture
def pb(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
                     WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"), EVENTS_CHANNEL_ID="100",
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md"), POINTS_PER_HOUR="60").items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from config import load_config
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])       # no background loops in tests
    bot = RceBot(load_config("/nonexistent"))
    said, posted = [], []

    async def command(cmd, **kw):
        said.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    monkeypatch.setattr(type(bot.rcon), "connected", property(lambda self: True))

    class Channel:
        id = 100

        async def send(self, content=None, embed=None, **kw):
            posted.append(NS(content=content, embed=embed, kw=kw))
            return NS(id=len(posted))
    channel = Channel()
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel if cid else None)
    bot.plugins = PluginManager(bot)
    yield NS(bot=bot, db=bot.db, m=bot.plugins, said=said, posted=posted, tmp=tmp_path)
    bot.db.close()


def plug(pb, pid, **settings):
    """A plugin instance without Discord (settings set directly)."""
    m = pb.m
    if not m.infos:
        m.discover()
    m.state["settings"][pid] = settings
    p = m.infos[pid].cls(pb.bot, pid, m)
    m.instances[pid] = p
    return p


def test_builtins_load_toggle_and_settings(pb):
    from plugin_manager import enabled_plugin_intents

    async def main():
        m = pb.m
        await m.load_all()
        assert set(m.infos) == ALL and all(i.error is None for i in m.infos.values())
        assert set(m.instances) == DEFAULT_ON
        assert pb.bot.tree.get_command("bounty") is not None
        info = await m.set_enabled("invites", True)
        assert info["running"] and info["missing_intents"] == ["members"]       # needs the Server Members intent
        assert enabled_plugin_intents(pb.bot.config.database_path) == {"members": ["Plugin Invite rewards"]}
        await m.set_enabled("bounties", False)
        assert "bounties" not in m.instances and pb.bot.tree.get_command("bounty") is None
        state = json.loads(m.state_path.read_text())
        assert state["enabled"] == {"invites": True, "bounties": False}
        errors = await m.save_settings("early_bird", {"players": "abc", "points": "5"})
        assert "players" in errors and m.settings_of("early_bird") == {}
        assert await m.save_settings("early_bird", {"players": "3", "points": "100", "announce": False}) == {}
        assert m.settings_of("early_bird") == {"players": 3, "announce": False}      # default (100) isn't stored
        assert m.running("early_bird").setting("players") == 3
        all_info = m.describe_all()
        assert {p["id"] for p in all_info} == ALL and all(p["settings"] is not None for p in all_info)
        await m.unload_all()
        assert not m.instances
    asyncio.run(main())


def test_own_plugin_upload_reload_and_delete(pb):
    from plugin_manager import PluginError

    async def main():
        m = pb.m
        await m.load_all()
        info = await m.install("hello.py", HELLO)
        assert info["name"] == "Hello" and not info["enabled"] and not info["builtin"]
        assert (m.user_dir / "hello.py").exists()
        await m.set_enabled("hello", True)
        await m.save_settings("hello", {"times": "2"})
        await m.running("hello").on_rce_player_join("Bob")
        assert pb.said[-2:] == ["say Hi Bob!", "say Hi Bob!"]
        # A broken update keeps the old, working version
        with pytest.raises(PluginError, match="line"):
            await m.install("hello.py", "def broken(:\n")
        with pytest.raises(PluginError, match="exactly one class"):
            await m.install("hello.py", "x = 1\n")
        assert m.running("hello") is not None and "Hi" in (m.user_dir / "hello.py").read_text()
        with pytest.raises(PluginError, match="built-in"):
            await m.install("bounties.py", HELLO)
        with pytest.raises(PluginError, match="File name"):
            await m.install("../evil.py.txt", HELLO)
        (m.user_dir / "hello.py").write_text(HELLO.replace("Hi {player}!", "Yo {player}!"))
        await m.reload("hello")
        pb.said.clear()
        await m.save_settings("hello", {"times": "1", "text": "Yo {player}!"})
        await m.running("hello").on_rce_player_join("Ann")
        assert pb.said == ["say Yo Ann!"]
        with pytest.raises(PluginError, match="Built-in"):
            await m.uninstall("bounties")
        await m.uninstall("hello")
        assert "hello" not in m.infos and not (m.user_dir / "hello.py").exists()
    asyncio.run(main())


def test_broken_plugin_never_stops_the_bot(pb):
    from plugin_manager import PluginError

    async def main():
        m = pb.m
        m.user_dir.mkdir(parents=True)
        (m.user_dir / "crash.py").write_text("raise RuntimeError('boom')\n")
        (m.user_dir / "Bad-Name.py").write_text(HELLO)
        clash = HELLO.replace("class Hello", "class Economy")          # same name as a core part of the bot
        (m.user_dir / "clash.py").write_text(clash)
        m.state["enabled"]["clash"] = True
        from discord.ext import commands

        class Economy(commands.Cog):          # stands in for the core cog of that name
            pass
        await pb.bot.add_cog(Economy())
        await m.load_all()
        assert "RuntimeError: boom" in m.describe("crash")["error"] and "Bad-Name" not in m.infos
        with pytest.raises(PluginError):
            await m.set_enabled("crash", True)
        assert "clash" not in m.instances and "already called Economy" in m.describe("clash")["error"]
        assert set(m.instances) == DEFAULT_ON                            # everything else runs
    asyncio.run(main())


def test_web_api_and_public_page(pb):
    pb.db.record_kill("<script>alert(1)</script>", "Victim")

    async def main():
        from web.server import WebInterface
        m = pb.m
        await m.load_all()
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/plugins")).status == 401
            assert (await c.get("/p/public_page")).status == 404                 # off -> no page
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await (await c.get("/api/plugins")).json()
            assert {p["id"] for p in r["plugins"]} == ALL
            r = await c.post("/api/plugins/public_page/toggle", json={"enabled": True}, headers=H)
            assert (await r.json())["plugin"]["running"] is True
            r = await c.put("/api/plugins/public_page/settings", json={"values": {"accent": "#123456",
                                                                              "invite": "https://discord.gg/abc"}},
                            headers=H)
            assert r.status == 200
            r = await c.put("/api/plugins/weekly_recap/settings", json={"values": {"hour": "99"}}, headers=H)
            assert r.status == 400 and "hour" in (await r.json())["errors"]
            r = await c.post("/api/plugins/upload", json={"filename": "hello.py", "code": HELLO}, headers=H)
            assert (await r.json())["plugin"]["name"] == "Hello"
            assert (await c.delete("/api/plugins/hello", headers=H)).status == 200
            r = await c.post("/api/plugins/upload", json={"filename": "x.py", "code": "nope("}, headers=H)
            assert r.status == 400
            c.session.cookie_jar.clear()                                          # the public page needs no login
            r = await c.get("/p/public_page")
            page = await r.text()
            assert r.status == 200 and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>" not in page
            assert "https://discord.gg/abc" in page and "#123456" in page
            assert "default-src 'none'" in r.headers["Content-Security-Policy"]
            data = await (await c.get("/p/public_page/data.json")).json()
            assert data["top_kills"][0]["kills"] == 1 and "password" not in json.dumps(data).lower()
            assert (await c.get("/p/public_page/../../.env")).status == 404
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_backup_contains_plugins(tmp_path):
    import zipfile

    import backups
    (tmp_path / "data" / "plugins").mkdir(parents=True)
    (tmp_path / "data" / "plugins" / "mine.py").write_text("x = 1\n")
    (tmp_path / "data" / "plugins.json").write_text("{}")
    path = backups.make_backup(tmp_path / "data" / "bot.db", base=tmp_path)
    names = zipfile.ZipFile(path).namelist()
    assert "data/plugins/mine.py" in names and "data/plugins.json" in names
