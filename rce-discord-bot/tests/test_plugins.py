"""Plugin system: discovery, on/off, settings, own plugin files, broken plugins, web API, player website and
item pictures."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

H = {"X-Requested-With": "rce-web"}
DEFAULT_ON = {"starter_kit", "lowpop_bonus", "early_bird", "bounties", "weekly_recap", "achievements", "wipe_poll"}
ALL = DEFAULT_ON | {"invites", "website", "filling_ping", "comeback", "casino", "offline_protection", "battlepass", "battle_royale",
                    "player_count", "streamers"}

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
        pb.bot._connection._intents.members = False              # as if the intent were not granted
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
        assert {p["id"] for p in all_info} == ALL - {"offline_protection"}           # a main feature, own page
        assert all(p["settings"] is not None for p in all_info)
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
        with pytest.raises(PluginError, match="same key: text"):
            await m.install("hello.py", HELLO.replace('Setting("times"', 'Setting("text"'))
        assert m.running("hello") is not None and "Hi" in (m.user_dir / "hello.py").read_text()
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


def test_own_copy_replaces_a_builtin_and_survives(pb):
    """A file in data/plugins/ with a built-in's name replaces it (so an update adding a built-in with your plugin's
    name can't hide yours, and a changed built-in can be kept); deleting it brings the built-in back."""
    from plugin_manager import PluginError
    MINE = HELLO.replace("class Hello", "class MyBounties").replace('NAME = "Hello"', 'NAME = "My bounties"')

    async def main():
        m = pb.m
        await m.load_all()
        assert m.infos["bounties"].builtin and m.running("bounties") is not None         # built-in, on by default
        await m.save_settings("bounties", {})
        info = await m.install("bounties.py", MINE)
        assert info["name"] == "My bounties" and info["overrides"] and not info["builtin"]
        assert info["enabled"] and m.running("bounties").NAME == "My bounties"            # took over, stayed on
        m.discover()                                                                       # e.g. after a restart
        assert m.infos["bounties"].overrides and m.running("bounties").NAME == "My bounties"
        # Deleting your copy brings the built-in back (still on)
        await m.uninstall("bounties")
        assert m.infos["bounties"].builtin and m.running("bounties") is not None
        # A broken copy never takes down the built-in
        with pytest.raises(PluginError, match="can't be used"):
            await m.install("bounties.py", "x = 1\n")
        assert m.infos["bounties"].builtin and m.running("bounties") is not None
        assert not (m.user_dir / "bounties.py").exists()
        with pytest.raises(PluginError, match="Built-in"):
            await m.uninstall("bounties")
    asyncio.run(main())


def test_copy_found_at_start_keeps_on_off(pb):
    """The updater put your changed copy of a built-in into data/plugins/ – after the restart it runs, still on."""
    async def main():
        m = pb.m
        await m.load_all()                       # first start: remembers that bounties is on by default
        await m.unload_all()
        from plugin_manager import PluginManager
        m.user_dir.mkdir(parents=True, exist_ok=True)
        src = (m.builtin_dir / "bounties.py").read_text().replace("DEFAULT_ENABLED = True", "DEFAULT_ENABLED = False")
        (m.user_dir / "bounties.py").write_text(src)
        fresh = PluginManager(pb.bot)            # = the restart after the update
        pb.bot.plugins = fresh
        await fresh.load_all()
        assert fresh.infos["bounties"].overrides and fresh.running("bounties") is not None
        await fresh.unload_all()
    asyncio.run(main())


def test_failed_plugins_are_reported_at_start(pb):
    async def main():
        m = pb.m
        m.user_dir.mkdir(parents=True, exist_ok=True)
        (m.user_dir / "oldone.py").write_text("from plugin_api import Gone\n")         # e.g. made for an older bot
        m.state["enabled"]["oldone"] = True
        await m.load_all()
        assert [p for p, _ in m.startup_failures] == ["oldone"] and "Gone" in m.startup_failures[0][1]
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


