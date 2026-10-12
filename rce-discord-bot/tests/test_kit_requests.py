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


def new_kit(env, args):
    call = {"id": "c", "type": "function", "function": {"name": "request_new_custom_kit",
                                                        "arguments": json.dumps(args)}}
    return asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))


ORDER = [{"item": "m249", "amount": 2}, {"item": "ammo.rocket.basic", "amount": 50},
         {"item": "High Quality Metal", "amount": 50000}, {"item": "rifle.l96", "amount": 5},
         {"item": "door.hinged.toptier", "amount": 10}, {"item": "wall.frame.garagedoor", "amount": 20}]


def test_find_items_and_unknown_entries(env):
    names = {t["function"]["name"] for t in env.tickets._ai_tools()}
    assert {"request_new_custom_kit", "find_items"} <= names
    call = {"id": "c", "type": "function", "function": {"name": "find_items", "arguments": '{"query": "garage"}'}}
    assert "wall.frame.garagedoor (Garage Door)" in asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))[
        "matches"]

    r = new_kit(env, {"items": ORDER + [{"item": "metal kit", "amount": 10}, {"item": "hv", "amount": 30},
                                        {"item": "rifle.ak", "amount": 0}], "proof": "Tebex #77"})
    assert r["result"] == "fix_items" and len(r["resolved"]) == len(ORDER)
    problems = {u["item"]: u for u in r["unknown"]}
    assert set(problems) == {"metal kit", "hv", "rifle.ak"}
    assert "ammo.rocket.hv (High Velocity Rocket)" in problems["hv"]["candidates"]
    assert "amount" in problems["rifle.ak"]["problem"]
    assert env.posted == [] and env.db.kit_requests(status="pending") == []         # nothing filed
    assert "error" in new_kit(env, {"items": [], "proof": "x"})


def test_new_kit_created_on_approval(env):
    r = new_kit(env, {"kit_name": "Zim raid kit", "items": ORDER, "proof": "Tebex order #555, 40 EUR"})
    assert r["result"] == "waiting_for_staff" and r["kit"] == "Zim raid kit"
    content, embed, view = env.posted[-1]
    assert content == "<@&42>" and view is not None and "new custom kit" in embed.description
    items_text = "\n".join(f.value for f in embed.fields)
    assert "(`lmg.m249`) ×2" in items_text and "(`metal.refined`) ×50,000" in items_text
    assert env.db.custom_kit("Zim raid kit") is None and env.buyer.roles == []      # nothing before approval

    # one purchase = one kit: no second new kit while this one waits
    assert new_kit(env, {"kit_name": "Zim raid kit", "items": ORDER[:1], "proof": "2nd order"})["result"] == \
        "already_waiting_for_staff"

    ok, text = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    assert ok, text
    kit = env.db.custom_kit("Zim raid kit")
    assert kit is not None
    assert {(i["shortname"], i["amount"]) for i in env.db.custom_kit_items("Zim raid kit")} == {
        (i["item"] if "." in i["item"] else {"m249": "lmg.m249", "High Quality Metal": "metal.refined"}[i["item"]],
         i["amount"]) for i in ORDER}
    role = env.created[-1]
    assert role.name == "Zim raid kit" and role in env.buyer.roles
    assert env.db.claim_roles(kit) == [role.id]
    assert env.bot.get_cog("CustomKits").can_claim(env.buyer, kit)
    assert "/customkit claim Zim raid kit" in env.posted[-1][0]
    # numbered when the name is taken by an existing kit
    assert new_kit(env, {"kit_name": "Ak Kit", "items": ORDER[:1], "proof": "3rd order"})["kit"] == "Ak Kit 2"


def test_new_kit_name_taken_meanwhile(env):
    r = new_kit(env, {"kit_name": "Late kit", "items": ORDER[:2], "proof": "order 1"})
    env.db.create_custom_kit("Late kit", "made by an admin", "admin")
    ok, text = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    assert not ok and "exists by now" in text and env.buyer.roles == []
    assert env.db.kit_request(r["request_id"])["status"] == "pending"               # reopened for staff
    assert env.db.custom_kit_items("Late kit") == []                                # admin's kit untouched


def test_new_kit_retry_after_failed_role_step(env, monkeypatch):
    r = new_kit(env, {"items": ORDER[:2], "proof": "order 2"})
    assert r["kit"] == "Bob kit"
    import discord

    async def forbidden(guild, kit):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    real = env.requests.kit_role
    monkeypatch.setattr(env.requests, "kit_role", forbidden)
    ok, text = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    assert not ok and env.db.kit_request(r["request_id"])["status"] == "pending"
    monkeypatch.setattr(env.requests, "kit_role", real)
    ok, text = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))    # staff fixed it, approves again
    assert ok, text
    assert len(env.db.custom_kit_items("Bob kit")) == 2 and env.created[-1] in env.buyer.roles


