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
    monkeypatch.setattr(bot, "get_cog", lambda name, _orig=bot.get_cog: NS(kit_names=NS(get=names,
                                                                                           items=["Raider", "Ak Kit"]))
                        if name == "Kits" else _orig(name))
    bot.online_players = {"Gamer7": {}}
    from dataclasses import replace
    bot.config = replace(bot.config, kit_roles_create=True)          # these tests let the bot make missing roles
    return NS(bot=bot, db=shop.db, guild=guild, buyer=shop.env.buyer, said=said, shop=shop,
              custom=bot.get_cog("CustomKits"), env=shop.env)


def test_every_kit_gets_a_role(kr):
    done = asyncio.run(kr.custom.sync_kit_roles())
    assert done == {"custom": 2, "ingame": 2, "unmatched": []}
    by_name = {r.name: r for r in kr.guild.roles}
    ak = kr.env.existing                                                  # the role "Ak Kit" already existed
    assert kr.db.claim_roles(kr.db.custom_kit("Ak Kit")) == [ak.id]
    assert kr.db.ingame_kit_role("Ak Kit")["role_id"] == ak.id             # same name: same role
    assert kr.db.ingame_kit_role("Raider")["role_id"] == by_name["Raider"].id   # created
    assert kr.db.claim_roles(kr.db.custom_kit("Mega raid kit")) == [by_name["Mega raid kit"].id]
    assert [c.name for c in kr.env.created] == ["Mega raid kit", "Raider"]
    assert asyncio.run(kr.custom.sync_kit_roles()) == {"custom": 0, "ingame": 0, "unmatched": []}   # nothing twice


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
            assert (await r.json())["done"] == {"custom": 0, "ingame": 0, "unmatched": []}
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())



def test_kit_role_names_are_compared_loosely():
    from support.kit_role_match import name_match, normalize
    roles = [(1, "M4 Spawn"), (2, "MP5 Roadsign"), (3, "Mega Build Kit"), (4, "Admin"), (5, "Mega Raid")]
    assert normalize("m4 spawn kit") == "m4 spawn"
    assert name_match("m4 spawn kit", roles) == 1
    assert name_match("mp5 roadsgin", roles) == 2                         # typo
    assert name_match("mega build kit", roles) == 3
    assert name_match("mega raid base", roles) is None                    # not sure – left to the AI / owner
    assert name_match("starter", roles) is None


def test_existing_roles_are_used_not_new_ones(kr):
    """Without KIT_ROLES_CREATE the sync only connects kits to existing roles (names compared loosely)."""
    from dataclasses import replace

    from tests.test_kit_requests import FakeRole
    kr.bot.config = replace(kr.bot.config, kit_roles_create=False)
    raider = FakeRole(300, "RAIDER kit", 3)
    kr.guild.roles.append(raider)
    done = asyncio.run(kr.custom.sync_kit_roles())
    assert done["ingame"] == 2 and done["custom"] == 1 and done["unmatched"] == ["Mega raid kit"]
    assert kr.db.ingame_kit_role("Raider")["role_id"] == 300 and kr.env.created == []      # nothing created