def test_web_api_and_player_website(pb):
    db = pb.db
    db.record_kill("<script>alert(1)</script>", "Victim")
    db.record_kill("Ace", "Victim")
    db.record_kill("Ace", "Bob")
    db.add_playtime(["Ace"], 7200)
    db.create_custom_kit("Raid", None, "admin")
    db.add_custom_kit_item("Raid", "ammo.rocket.basic", 3)
    db.add_shop_item("Raid kit", "custom_kit", "Raid", 1, 500, "Boom")

    async def main():
        from web.server import WebInterface
        m = pb.m
        await m.load_all()
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/plugins")).status == 401
            r = await c.get("/", allow_redirects=False)                          # website off -> the panel
            assert r.status == 302 and r.headers["Location"] == "/admin"
            assert (await c.get("/leaderboard")).status == 404
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await (await c.get("/api/plugins")).json()
            assert {p["id"] for p in r["plugins"]} == ALL - {"offline_protection"}
            r = await c.post("/api/plugins/website/toggle", json={"enabled": True}, headers=H)
            assert (await r.json())["plugin"]["running"] is True
            r = await c.put("/api/plugins/website/settings", json={"values": {
                "accent": "#123456", "invite": "https://discord.gg/abc", "store": "javascript:alert(1)",
                "about": "5x gather\n<b>solo/duo</b>"}}, headers=H)
            assert r.status == 200
            r = await c.put("/api/plugins/weekly_recap/settings", json={"values": {"hour": "99"}}, headers=H)
            assert r.status == 400 and "hour" in (await r.json())["errors"]
            r = await c.post("/api/plugins/upload", json={"filename": "hello.py", "code": HELLO}, headers=H)
            assert (await r.json())["plugin"]["name"] == "Hello"
            assert (await c.delete("/api/plugins/hello", headers=H)).status == 200
            r = await c.post("/api/plugins/upload", json={"filename": "x.py", "code": "nope("}, headers=H)
            assert r.status == 400
            c.session.cookie_jar.clear()                                          # the website needs no login
            r = await c.get("/")
            page = await r.text()
            assert r.status == 200 and "<script>" not in page and "javascript:" not in page
            assert "https://discord.gg/abc" in page and "#123456" in page and "&lt;b&gt;solo/duo&lt;/b&gt;" in page
            assert "default-src 'none'" in r.headers["Content-Security-Policy"]
            page = await (await c.get("/leaderboard?by=kills")).text()
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "player?name=Ace" in page
            page = await (await c.get("/player?name=ace")).text()
            assert ">Ace <span" in page and "2h" in page
            page = await (await c.get("/player?name=A")).text()
            assert "Did you mean" in page and "player?name=Ace" in page
            page = await (await c.get("/shop")).text()
            assert "Raid kit" in page and "/img/items/ammo.rocket.basic.png?ph=1" in page and "500" in page
            assert (await c.get("/rules")).status == 200
            assert (await c.get("/nope")).status == 404
            assert (await c.get("/p/website/leaderboard")).status == 200
            data = await (await c.get("/status.json")).json()
            assert data["online"] is False and "password" not in json.dumps(data).lower()
            await m.save_settings("website", {"leaderboards": False})
            assert (await c.get("/leaderboard")).status == 404
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_ai_plugin_generate_endpoint(pb, monkeypatch):
    """POST /api/plugins/generate drafts a plugin with the AI but does NOT install it; needs the AI on."""
    async def main():
        from web.server import WebInterface
        await pb.m.load_all()
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")

        async def chat_json(messages, **kw):
            return json.dumps({"filename": "greeter.py", "name": "Greeter", "summary": "Greets players.",
                               "code": HELLO})
        real_get_cog = pb.bot.__class__.get_cog.__get__(pb.bot)
        fake_ai = NS(enabled=True, chat_json=chat_json)
        monkeypatch.setattr(pb.bot, "get_cog",
                            lambda name: NS(ai=fake_ai) if name == "Tickets" else real_get_cog(name))
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post("/api/plugins/generate", json={"description": "greet players on join"}, headers=H)
            assert r.status == 200
            draft = (await r.json())["draft"]
            assert draft["filename"] == "greeter.py" and draft["warnings"] == [] and "class Hello" in draft["code"]
            assert "greeter" not in {p["id"] for p in (await (await c.get("/api/plugins")).json())["plugins"]}
            # Empty description is rejected; AI off -> clear error
            assert (await c.post("/api/plugins/generate", json={"description": " "}, headers=H)).status == 400
            fake_ai.enabled = False
            r = await c.post("/api/plugins/generate", json={"description": "greet players"}, headers=H)
            assert r.status == 400 and "AI is off" in (await r.json())["error"]
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_item_pictures_are_cached(pb, monkeypatch):
    from web import item_images
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
    calls = []

    async def fake_download(self, shortname):
        calls.append(shortname)
        if shortname == "rifle.ak":
            self.folder.mkdir(parents=True, exist_ok=True)
            self._path(shortname).write_bytes(png)
            return png
        self._missing[shortname] = item_images.time.time()
        return None
    monkeypatch.setattr(item_images.ItemImages, "_download", fake_download)
    assert item_images.image_type(png) == "image/png" and item_images.image_type(b"<html>") is None

    async def main():
        from web.server import WebInterface
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            r = await c.get("/img/items/rifle.ak.png")                             # public, no login needed
            assert r.status == 200 and r.content_type == "image/png" and await r.read() == png
            assert "max-age" in r.headers["Cache-Control"]
            await c.get("/img/items/rifle.ak.png")
            assert calls == ["rifle.ak"]                                         # downloaded once, then cached
            assert (await c.get("/img/items/nothing.png")).status == 404
            assert (await c.get("/img/items/nothing.png")).status == 404
            assert calls == ["rifle.ak", "nothing"]                              # missing ones aren't retried
            r = await c.get("/img/items/nothing.png?ph=1")                        # website: placeholder tile
            assert r.content_type == "image/svg+xml" and "nothing" in await r.text()
            assert (await c.get("/img/items/..%2F..%2Fsecret.png")).status == 404
            assert (await c.get("/img/items/UPPER$.png")).status == 404
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())
    from settings_schema import validate
    assert validate("ITEM_IMAGE_URL", "https://x.example/{shortname}.png") is None
    assert validate("ITEM_IMAGE_URL", "off") is None and validate("ITEM_IMAGE_URL", "http://x/{shortname}")