def test_ai_test_runs_like_a_ticket_without_doing_anything(env):
    """/ticket ai-test uses the real prompt + tools, but actions are only simulated."""
    seen = {}
    replies = iter([
        {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "find_items", "arguments": '{"query": "garage"}'}}]},
        {"content": "", "tool_calls": [{"id": "2", "type": "function", "function": {
            "name": "request_new_custom_kit", "arguments": json.dumps({"items": ORDER, "proof": "Tebex #1"})}}]},
        {"content": '{"reply": "Filed for staff approval!", "escalate": false, "reason": ""}', "tool_calls": []},
    ])

    async def chat(messages, tools=None, json_mode=True):
        seen.setdefault("system", messages[0]["content"])
        seen["tools"] = {t["function"]["name"] for t in tools or []}
        return next(replies)
    env.tickets.ai = SimpleNamespace(enabled=True, model="test-model", chat=chat)
    sent = []

    async def defer(**kw):
        return None

    async def followup_send(text, **kw):
        sent.append(text)
    interaction = SimpleNamespace(user=env.staff, guild=env.channel.guild,
                                  response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send))
    env.staff.display_name = "Mod"
    asyncio.run(env.tickets.ai_test.callback(env.tickets, interaction, "I bought M249 x2 and 50 rockets"))
    assert "request_new_custom_kit" in seen["tools"] and "request_new_custom_kit" in seen["system"]
    assert "Filed for staff approval!" in sent[0] and "find_items `garage`" in sent[0]
    assert "lmg.m249 ×2" in sent[0] and "nothing was executed" in sent[0]
    assert env.posted == [] and env.db.kit_requests(status="pending") == []          # nothing filed for real


def test_ai_error_is_never_silent(env, monkeypatch):
    async def boom(channel):
        raise RuntimeError("tool crashed")
    monkeypatch.setattr(env.tickets, "_ai_reply", boom)
    env.channel.mention = "#ticket"
    env.tickets._staff_roles = lambda guild: []

    async def no_log(guild, **kw):
        return None
    env.tickets._log = no_log
    asyncio.run(env.tickets._debounced_reply(env.channel, env.tickets._generation[env.channel.id], 0))
    assert any("ran into an error" in (c or "") for c, _, _ in env.posted)
    assert env.db.ticket_by_channel(100)["escalated"]


def test_chosen_approver_roles(env):
    """KIT_APPROVER_ROLE_IDS: pinged in the ticket, get access to it, and only they (+ admins) can decide."""
    from dataclasses import replace
    from cogs.kit_requests import KitRequestView
    approver_role = FakeRole(88, "Store team", 3)
    env.channel.guild.roles.append(approver_role)
    env.bot.config = replace(env.bot.config, kit_approver_role_ids=frozenset({88}))
    env.channel.overwrites = {}
    perms = []

    async def set_permissions(target, **kw):
        perms.append((target, kw))
        env.channel.overwrites[target] = kw
    env.channel.set_permissions = set_permissions

    r = tool(env, {"kit": "Ak Kit", "proof": "Tebex #9"})
    assert env.posted[-1][0] == "<@&88>"                                       # approvers pinged, not support
    assert perms and perms[0][0] is approver_role and perms[0][1]["view_channel"]
    tool(env, {"kit": "Mega raid kit", "proof": "Tebex #10"})
    assert len(perms) == 1                                                     # access only added once

    approver = SimpleNamespace(id=3, roles=[approver_role], __str__=lambda: "Store")
    assert not env.requests.can_decide(env.staff) and env.requests.can_decide(approver)
    assert not env.requests.can_decide(env.buyer)
    answers = []

    async def send_message(text, **kw):
        answers.append(text)
    interaction = SimpleNamespace(user=env.staff, message=SimpleNamespace(id=env.db.kit_request(r["request_id"])[
        "message_id"]), response=SimpleNamespace(send_message=send_message))
    asyncio.run(KitRequestView(env.requests)._decide(interaction, True))
    assert "Only <@&88>" in answers[0] and env.db.kit_request(r["request_id"])["status"] == "pending"
    ok, _ = asyncio.run(env.requests.decide(r["request_id"], True, approver))
    assert ok and env.existing in env.buyer.roles


