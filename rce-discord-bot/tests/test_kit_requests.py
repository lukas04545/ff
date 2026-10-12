"""Bought custom kits: AI files a request, staff approves, the buyer gets the (linked or new) kit role."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest


class FakeRole:
    def __init__(self, rid, name, position):
        self.id, self.name, self.position, self.managed = rid, name, position, False
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    __hash__ = object.__hash__


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
                     SUPPORT_ROLE_IDS="42", DATABASE_PATH=str(tmp_path / "bot.db"), TICKET_CATEGORY_ID="5",
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from cogs.kit_requests import KitRequests
    from cogs.tickets import Tickets
    from cogs.timed_roles import TimedRoles
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    db = bot.db
    for kit in ("Ak Kit", "Mega raid kit"):
        db.create_custom_kit(kit, None, "admin")
        db.add_custom_kit_item(kit, "rifle.ak", 1)
    top, support = FakeRole(10, "Bot", 50), FakeRole(42, "Support", 20)
    existing = FakeRole(77, "Ak Kit", 5)                              # the server already has a role with that name
    roles = [existing, support]
    created = []

    async def create_role(name, reason=None):
        role = FakeRole(900 + len(created), name, 1)
        created.append(role)
        roles.append(role)
        return role
    guild = SimpleNamespace(id=1, name="Srv", me=SimpleNamespace(top_role=top), roles=roles, create_role=create_role)
    guild.get_role = lambda rid: next((r for r in roles if r.id == rid), None)
    buyer = SimpleNamespace(id=7, guild=guild, roles=[], mention="<@7>", display_name="Bob", name="bob",
                            __str__=lambda: "Bob")

    async def add_roles(role, reason=None):
        buyer.roles.append(role)
    buyer.add_roles = add_roles
    staff = SimpleNamespace(id=2, roles=[support], __str__=lambda: "Mod")
    guild.get_member = {7: buyer}.get
    posted, edits = [], []

    class Channel:
        id = 100

        def __init__(self):
            self.guild = guild

        async def send(self, content=None, embed=None, view=None, **kw):
            posted.append((content, embed, view))
            return SimpleNamespace(id=555 + len(posted), channel=SimpleNamespace(id=100))

        async def fetch_message(self, mid):
            async def edit(embed=None, view="same"):
                edits.append((embed, view))
            return SimpleNamespace(edit=edit)
    channel = Channel()
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel)
    tid = db.create_ticket(7, "I bought the AK kit", None)
    db.update_ticket(tid, channel_id=100)

    async def no_loop(self):
        return None
    monkeypatch.setattr(TimedRoles, "cog_load", no_loop)

    async def add():
        await bot.add_cog(CustomKits(bot))
        await bot.add_cog(TimedRoles(bot))
        await bot.add_cog(KitRequests(bot))
        t = Tickets(bot)
        await bot.add_cog(t)
        return t
    tickets = asyncio.run(add())
    yield SimpleNamespace(bot=bot, db=db, tickets=tickets, channel=channel, buyer=buyer, staff=staff, posted=posted,
                          edits=edits, created=created, existing=existing, ticket=db.ticket_by_channel(100),
                          requests=bot.get_cog("KitRequests"))
    db.close()


def tool(env, args):
    call = {"id": "c", "type": "function", "function": {"name": "request_kit_access", "arguments": json.dumps(args)}}
    return asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))


def test_ai_files_request_staff_approves_existing_role(env):
    assert "request_kit_access" in {t["function"]["name"] for t in env.tickets._ai_tools()}
    r = tool(env, {"kit": "Nope kit", "proof": "x"})
    assert r["result"] == "unknown_kit" and "Ak Kit" in r["available_custom_kits"]

    r = tool(env, {"kit": "ak kit", "proof": "Tebex order #1234, 10 EUR"})
    assert r["result"] == "waiting_for_staff" and r["kit"] == "Ak Kit"
    content, embed, view = env.posted[-1]
    assert content == "<@&42>" and "Tebex order #1234" in embed.fields[1].value and view is not None
    assert env.buyer.roles == []                                        # nothing given before approval
    assert tool(env, {"kit": "Ak Kit", "proof": "again"})["result"] == "already_waiting_for_staff"

    req_id = r["request_id"]
    ok, text = asyncio.run(env.requests.decide(req_id, True, env.staff))
    assert ok and env.existing in env.buyer.roles and env.created == []   # existing "Ak Kit" role linked, not created
    assert env.db.claim_roles(env.db.custom_kit("Ak Kit")) == [77]
    assert env.bot.get_cog("CustomKits").can_claim(env.buyer, env.db.custom_kit("Ak Kit"))
    assert "/customkit claim Ak Kit" in env.posted[-1][0] and env.edits[-1][1] is None   # buttons removed
    assert not asyncio.run(env.requests.decide(req_id, False, env.staff))[0]             # only once
    assert tool(env, {"kit": "Ak Kit", "proof": "bought again"})["result"] == "already_has_access"


def test_new_role_created_and_timed(env):
    r = tool(env, {"kit": "Mega raid kit", "proof": "Store order 99, 30 days", "days": 30})
    ok, text = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    assert ok and [x.name for x in env.created] == ["Mega raid kit"]     # created and linked
    role = env.created[0]
    assert role in env.buyer.roles and env.db.claim_roles(env.db.custom_kit("Mega raid kit")) == [role.id]
    row = env.db.timed_role(1, 7, role.id)
    assert row and abs(row["expires_at"] - (time.time() + 30 * 86400)) < 10


def test_reject(env):
    r = tool(env, {"kit": "Ak Kit", "proof": "trust me"})
    ok, _ = asyncio.run(env.requests.decide(r["request_id"], False, env.staff))
    assert ok and env.buyer.roles == [] and "not confirmed" in env.posted[-1][0]
    assert env.db.kit_request(r["request_id"])["status"] == "rejected"
    assert not env.requests.is_staff(env.buyer) and env.requests.is_staff(env.staff)