def test_backup_contains_plugins(tmp_path):
    import zipfile

    import backups
    (tmp_path / "data" / "plugins").mkdir(parents=True)
    (tmp_path / "data" / "plugins" / "mine.py").write_text("x = 1\n")
    (tmp_path / "data" / "plugins.json").write_text("{}")
    path = backups.make_backup(tmp_path / "data" / "bot.db", base=tmp_path)
    names = zipfile.ZipFile(path).namelist()
    assert "data/plugins/mine.py" in names and "data/plugins.json" in names


def test_website_logo_upload_and_default(pb):
    import base64
    png = b"\x89PNG\r\n\x1a\n" + b"y" * 40

    async def main():
        from web.server import WebInterface
        m = pb.m
        await m.load_all()
        await m.set_enabled("website", True)
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            r = await c.get("/logo")                                         # bundled default logo
            assert r.status == 200 and r.content_type == "image/webp"
            assert (await c.get("/icon")).content_type == "image/png"
            page = await (await c.get("/")).text()
            assert 'src="/logo?s=96&amp;v=' in page and "Build · Survive · Dominate" in page
            url = "/api/plugins/website/file/logo"
            assert (await c.post(url, json={"data": base64.b64encode(png).decode()}, headers=H)).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post(url, json={"data": "data:text/html;base64," + base64.b64encode(b"<script>").decode()},
                             headers=H)
            assert r.status == 400                                                 # only real pictures
            r = await c.post(url, json={"data": "data:image/png;base64," + base64.b64encode(png).decode()}, headers=H)
            settings = {f["key"]: f for f in (await r.json())["plugin"]["settings"]}
            assert r.status == 200 and settings["logo"]["value"] == "logo.png" and settings["logo"]["kind"] == "image"
            assert (m.files_dir / "website" / "logo.png").read_bytes() == png
            assert await (await c.get(url)).read() == png
            r = await c.put("/api/plugins/website/settings", json={"values": {"logo": "../../.env"}}, headers=H)
            assert r.status == 200 and m.settings_of("website")["logo"] == "logo.png"     # can't be set by hand
            c.session.cookie_jar.clear()
            assert await (await c.get("/logo")).read() == png                 # the uploaded logo is used
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            assert (await c.delete(url, headers=H)).status == 200
            assert (await c.get("/logo")).content_type == "image/webp"        # back to the default
            assert (await c.post("/api/plugins/bounties/file/min_amount", json={"data": ""}, headers=H)).status == 400
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_website_halloween_theme_and_phone_menu(pb, monkeypatch):
    import datetime as dt
    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        site = pb.m.running("website")
        october = dt.datetime(2026, 10, 24, 12, tzinfo=dt.timezone.utc).timestamp()
        june = dt.datetime(2026, 6, 1, 12, tzinfo=dt.timezone.utc).timestamp()
        monkeypatch.setattr(type(site), "now", staticmethod(lambda: int(october)))
        assert site.seasonal() == "halloween"
        page = site.home("")
        assert 'class="halloween"' in page and 'class="bat b1"' in page and "hw-banner" in page and "🕷" in page
        assert 'id="mnav"' in page and 'class="burger"' in page                      # ☰ menu on phones
        monkeypatch.setattr(type(site), "now", staticmethod(lambda: int(june)))
        page = site.home("")
        assert site.seasonal() == "" and "hw-banner" not in page and 'class="bat' not in page
        await pb.m.save_settings("website", {"season_theme": "halloween"})
        assert site.seasonal() == "halloween"
        await pb.m.save_settings("website", {"season_theme": "off"})
        monkeypatch.setattr(type(site), "now", staticmethod(lambda: int(october)))
        assert site.seasonal() == ""
    asyncio.run(main())


