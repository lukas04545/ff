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


def test_auto_give_to_members_with_the_role(kr, monkeypatch):
    import cogs.custom_kits as ck
    asyncio.run(kr.custom.sync_kit_roles())
    raider = next(r for r in kr.guild.roles if r.name == "Raider")
    raider.members = [kr.buyer]
    db = kr.db
    kr.buyer.roles.append(raider)
    db.set_ingame_kit_cooldown("Raider", 0)
    db.set_auto_give("ingame", "Raider", True)
    assert asyncio.run(kr.custom.auto_give())["given"] == []                     # no cooldown: never automatic
    db.set_ingame_kit_cooldown("Raider", 60)
    out = asyncio.run(kr.custom.auto_give())
    assert out["given"] == [{"kit": "Raider", "player": "Gamer7"}] and kr.said[-1] == 'kit givetoplayer "Raider" "Gamer7"'
    assert asyncio.run(kr.custom.auto_give()) == {"given": [], "waiting": 1}   # cooldown runs
    table = {k["kit"]: k for k in kr.custom.kit_role_table()["kits"] if k["kind"] == "ingame"}
    row = table["Raider"]
    assert row["auto_give"] and row["cooldown_minutes"] == 60 and row["member_count"] == 1
    assert row["members"][0]["player"] == "Gamer7" and row["members"][0]["online"] and row["members"][0]["ready_at"]
    assert table["Ak Kit"]["member_count"] == 0 and not table["Ak Kit"]["auto_give"]
    # Joining: given after a short delay (the player has to load in first)
    db.reset_ingame_claims("Raider")
    slept = []

    async def no_sleep(s):
        slept.append(s)
    monkeypatch.setattr(ck.asyncio, "sleep", no_sleep)
    asyncio.run(kr.custom.on_rce_player_join("Gamer7"))
    assert slept == [ck.AUTO_GIVE_JOIN_DELAY] and kr.said.count('kit givetoplayer "Raider" "Gamer7"') == 2
    kr.buyer.roles.remove(raider)                                                # no role, no kit
    db.reset_ingame_claims("Raider")
    assert asyncio.run(kr.custom.auto_give())["given"] == []


def test_kit_roles_in_the_control_panel(kr):
    from dataclasses import replace

    from aiohttp.test_utils import TestClient, TestServer
    H = {"X-Requested-With": "rce-web"}
    bot = kr.bot
    bot.config = replace(bot.config, web_password="secret123")
    asyncio.run(kr.custom.sync_kit_roles())
    raider = next(r for r in kr.guild.roles if r.name == "Raider")
    raider.members = [kr.buyer]
    kr.buyer.roles.append(raider)

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/kit-roles")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/kit-roles")).json()
            kits = {(k["kind"], k["kit"]): k for k in d["kits"]}
            assert kits[("ingame", "Raider")]["roles"][0]["name"] == "Raider" and d["members_intent"] is True   # requested
            r = await c.put("/api/kit-roles", json={"kind": "ingame", "kit": "raider", "cooldown": "2h",
                                                    "auto_give": True}, headers=H)
            k = next(k for k in (await r.json())["kits"] if k["kit"] == "Raider")
            assert k["cooldown_minutes"] == 120 and k["auto_give"]
            r = await c.post("/api/kit-roles/give", json={"kind": "ingame", "kit": "Raider"}, headers=H)
            assert (await r.json())["given"] == [{"kit": "Raider", "player": "Gamer7"}]
            r = await c.put("/api/kit-roles", json={"kind": "ingame", "kit": "Nope"}, headers=H)
            assert r.status == 404
            r = await c.post("/api/kit-roles/sync", headers=H)
            assert (await r.json())["done"] == {"custom": 0, "ingame": 0}
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())