def test_aiconfirm_keeps_ai_running_and_allows_exception(env):
    """/aiconfirm: staff tells the AI something, the AI stays on, a blocked second kit can be filed, approve works."""
    from types import SimpleNamespace as NS
    db = env.db
    db.update_ticket(env.ticket["id"], staff_joined=1)                  # AI was paused by a staff message
    first = new_kit(env, {"kit_name": "First", "items": ORDER[:1], "proof": "order tbx-1a2b3c"})
    assert first["result"] == "waiting_for_staff"
    assert new_kit(env, {"kit_name": "Second", "items": ORDER[:1], "proof": "order tbx-1a2b3c"})["result"] == \
        "already_waiting_for_staff"

    replies = []

    async def defer(**kw):
        return None

    async def followup(text, **kw):
        replies.append(text)

    async def send_message(text, **kw):
        replies.append(text)
    inter = NS(channel_id=100, channel=env.channel, user=env.staff,
               response=NS(defer=defer, send_message=send_message), followup=NS(send=followup))
    env.staff.display_name = "Mod"
    asyncio.run(env.tickets.aiconfirm.callback(env.tickets, inter, "Both purchases are verified, file the second kit",
                                               False))
    t = db.ticket_by_channel(100)
    assert t["staff_joined"] == 0 and t["ai_enabled"] == 1                # the AI keeps answering
    assert env.posted[-1][0].startswith("🛡️ Staff confirmation – **Mod:** Both purchases")

    second = new_kit(env, {"kit_name": "Second", "items": ORDER[:1], "proof": "order tbx-1a2b3c"})
    assert second["result"] == "waiting_for_staff"                         # exception allowed by staff
    assert "[allowed by staff via /aiconfirm]" in db.kit_request(second["request_id"])["proof"]

    # approve=True approves the ticket's open requests (approver check applies)
    buyer_inter = NS(channel_id=100, channel=env.channel, user=env.buyer,
                     response=NS(defer=defer, send_message=send_message), followup=NS(send=followup))
    asyncio.run(env.tickets.aiconfirm.callback(env.tickets, buyer_inter, "approve all", True))
    assert "Staff only" in replies[-1] and db.kit_request(first["request_id"])["status"] == "pending"
    asyncio.run(env.tickets.aiconfirm.callback(env.tickets, inter, "Approved, enjoy!", True))
    assert "several open requests" in replies[-1]                        # one purchase = one kit
    assert db.kit_request(first["request_id"])["status"] == "pending"
    # the AI cancels the old one (as staff asked), then approve:True approves the single open request
    call = {"id": "c", "type": "function", "function": {"name": "cancel_kit_request",
                                                        "arguments": json.dumps({"request_id": first["request_id"]})}}
    res = asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))
    assert res["result"] == "cancelled" and db.kit_request(first["request_id"])["status"] == "cancelled"
    assert "Mod's /aiconfirm" in db.kit_request(first["request_id"])["decided_by"]
    assert env.edits[-1][1] is None and "Cancelled" in env.edits[-1][0].title          # buttons removed
    asyncio.run(env.tickets.aiconfirm.callback(env.tickets, inter, "Approved, enjoy!", True))
    assert db.kit_request(second["request_id"])["status"] == "approved"
    assert replies[-1].count("✅ Request #") == 1

    # the AI sees the confirmation as a trusted message in its history
    msgs = [NS(author=NS(id=env.bot.user.id if env.bot.user else 0, bot=True), content=env.posted[i][0] or "",
               attachments=[]) for i in range(len(env.posted)) if (env.posted[i][0] or "").startswith("🛡️")]

    async def history(limit=None):
        for m in reversed(msgs):
            yield m
    env.channel.history = history
    from unittest import mock
    with mock.patch.object(type(env.bot), "user", new_callable=mock.PropertyMock) as user:
        user.return_value = NS(id=0)
        for m in msgs:
            m.author.id = 0
        hist = asyncio.run(env.tickets._history(env.channel, db.ticket_by_channel(100)))
    assert any(text.startswith("[Staff confirmation – from the team, trusted] **Mod:** Both purchases")
               for role, text in hist if role == "user")



def test_cancel_only_own_pending_requests(env):
    r = tool(env, {"kit": "Ak Kit", "proof": "Tebex #1"})
    call = lambda rid: {"id": "c", "type": "function", "function": {  # noqa: E731
        "name": "cancel_kit_request", "arguments": json.dumps({"request_id": rid})}}
    other = env.db.add_kit_request(None, 99, "Mega raid kit", 0, "someone else")
    res = asyncio.run(env.tickets._run_tool(call(other), env.ticket, env.channel))
    assert res["result"] == "not_cancellable" and env.db.kit_request(other)["status"] == "pending"
    assert [o["request_id"] for o in res["open_requests"]] == [r["request_id"]]
    res = asyncio.run(env.tickets._run_tool(call(r["request_id"]), env.ticket, env.channel))
    assert res["result"] == "cancelled"
    assert asyncio.run(env.tickets._run_tool(call(r["request_id"]), env.ticket, env.channel))["result"] == \
        "not_cancellable"                                                   # only once
    assert not asyncio.run(env.requests.decide(r["request_id"], True, env.staff))[0]   # can't approve it now


def test_order_ids_with_label():
    from cogs.kit_requests import order_ids
    assert order_ids("Receipt/order ID 1110065 (store purchase)") == {"1110065"}
    assert order_ids("T4s, order ID #1110065") == {"1110065"}
    assert order_ids("I bought 50000 hqm and 10 aks for 25 EUR on 12.10.2026") == set()
