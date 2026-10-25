"""Every kit has its own Discord role (KIT_ROLES): roles found or created for custom and in-game kits, the role lets
members claim the kit (in-game kits too, with a cooldown), losing it removes access, buying a kit gives its role."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from tests.test_kit_requests import env  # noqa: F401
from tests.test_webshop import shop  # noqa: F401


@pytest.fixture
def kr(shop, monkeypatch):  # noqa: F811
    bot, guild = shop.env.bot, shop.env.buyer.guild
    said = []

    async def command(cmd, **kw):
        said.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    monkeypatch.setattr(type(bot.rcon), "connected", property(lambda self: True))

    async def names(**kw):
        return ["Raider", "Ak Kit"]
    monkeypatch.setattr(bot, "get_cog", lambda name, _orig=bot.get_cog: NS(kit_names=NS(get=names))
                        if name == "Kits" else _orig(name))
    bot.online_players = {"Gamer7": {}}
    return NS(bot=bot, db=shop.db, guild=guild, buyer=shop.env.buyer, said=said, shop=shop,
              custom=bot.get_cog("CustomKits"), env=shop.env)


def test_every_kit_gets_a_role(kr):
    done = asyncio.run(kr.custom.sync_kit_roles())
    assert done == {"custom": 2, "ingame": 2}
    by_name = {r.name: r for r in kr.guild.roles}
    ak = kr.env.existing                                                  # the role "Ak Kit" already existed
    assert kr.db.claim_roles(kr.db.custom_kit("Ak Kit")) == [ak.id]
    assert kr.db.ingame_kit_role("Ak Kit")["role_id"] == ak.id             # same name: same role
    assert kr.db.ingame_kit_role("Raider")["role_id"] == by_name["Raider"].id   # created
    assert kr.db.claim_roles(kr.db.custom_kit("Mega raid kit")) == [by_name["Mega raid kit"].id]
    assert [c.name for c in kr.env.created] == ["Mega raid kit", "Raider"]
    assert asyncio.run(kr.custom.sync_kit_roles()) == {"custom": 0, "ingame": 0}   # nothing twice


def test_the_role_lets_members_claim_an_ingame_kit(kr):
    asyncio.run(kr.custom.sync_kit_roles())
    raider = next(r for r in kr.guild.roles if r.name == "Raider")
    member = kr.buyer
    out = asyncio.run(kr.custom.claim_for(member, "Raider", via="test"))
    assert not out.ok and out.code == "no_access"                         # no role, no kit
    member.roles.append(raider)
    assert "Raider" in [k["kit"] for k in kr.custom.kit_overview(member)["claimable_kits"]]
    out = asyncio.run(kr.custom.claim_for(member, "raider", via="test"))
    assert out.ok and kr.said[-1] == 'kit givetoplayer "Raider" "Gamer7"'
    out = asyncio.run(kr.custom.claim_for(member, "Raider", via="test"))
    assert out.code == "cooldown" and out.ready_at                        # default: once a day
    kr.db.set_ingame_kit_cooldown("Raider", 0)
    assert asyncio.run(kr.custom.claim_for(member, "Raider", via="test")).ok
    member.roles.remove(raider)                                            # role gone: access gone
    assert asyncio.run(kr.custom.claim_for(member, "Raider", via="test")).code == "no_access"
    kr.bot.config = kr.bot.config.__class__(**{**kr.bot.config.__dict__, "kit_roles": False})
    member.roles.append(raider)
    assert asyncio.run(kr.custom.claim_for(member, "Raider", via="test")).code == "no_access"   # switched off


def test_buying_an_ingame_kit_gives_its_role(kr):
    asyncio.run(kr.custom.sync_kit_roles())
    raider = next(r for r in kr.guild.roles if r.name == "Raider")
    db, s = kr.db, kr.shop.s
    kit_id = db.add_store_kit("Raider", "Raider", 5, 7, None, source="ingame")
    code = s.create_gift_cards(20, 1, "test")[0]
    r = asyncio.run(s.checkout("kit", base_url="https://x", kit_id=kit_id, player="Gamer7", gift_code=code))
    order = db.store_order_by("token", r["token"])
    assert order["status"] == "delivered" and order["result"] == "Raider + role Raider for 7 days"
    assert raider in kr.buyer.roles and db.timed_role(1, 7, raider.id)    # the store kit's 7 days
    out = asyncio.run(kr.custom.claim_for(kr.buyer, "Raider", via="test"))
    assert out.code == "cooldown"                                         # just given: the cooldown runs