def test_owner_picks_the_plugins_in_the_menu(pb):  # noqa: F811
    """Control panel sidebar: which plugins get their own tab (default: the ones with TAB) – saved, ordered."""
    import json

    from tests.test_web import H

    async def main():
        from web.server import WebInterface
        m = pb.m
        await m.load_all()
        assert m.menu() == []                                     # nothing pinned by default
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/plugins/menu")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.put("/api/plugins/menu", json={"plugins": ["offline_protection"]}, headers=H)
            assert r.status == 400                                   # a main feature: fixed place, not pinnable
            r = await c.put("/api/plugins/menu", json={"plugins": ["casino", "achievements", "casino"]},
                            headers=H)
            assert [x["id"] for x in (await r.json())["menu"]] == ["casino", "achievements"]   # no doubles
            assert (await r.json())["menu"][0]["icon"] == "🎰"
            plugins = (await (await c.get("/api/plugins")).json())["plugins"]
            assert {p["id"] for p in plugins if p["in_menu"]} == {"casino", "achievements"}
            assert [x["group"] for x in (await r.json())["menu"]] == ["plugins", "plugins"]       # ids only: Plugins
            r = await c.put("/api/plugins/menu", json={"plugins": [{"id": "casino", "group": "community"},
                                                                    {"id": "achievements", "group": "server"}]},
                            headers=H)
            assert [(x["id"], x["group"]) for x in (await r.json())["menu"]] == [
                ("casino", "community"), ("achievements", "server")]
            r = await c.put("/api/plugins/menu", json={"plugins": [{"id": "casino", "group": "kitchen"}]}, headers=H)
            assert r.status == 400 and "category" in (await r.json())["error"]
            m.state["menu"] = ["casino"]                                           # an older plugins.json
            assert m.menu_entries() == [{"id": "casino", "group": "plugins"}]
            r = await c.put("/api/plugins/menu", json={"plugins": ["nope"]}, headers=H)
            assert r.status == 400 and "Unknown plugin" in (await r.json())["error"]
            await c.put("/api/plugins/menu", json={"plugins": []}, headers=H)                # empty is fine
            assert (await (await c.get("/api/plugins/menu")).json())["menu"] == []
            assert json.loads(m.state_path.read_text())["menu"] == []                        # kept after a restart
            index = await (await c.get("/admin")).text()
            assert 'id="nav-plugins"' in index and 'href="#protection" data-view="protection"' in index
            assert all(f'data-key="{k}"' in index for k in m.MENU_GROUPS)                  # categories to pick
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_panel_logo_is_the_bots_discord_picture(pb, monkeypatch):  # noqa: F811
    from types import SimpleNamespace as NS
    reads = []

    class Asset:
        url = "https://cdn.discordapp.com/avatars/1/abc.png?size=128"

        def replace(self, size=None, format=None):
            assert size == 128 and format == "png"
            return self

        async def read(self):
            reads.append(1)
            return b"\x89PNG fake"

    async def main():
        from web.server import WebInterface
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/img/bot-avatar.png")).status == 404            # not logged in to Discord yet
            monkeypatch.setattr(type(pb.bot), "user", property(lambda self: NS(display_avatar=Asset())))
            r = await c.get("/img/bot-avatar.png")                                # public: the login screen too
            assert r.status == 200 and r.content_type == "image/png" and await r.read() == b"\x89PNG fake"
            r2 = await c.get("/img/bot-avatar.png", headers={"If-None-Match": r.headers["ETag"]})
            assert r2.status == 304 and reads == [1]                              # fetched from Discord once
            js = await (await c.get("/static/app.js")).text()
            assert "/img/bot-avatar.png" in js
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())


