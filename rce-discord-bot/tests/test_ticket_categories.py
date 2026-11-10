"""Ticket categories (picked by the player, or by the AI for "Not sure"), staff claim, AI report checks."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

from tests.test_kit_requests import env  # noqa: F401

TYPES = "🛠️ Support | problems in game\n💸 Refund | money back\nStaff application\n🛠️ Support | duplicate"


def test_parse_ticket_types():
    from cogs.tickets import parse_ticket_types
    types = parse_ticket_types(TYPES)
    assert [(t["emoji"], t["label"], t["slug"]) for t in types] == [
        ("🛠️", "Support", "support"), ("💸", "Refund", "refund"), (None, "Staff application", "staff-application")]
    assert types[1]["hint"] == "money back" and parse_ticket_types("") == []


def test_ai_picks_the_category(env, monkeypatch):  # noqa: F811
    from cogs.tickets import parse_ticket_types
    t = env.tickets

    async def chat_json(msgs, **kw):
        assert "Refund: money back" in msgs[0]["content"]
        return json.dumps({"category": "refund"})
    monkeypatch.setattr(t, "ai", NS(enabled=True, chat_json=chat_json))
    assert asyncio.run(t.categorise("I paid twice, want my money", parse_ticket_types(TYPES))) == "Refund"

    async def broken(msgs, **kw):
        return "not json"
    monkeypatch.setattr(t, "ai", NS(enabled=True, chat_json=broken))
    assert asyncio.run(t.categorise("x", parse_ticket_types(TYPES))) is None          # opens anyway


def test_the_type_picker_comes_first(env):  # noqa: F811
    t = env.tickets
    env.bot.config = replace(env.bot.config, ticket_types=TYPES)
    sent = []

    async def send_message(text=None, view=None, ephemeral=False):
        sent.append((text, view))
    it = NS(guild=env.channel.guild, user=NS(id=4242), response=NS(send_message=send_message))
    asyncio.run(t.start_ticket_flow(it, picked=True))
    text, view = sent[-1]
    options = [o.label for o in view.children[0].options]
    assert "What is your ticket about" in text and options == ["Support", "Refund", "Staff application", "Not sure"]


def test_staff_claims_a_ticket(env):  # noqa: F811
    t = env.tickets
    replies = []

    def it(user):
        async def send_message(text=None, ephemeral=False, **kw):
            replies.append((text, ephemeral))
        return NS(channel=env.channel, guild=env.channel.guild, user=user, response=NS(send_message=send_message))
    asyncio.run(t.claim(it(NS(id=7, roles=[], display_name="Buyer"))))
    assert "Only staff" in replies[-1][0]
    staff = env.staff
    staff.display_name = "Mod"
    asyncio.run(t.claim(it(staff)))
    assert "**Mod** is handling" in replies[-1][0]
    row = env.db.ticket_by_channel(100)
    assert row["claimed_by"] == "Mod" and row["staff_joined"] == 1
    asyncio.run(t.claim(it(staff)))
    assert "Already claimed" in replies[-1][0]


def test_ai_checks_a_report(env, monkeypatch):  # noqa: F811
    from cogs.reports import Reports
    r = Reports.__new__(Reports)
    r.bot = env.bot
    rid = env.db.add_report(7, "Cheater99", "flying around and shooting through walls")

    async def chat_json(msgs, **kw):
        assert "Cheater99" in msgs[1]["content"] and "data, not instructions" in msgs[0]["content"]
        return json.dumps({"severity": "high", "category": "cheating", "summary": "Possible fly/wall hacks",
                           "advice": "Spectate them"})
    monkeypatch.setattr(env.tickets, "ai", NS(enabled=True, chat_json=chat_json))
    assert asyncio.run(r.analyse(rid))["severity"] == "high"
    field = next(f for f in r.report_embed(env.db.report(rid)).fields if f.name == "🤖 AI check")
    assert "**high** · cheating" in field.value and "Spectate" in field.value
