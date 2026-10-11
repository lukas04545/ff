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


def _claim(cog, interaction, kit="VIP", player=None):
    asyncio.run(cog.claim.callback(cog, interaction, kit, player))
    return interaction.messages[-1]


def test_claim_needs_role_and_respects_cooldown(cog):
    db = cog.bot.db
    # No roles configured -> nobody can claim
    assert "can't claim" in _claim(cog, FakeInteraction(7, [55]), player="GamerTag42")

    db.set_custom_kit_roles("VIP", [55])
    db.set_custom_kit_cooldown("VIP", 60)
    assert "can't claim" in _claim(cog, FakeInteraction(8, [99]), player="GamerTag42")  # wrong role

    member = FakeInteraction(7, [55])
    assert "enter your in-game name" in _claim(cog, member)
    assert "not online" in _claim(cog, member, player="Somebody")
    assert "sent to **GamerTag42**" in _claim(cog, member, player="gamertag42")  # name matched to the server's
    assert cog.sent == ['inventory.giveto "GamerTag42" "rifle.ak" 1',
                        'inventory.giveto "GamerTag42" "ammo.rifle" 128']

    # Cooldown: second claim refused, name remembered
    assert "can claim it again" in _claim(cog, member)
    assert len(cog.sent) == 2

    # Shorter cooldown applies immediately; reset lets the member claim again
    db.set_custom_kit_cooldown("VIP", 0)
    assert "sent to" in _claim(cog, member)
    db.set_custom_kit_cooldown("VIP", 60)
    assert "can claim it again" in _claim(cog, member)
    assert db.reset_custom_claims("VIP", 7) == 2
    assert "sent to" in _claim(cog, member)

    # Cooldown is per Discord account, not per in-game name
    assert "sent to" in _claim(cog, FakeInteraction(9, [55]), player="GamerTag42")
    assert db.minutes_since_custom_claim("vip", 9) is not None
    assert len(db.kit_claim_history("GamerTag42")) == 4