def test_ai_matches_cleanup_moves_members_and_deletes_duplicates(kr, monkeypatch):
    """The bot made roles although the server had its own – the AI picks the real ones, members move over and
    the duplicates are deleted; undo removes everything the bot made."""
    import json
    from dataclasses import replace

    from tests.test_kit_requests import FakeRole
    asyncio.run(kr.custom.sync_kit_roles())                                # old behaviour: made "Mega raid kit", "Raider"
    made = {r.name: r for r in kr.env.created}
    assert set(made) == {"Mega raid kit", "Raider"} and kr.db.created_role_ids() == {r.id for r in made.values()}
    deleted = []
    for r in kr.guild.roles:
        async def delete(reason=None, r=r):
            deleted.append(r.name)
            kr.guild.roles.remove(r)
        r.delete = delete
    real = FakeRole(400, "💥 Boom Squad", 3)                                # the owner's own role (AI only)
    kr.guild.roles.append(real)
    made["Mega raid kit"].members = [kr.buyer]                               # someone already has the duplicate
    kr.buyer.roles.append(made["Mega raid kit"])
    kr.bot.config = replace(kr.bot.config, kit_roles_create=False)

    class AI:
        enabled = True

        async def chat(self, messages, **kw):
            listing = json.loads(messages[1]["content"])
            assert "Mega raid kit" in listing["kits"] and all(r["id"] != str(made["Raider"].id) for r in listing["roles"])
            return {"content": json.dumps({"matches": [{"kit": "Mega raid kit", "role_id": "400"},
                                                       {"kit": "Raider", "role_id": None}]})}
    monkeypatch.setattr(kr.bot, "get_cog", lambda name, _orig=kr.bot.get_cog:
                        NS(ai=AI()) if name == "Tickets" else _orig(name))
    m = asyncio.run(kr.custom.propose_matches())
    props = {(p["kind"], p["kit"]): p for p in m["proposals"]}
    assert m["ai"] and props[("custom", "Mega raid kit")]["role_id"] == "400" and \
        props[("custom", "Mega raid kit")]["source"] == "ai"
    assert props[("custom", "Ak Kit")]["source"] == "linked"
    assert {c["name"] for c in m["created"]} == {"Mega raid kit", "Raider"}
    choices = [{"kind": p["kind"], "kit": p["kit"], "role_id": p["role_id"]} for p in m["proposals"]]
    out = asyncio.run(kr.custom.apply_matches(choices))
    assert kr.db.claim_roles(kr.db.custom_kit("Mega raid kit")) == [400]
    assert real in kr.buyer.roles and out["moved"] == 1                     # the member got the real role
    assert out["deleted"] == ["Mega raid kit"] and "Raider" not in deleted  # Raider: no match – left as it is
    assert kr.db.ingame_kit_role("Raider")["role_id"] == made["Raider"].id
    with pytest.raises(ValueError, match="can't be used"):                  # a bot-made role is never "existing"
        asyncio.run(kr.custom.apply_matches([{"kind": "ingame", "kit": "Raider", "role_id": str(made["Raider"].id)}]))
    r = asyncio.run(kr.custom.undo_created())                                # remove everything the bot made
    assert r["deleted"] == ["Raider"] and kr.db.ingame_kit_role("Raider") is None
    assert kr.db.created_role_ids() == set()


def test_kit_database(kr):
    """One searchable list of every kit with contents, role, cooldown, auto-give, store and claims; the role of a
    kit can be changed there."""
    from dataclasses import replace

    from aiohttp.test_utils import TestClient, TestServer

    from tests.test_kit_requests import FakeRole
    H = {"X-Requested-With": "rce-web"}
    bot, db = kr.bot, kr.db
    bot.config = replace(bot.config, web_password="secret123")
    asyncio.run(kr.custom.sync_kit_roles())
    db.add_store_kit("Raider pack", "Raider", 4.5, 0, None, source="ingame")
    db.record_ingame_claim("Raider", "Gamer7", 7, "test")
    vip = FakeRole(500, "VIP", 2)
    kr.guild.roles.append(vip)

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/kit-db")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/kit-db")).json()
            kits = {(k["kind"], k["name"]): k for k in d["kits"]}
            assert set(kits) >= {("custom", "Ak Kit"), ("custom", "Mega raid kit"), ("ingame", "Raider"),
                                 ("ingame", "Ak Kit")}
            ak = kits[("custom", "Ak Kit")]
            assert ak["items"][0]["short"] == "rifle.ak" and ak["items"][0]["label"] and ak["item_count"] == 1
            raider = kits[("ingame", "Raider")]
            assert raider["store"]["price"] == 4.5 and raider["claims"]["total"] == 1 and raider["roles"][0]["name"]
            assert "VIP" in {r["name"] for r in d["roles"]}
            r = await c.put("/api/kit-roles", json={"kind": "custom", "kit": "Ak Kit", "role_id": "500"}, headers=H)
            assert r.status == 200 and db.claim_roles(db.custom_kit("Ak Kit")) == [500]
            r = await c.put("/api/kit-roles", json={"kind": "ingame", "kit": "Raider", "role_id": None}, headers=H)
            assert r.status == 200 and db.ingame_kit_role("Raider") is None
            r = await c.put("/api/kit-roles", json={"kind": "ingame", "kit": "Raider", "cooldown": "2h"}, headers=H)
            assert r.status == 400 and "role first" in (await r.json())["error"]
            r = await c.put("/api/kit-roles", json={"kind": "ingame", "kit": "Raider", "role_id": "500"}, headers=H)
            assert db.ingame_kit_role("Raider")["role_id"] == 500                 # role-less in-game kit: works
            r = await c.put("/api/kit-roles", json={"kind": "custom", "kit": "Ak Kit", "role_id": "999"}, headers=H)
            assert r.status == 400
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())
