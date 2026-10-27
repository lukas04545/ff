"""One Discord bot for several Rust servers (discord_router.py): the "server" choice on slash commands, and every
event handled by exactly one server's process."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import discord_router
import servers
from envfile import read_env, update_env
from tests.test_servers import TOKEN_2, main_dir  # noqa: F401

MAIN_TOKEN = "main-token-" + "z" * 60


def cmd(name, *options):
    return {"type": 1, "name": name, "description": name, "options": list(options)}


def sub(name, *options):
    return {"type": 1, "name": name, "description": name, "options": list(options)}


def opt(name, required=True):
    return {"type": 3, "name": name, "description": name, "required": required}


def test_merged_commands_get_a_server_choice_where_more_than_one_server_has_them():
    own = [cmd("status"), cmd("kit", sub("give", opt("player")), sub("list")), cmd("main-only")]
    two = [cmd("status"), cmd("kit", sub("give", opt("player")), sub("list")), cmd("slots", opt("bet"))]
    out = {c["name"]: c for c in discord_router.merged_payload(own, {"two": two}, {"two": "2x Monthly"})}
    assert set(out) == {"status", "kit", "main-only", "slots"}               # slots only exists on "two"
    choice = out["status"]["options"][-1]
    assert choice["name"] == "server" and choice["required"] is False
    assert choice["choices"] == [{"name": "Main server", "value": "main"}, {"name": "2x Monthly", "value": "two"}]
    give, lst = out["kit"]["options"]
    assert [o["name"] for o in give["options"]] == ["player", "server"]      # after the required options
    assert [o["name"] for o in lst["options"]] == ["server"]
    assert out["main-only"]["options"] == [] and out["slots"]["options"] == [opt("bet")]
    assert own[0]["options"] == []                                           # the input isn't changed


def test_the_server_option_is_taken_out_before_discord_py_sees_it():
    data = {"name": "kit", "options": [{"type": 1, "name": "give", "options": [
        {"type": 3, "name": "player", "value": "Bob"}, {"type": 3, "name": "server", "value": "two"}]}]}
    assert discord_router.take_server_option(data) == "two"
    assert data["options"][0]["options"] == [{"type": 3, "name": "player", "value": "Bob"}]
    assert discord_router.take_server_option({"name": "status"}) is None


@pytest.fixture
def shared(main_dir, monkeypatch):  # noqa: F811
    """Main + server "two" on the main bot (its chat channel 500); commands as the servers registered them."""
    (main_dir / ".env").write_text((main_dir / ".env").read_text().replace(
        "DISCORD_TOKEN=main-token-" + "z" * 60, f"DISCORD_TOKEN={MAIN_TOKEN}") + "CHAT_CHANNEL_ID=400\n",
        encoding="utf-8")
    info = servers.create_server("Two", "1.2.3.4", "28016", "pw", "")          # no token: the main bot
    assert info["discord"] == "shared" and info["id"] == "two"
    update_env(servers.env_path("two"), {"CHAT_CHANNEL_ID": "500"})
    discord_router.write_commands("two", [cmd("status"), cmd("slots")])
    discord_router.write_commands("main", [cmd("status"), cmd("kit")])     # what the main registered
    return main_dir


def fake_bot(commands, handled):
    async def request(route, **kwargs):
        return kwargs.get("result")
    parsers = {"INTERACTION_CREATE": lambda d: handled.append(("interaction", d)),
               **{e: (lambda d, e=e: handled.append((e, d))) for e in discord_router.CHANNEL_EVENTS}}
    return SimpleNamespace(
        tree=SimpleNamespace(get_commands=lambda: [SimpleNamespace(name=n) for n in commands]),
        _connection=SimpleNamespace(parsers=parsers, _view_store=SimpleNamespace(
            _views={}, _synced_message_views={}, _modals={})),
        http=SimpleNamespace(request=request))


def command(name, channel, server=None, iid=1):
    options = [{"type": 3, "name": "server", "value": server}] if server else []
    return {"id": str(iid), "type": 2, "channel_id": str(channel), "data": {"name": name, "options": options}}


def test_a_shared_server_is_set_up_from_the_main_bot(shared):
    env = read_env(servers.env_path("two"))
    assert env["DISCORD_TOKEN"] == MAIN_TOKEN and env["GUILD_ID"] == "111"
    assert servers.child_environment("two")[servers.ENV_SHARED_BOT] == "1"
    (shared / ".env").write_text((shared / ".env").read_text().replace(MAIN_TOKEN, "new-" + MAIN_TOKEN),
                                 encoding="utf-8")
    assert servers.sync_env("two") == {"DISCORD_TOKEN": "new-" + MAIN_TOKEN}     # a new main token follows
    own = servers.create_server("Own", "1.2.3.5", "28016", "pw", TOKEN_2)
    assert own["discord"] == "own" and servers.ENV_SHARED_BOT not in servers.child_environment("own")
    servers.use_main_bot("own")
    assert servers.discord_mode("own") == "shared"
    assert read_env(servers.env_path("own"))["DISCORD_TOKEN"] == "new-" + MAIN_TOKEN


def test_every_event_is_handled_by_exactly_one_server(shared, monkeypatch):
    seen = {"main": [], "two": []}
    store = discord_router.RouteStore(shared / "data" / "discord_routes.db")
    routers = {}
    for sid, commands in (("main", ["status", "kit"]), ("two", ["status", "slots"])):
        bot = fake_bot(commands, seen[sid])
        routers[sid] = discord_router.Router(bot, sid=sid, store=store)
        routers[sid].install()

    def deliver(event, payload):
        for router in routers.values():
            router.bot._connection.parsers[event](json.loads(json.dumps(payload)))

    def who():
        out = [sid for sid, items in seen.items() if items]
        for items in seen.values():
            items.clear()
        return out

    deliver("INTERACTION_CREATE", command("status", 1, server="two"))
    assert who() == ["two"]
    deliver("INTERACTION_CREATE", command("status", 500))                    # two's channel
    assert who() == ["two"]
    deliver("INTERACTION_CREATE", command("status", 999))                    # anywhere else: the main server
    assert who() == ["main"]
    deliver("INTERACTION_CREATE", command("slots", 999))                     # only two has it
    assert who() == ["two"]
    deliver("INTERACTION_CREATE", command("kit", 500))                       # two doesn't have it
    assert who() == ["main"]
    deliver("INTERACTION_CREATE", command("status", 999, server="two", iid=77))
    got = seen["two"][0][1]
    assert got["data"]["options"] == []                                      # discord.py never sees "server"
    who()
    # buttons: on a message the server sent / on the reply to its command / in its channel
    asyncio.run(routers["two"].bot.http.request(SimpleNamespace(method="POST", path="/channels/{channel_id}/messages"),
                                                result={"id": "9001", "channel_id": "999", "components": [{}]}))
    asyncio.run(routers["two"].bot.http.request(SimpleNamespace(method="POST", path="/channels/{channel_id}/messages"),
                                                result={"id": "9002", "channel_id": "999", "components": []}))
    button = {"id": "5", "type": 3, "channel_id": "999", "data": {"custom_id": "x", "component_type": 2}}
    deliver("INTERACTION_CREATE", {**button, "message": {"id": "9001", "channel_id": "999"}})
    assert who() == ["two"]
    deliver("INTERACTION_CREATE", {**button, "message": {"id": "9002", "channel_id": "999"}})
    assert who() == ["main"]                                                 # no buttons: not noted
    deliver("INTERACTION_CREATE", {**button, "message": {"id": "123", "channel_id": "999",
                                                         "interaction_metadata": {"id": "77"}}})
    assert who() == ["two"]                                                  # the reply to two's command
    deliver("INTERACTION_CREATE", {**button, "channel_id": "500", "message": {"id": "124", "channel_id": "500"}})
    assert who() == ["two"]
    # a modal: whoever opened it
    routers["main"].bot._connection._view_store._modals["m1"] = object()
    deliver("INTERACTION_CREATE", {"id": "6", "type": 5, "channel_id": "500", "data": {"custom_id": "m1"}})
    assert who() == ["main"]
    # channels it created (tickets) are its own; chat in a channel goes to its owner
    asyncio.run(routers["two"].bot.http.request(SimpleNamespace(method="POST", path="/guilds/{guild_id}/channels"),
                                                result={"id": "700", "type": 0, "name": "ticket-1"}))
    deliver("MESSAGE_CREATE", {"channel_id": "700", "id": "1"})
    assert who() == ["two"]
    deliver("MESSAGE_CREATE", {"channel_id": "400", "id": "2"})
    assert who() == ["main"]
    deliver("MESSAGE_CREATE", {"channel_id": "500", "id": "3"})
    assert who() == ["two"]
    deliver("MESSAGE_REACTION_ADD", {"channel_id": "999", "message_id": "9001"})
    assert who() == ["two"]
    store.close()


def test_an_extra_server_on_the_main_bot_hands_its_commands_to_the_main(shared, monkeypatch):
    from tests.test_web import bot as _  # noqa: F401
    monkeypatch.setenv(servers.ENV_ID, "two")
    monkeypatch.setenv(servers.ENV_SHARED_BOT, "1")
    monkeypatch.setenv(servers.ENV_SHARED, str(shared / "data"))
    assert discord_router.uses_main_bot()
    from web.server import locked_settings
    assert {"DISCORD_TOKEN", "GUILD_ID", "ABUSE_ITEMS"} <= locked_settings()
    monkeypatch.delenv(servers.ENV_SHARED_BOT)
    assert "DISCORD_TOKEN" not in locked_settings()


def test_the_bot_status_shows_every_server(shared):
    from cogs.status import presence_line
    main = ("Main", {"online": True, "players": 12, "max": 100, "queued": 0})
    two = ("2x Monthly", {"online": True, "players": 30, "max": 50, "queued": 4})
    off = ("Vanilla", {"online": False})
    assert presence_line([main], "rotate", 15) == ("12/100 players", True)                  # one server
    assert presence_line([main, two], "main", 15) == ("12/100 players", True)
    assert presence_line([main, two, off], "rotate", 15, now=0) == ("Main: 12/100 players", True)
    assert presence_line([main, two, off], "rotate", 15, now=15) == ("2x Monthly: 30/50 players (+4 queued)", True)
    assert presence_line([main, two, off], "rotate", 15, now=30) == ("Vanilla: Server offline", False)
    assert presence_line([main, two, off], "combined", 15) == ("Main 12/100 · 2x Monthly 30/50 (+4) · Vanilla offline",
                                                               True)
    assert presence_line([("Main", {"online": False}), off], "combined", 15)[1] is False   # all down: red dot
    assert len(presence_line([(n * 20, two[1]) for n in "abcdefg"], "combined", 15)[0]) == 128
    # what the extra servers report to the main server
    assert discord_router.read_presences(180) == [("Two", {"online": False})]              # not reported yet
    discord_router.write_presence("two", "2x Monthly", two[1])
    assert discord_router.read_presences(180) == [("2x Monthly", two[1])]
    assert discord_router.read_presences(0) == [("2x Monthly", {"online": False})]        # stopped reporting