def test_player_count_channel(pb, monkeypatch):  # noqa: F811
    """The channel's name shows the players online; created by the bot if none is picked; renamed at most every
    few minutes (Discord's rename limit) and only when the text changed."""
    from types import SimpleNamespace as NS

    from plugins import player_count as pcm
    channels, renames = {}, []

    class Channel:
        def __init__(self, cid, name):
            self.id, self.name = cid, name

        async def edit(self, name=None, reason=None):
            renames.append(name)
            self.name = name

    async def create_voice_channel(name, position=None, reason=None, overwrites=None):
        assert position == 0 and list(overwrites.values())[0].connect is False      # nobody can join
        channels[900] = Channel(900, name)
        return channels[900]
    guild = NS(id=1, default_role=object(), create_voice_channel=create_voice_channel)
    monkeypatch.setattr(type(pb.bot), "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(pb.bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(pb.bot, "get_channel", lambda cid: channels.get(int(cid)))
    monkeypatch.setattr(pb.bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(pb.bot.rcon), "connected", property(lambda self: True))
    clock = [1000.0]
    monkeypatch.setattr(pcm.time, "monotonic", lambda: clock[0])

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("player_count", True)
        p = pb.m.running("player_count")
        pb.bot.server_info = {"Players": 12, "MaxPlayers": 50, "Queued": 0}
        assert await p.refresh() and channels[900].name == "🟢 Online: 12/50" and p.data.get("created_channel") == 900
        assert not await p.refresh()                                      # same text: no rename
        pb.bot.server_info = {"Players": 13, "MaxPlayers": 50}
        assert not await p.refresh() and channels[900].name == "🟢 Online: 12/50"   # too soon (Discord limit)
        clock[0] += 5 * 60
        assert await p.refresh() and channels[900].name == "🟢 Online: 13/50"
        pb.bot.server_info = None                                          # server offline
        clock[0] += 5 * 60
        assert await p.refresh() and channels[900].name == "🔴 Server offline"
        pb.bot.server_info = {"Players": 3, "MaxPlayers": 50, "Queued": 4}
        await pb.m.save_settings("player_count", {"online_name": "👥 {players} online · {queued} queued"})
        assert await p.refresh() and channels[900].name == "👥 3 online · 4 queued"   # new text: right away
        assert p.status()["Shows"] == "👥 3 online · 4 queued" and p.problems() == []
        channels[5] = Channel(5, "stats")
        await pb.m.save_settings("player_count", {"channel": "5"})
        assert await p.refresh() and channels[5].name == "👥 3 online · 4 queued"    # own channel
        await pb.m.save_settings("player_count", {"channel": "6"})
        assert not await p.refresh() and "wasn't found" in p.problems()[0]
        await pb.bot.rcon.close()
    asyncio.run(main())
    assert renames == ["🟢 Online: 13/50", "🔴 Server offline", "👥 3 online · 4 queued", "👥 3 online · 4 queued"]
