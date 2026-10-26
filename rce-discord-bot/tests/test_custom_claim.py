"""Self-service claim of custom kits (/customkit claim) with role check and cooldown."""
import asyncio
from types import SimpleNamespace

import pytest

from utils import format_minutes, parse_duration


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("30m") == 30 and parse_duration("45min") == 45
    assert parse_duration("12h") == 720 and parse_duration("1d") == 1440
    assert parse_duration("1d 12h") == 2160 and parse_duration("2h30m") == 150
    assert parse_duration("off") == 0 and parse_duration("0") == 0
    assert parse_duration("soon") is None and parse_duration("") is None and parse_duration("5x") is None
    assert format_minutes(0) == "no cooldown" and format_minutes(150) == "2h 30m"


class FakeInteraction:
    def __init__(self, user_id, role_ids):
        self.user = SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=r) for r in role_ids],
                                    mention=f"<@{user_id}>", __str__=lambda: "user")
        self.guild = None
        self.messages = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, content=None, **kwargs):
        self.messages.append(content)

    async def _defer(self, **kwargs):
        pass


@pytest.fixture
def cog(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", DATABASE_PATH=str(tmp_path / "bot.db")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    sent = []

    async def command(cmd, **kwargs):
        sent.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    bot.online_players = {"GamerTag42": {}}
    bot.db.create_custom_kit("VIP", None, "admin")
    bot.db.add_custom_kit_item("VIP", "rifle.ak", 1)
    bot.db.add_custom_kit_item("VIP", "ammo.rifle", 128)
    c = CustomKits(bot)
    c.sent = sent
    yield c
    bot.db.close()


def _claim(cog, interaction, kit="VIP"):
    asyncio.run(cog.claim.callback(cog, interaction, kit))
    return interaction.messages[-1]


def _chat(cog, player, raw):
    from rce.log_parser import ChatEvent
    asyncio.run(cog.on_rce_chat(ChatEvent(channel="GLOBAL", player=player, raw=raw, text=raw)))


def _link(cog, interaction, player):
    """Starts /customkit link and returns the requested quick chat phrases."""
    asyncio.run(cog.link.callback(cog, interaction, player))
    pending = cog._pending.get(player.lower())
    return pending.phrases if pending else None


def test_link_requires_ingame_verification(cog):
    from dataclasses import replace
    cog.bot.config = replace(cog.bot.config, link_verify_quickchat=True)    # optional emote wheel check
    db = cog.bot.db
    member = FakeInteraction(7, [55])
    edits = []

    async def edit(content=None, **kwargs):
        edits.append(content)
    member.edit_original_response = edit

    assert _link(cog, member, "Nobody") is None and "not online" in member.messages[-1]
    first, second = _link(cog, member, "gamertag42")
    assert "quick chat" in member.messages[-1] and first != second
    assert all("_responses_" not in p for p in (first, second))

    _chat(cog, "OtherPlayer", first)        # someone else's chat doesn't count
    _chat(cog, "OtherPlayer", second)
    _chat(cog, "GamerTag42", second)        # wrong order doesn't count
    assert db.linked_player(7) is None
    _chat(cog, "GamerTag42", first)
    _chat(cog, "GamerTag42", "d11_quick_chat_responses_slot_0")  # unrelated chat in between is ignored
    _chat(cog, "GamerTag42", second)
    assert db.linked_player(7) == "GamerTag42" and "Verified" in edits[-1]

    # The player can't be taken over by another Discord account
    thief = FakeInteraction(8, [55])
    assert _link(cog, thief, "GamerTag42") is None and "already linked" in thief.messages[-1]
    assert not db.link_player(8, "gamertag42")
    assert db.unlink_player(7) == "GamerTag42" and db.linked_player(7) is None


def test_claim_goes_only_to_own_player_with_cooldown_per_player(cog):
    db = cog.bot.db
    member = FakeInteraction(7, [55])
    # No roles configured -> nobody can claim
    assert "can't claim" in _claim(cog, member)

    db.set_custom_kit_roles("VIP", [55])
    db.set_custom_kit_cooldown("VIP", 60)
    assert "can't claim" in _claim(cog, FakeInteraction(8, [99]))  # wrong role
    assert "link your in-game player" in _claim(cog, member)       # not linked yet

    db.link_player(7, "GamerTag42")
    assert "sent to **GamerTag42**" in _claim(cog, member)
    assert cog.sent == ['inventory.giveto "GamerTag42" "rifle.ak" 1',
                        'inventory.giveto "GamerTag42" "ammo.rifle" 128']
    assert "can claim it again" in _claim(cog, member)
    assert len(cog.sent) == 2

    # Cooldown belongs to the player: re-linking the same player to another account doesn't reset it
    db.unlink_player(7)
    db.link_player(9, "GamerTag42")
    assert "can claim it again" in _claim(cog, FakeInteraction(9, [55]))

    # Shorter cooldown applies immediately; reset lets the player claim again
    db.set_custom_kit_cooldown("VIP", 0)
    assert "sent to" in _claim(cog, FakeInteraction(9, [55]))
    db.set_custom_kit_cooldown("VIP", 60)
    assert db.reset_custom_claims("VIP", "gamertag42") == 2
    assert "sent to" in _claim(cog, FakeInteraction(9, [55]))

    # Linked player offline -> nothing is sent
    cog.bot.online_players = {}
    db.reset_custom_claims("VIP")
    assert "not online" in _claim(cog, FakeInteraction(9, [55]))
    assert len(db.kit_claim_history("GamerTag42")) == 3


def test_spawn_kit_requires_discord_role(cog, monkeypatch):
    """Autokit with a required role: only linked players whose Discord account has the role get it."""
    import discord

    import cogs.kits as kits_module
    from rce.log_parser import RespawnEvent
    bot = cog.bot
    bot.config = bot.config.__class__(**{**bot.config.__dict__, "autokit_delay": 0})
    members = {7: SimpleNamespace(roles=[SimpleNamespace(id=55)]), 8: SimpleNamespace(roles=[])}
    fetches = []

    async def fetch_member(user_id):
        fetches.append(user_id)
        if user_id not in members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        return members[user_id]
    guild = SimpleNamespace(get_member=lambda _id: None, fetch_member=fetch_member,
                            get_role=lambda _id: SimpleNamespace(name="VIP"))
    monkeypatch.setattr(kits_module, "main_guild", lambda _bot: guild)
    kits = kits_module.Kits(bot)

    rule = bot.db.add_kit_rule("starter", "respawn", None, 0, 0, "admin", "ingame", required_role_id=55)
    bot.db.link_player(7, "VipPlayer")
    bot.db.link_player(8, "NormalPlayer")
    bot.db.link_player(9, "LeftDiscord")

    def respawn(player):
        cog.sent.clear()
        asyncio.run(kits.on_rce_respawn(RespawnEvent(player=player, platform="Xbox")))
        return [c for c in cog.sent if c.startswith("kit ")]

    assert respawn("VipPlayer") == ['kit givetoplayer "starter" "VipPlayer"']
    assert respawn("NormalPlayer") == []        # linked, but no role
    assert respawn("Unlinked") == []            # not linked -> no kit
    assert respawn("LeftDiscord") == []         # not on the Discord server any more
    assert respawn("VipPlayer") and fetches.count(7) == 1  # roles are cached

    # Without a required role everyone gets it again
    assert bot.db.set_kit_rule_role(rule, None)
    assert respawn("Unlinked") == ['kit givetoplayer "starter" "Unlinked"']


def test_link_with_dropdown_no_check(cog):
    """Default: /customkit link without a name shows a dropdown of online players, picking links right away."""
    db = cog.bot.db
    cog.bot.online_players = {"GamerTag42": {}, "zed": {}, "Taken": {}}
    db.link_player(99, "Taken")
    member = FakeInteraction(7, [55])
    views = []

    async def send(content=None, view=None, **kw):
        member.messages.append(content)
        views.append(view)
    member.response.send_message = send
    asyncio.run(cog.link.callback(cog, member, None))
    select = views[-1].children[0]
    assert [o.value for o in select.options] == ["GamerTag42", "zed"]           # someone else's player not offered

    edits = []

    async def edit_message(content=None, view="same", **kw):
        edits.append((content, view))
    member.response.edit_message = edit_message
    select._values = ["zed"]                                                     # what Discord sets on a pick
    asyncio.run(select.callback(member))
    assert db.linked_player(7) == "zed" and "now linked to **zed**" in edits[-1][0] and edits[-1][1] is None
    assert cog._pending == {}                                                    # no emote wheel needed

    # Changing the link needs an admin (the kit cooldown counts per player)
    asyncio.run(cog.link.callback(cog, member, None))
    assert "You are linked to **zed**" in member.messages[-1]
    asyncio.run(cog.link.callback(cog, member, "GamerTag42"))
    assert "already linked to **zed**" in member.messages[-1] and db.linked_player(7) == "zed"
    asyncio.run(cog.link.callback(cog, FakeInteraction(8, []), "Taken"))       # typed name: same rules
    assert db.player_owner("Taken") == 99

    cog.bot.online_players = {}
    nobody = FakeInteraction(8, [])
    asyncio.run(cog.link.callback(cog, nobody, None))
    assert "Join the game first" in nobody.messages[-1]


def test_admin_links_offline_player(cog):
    db = cog.bot.db
    db.add_playtime(["Offline Olli"], 60)                          # seen on the server before, not online now
    name, prev = cog.admin_link(7, "offline olli")
    assert name == "Offline Olli" and prev is None and db.linked_player(7) == "Offline Olli"
    with pytest.raises(ValueError, match="already linked"):
        cog.admin_link(8, "Offline Olli")                          # someone else's player: needs "move"
    name, prev = cog.admin_link(8, "Offline Olli", force=True)
    assert prev == 7 and db.linked_player(8) == "Offline Olli" and db.linked_player(7) is None
    assert cog.admin_link(8, "Never Seen")[0] == "Never Seen"      # unknown names work too (spelling warning)
    assert db.linked_player(8) == "Never Seen" and db.player_owner("Offline Olli") is None
    with pytest.raises(ValueError):
        cog.admin_link(9, "   ")


def test_admin_link_web_endpoint(cog):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from web.server import WebInterface

    async def main():
        await cog.bot.add_cog(cog)
        web = WebInterface(cog.bot, env_path=cog.bot.config.database_path.parent / ".env",
                           secret_path=cog.bot.config.database_path.parent / "web_secret.key")
        from web.users import hash_password
        web.ws.password = web.password_hash = hash_password("secret123")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        H = {"X-Requested-With": "rce-web"}
        try:
            assert (await c.post("/api/linked-players", json={"member": "5", "player": "X"}, headers=H)).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post("/api/linked-players", json={"member": "123456", "player": "Somebody"}, headers=H)
            body = await r.json()
            assert r.status == 200 and body["player"] == "Somebody" and body["seen"] is False
            assert cog.bot.db.linked_player(123456) == "Somebody"
            r = await c.post("/api/linked-players", json={"member": "999", "player": "Somebody"}, headers=H)
            assert r.status == 400 and "already linked" in (await r.json())["error"]
            r = await c.post("/api/linked-players", json={"member": "999", "player": "Somebody", "force": True},
                             headers=H)
            assert r.status == 200 and cog.bot.db.linked_player(999) == "Somebody"
            r = await c.post("/api/linked-players", json={"member": "nobody-here", "player": "Y"}, headers=H)
            assert r.status == 400 and "No Discord member" in (await r.json())["error"]
        finally:
            await c.close()
            await cog.bot.rcon.close()
    asyncio.run(main())
