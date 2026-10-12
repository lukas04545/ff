"""The AI asks "Is your issue resolved?" after the player went quiet; yes closes the ticket."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.test_kit_requests import env  # noqa: F401  (fixture: bot, tickets cog, ticket channel, buyer)


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes!", True), ("yep thanks", True), ("ja passt", True), ("resolved ty", True), ("👍", True),
    ("thanks", True), ("ok", False), ("ok I'll try", False), ("yes it doesn't work", False),
    ("no", False), ("yes but I still need the kit", False), ("yes?", False), ("ok where do I find it", False),
    ("what do you mean", False), ("", False), ("yes " + "really " * 8, False), ("nein", False),
])
def test_is_yes(text, expected):
    from cogs.tickets import is_yes
    assert is_yes(text) is expected


@pytest.fixture
def setup(env, monkeypatch):
    import cogs.tickets as tickets_mod
    from dataclasses import replace
    env.bot.config = replace(env.bot.config, ticket_resolve_minutes=5)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tickets_mod.asyncio, "sleep", no_sleep)
    closed = []

    async def fake_close(channel, ticket, closer, reason):
        closed.append((ticket["id"], closer, reason))
    monkeypatch.setattr(env.tickets, "_close", fake_close)
    env.closed = closed
    return env


def ask(env):
    t = env.tickets
    asyncio.run(t._resolve_check(env.channel, t._generation[env.channel.id]))


def owner_says(env, text):
    msg = SimpleNamespace(author=SimpleNamespace(id=7, bot=False, roles=[]), guild=env.channel.guild,
                          channel=env.channel, content=text)
    asyncio.run(env.tickets.on_message(msg))


def test_asks_and_typed_yes_closes(setup):
    env = setup
    ask(env)
    content, _, view = env.posted[-1]
    assert "Is your issue resolved?" in content and view is not None
    ask(env)
    assert len(env.posted) == 1                                    # asked only once
    owner_says(env, "yes thanks!")
    assert env.closed and env.closed[0][2] == "Resolved (confirmed by the player)"
    assert env.edits[-1][1] is None                                # buttons removed from the question


def test_other_answer_keeps_ticket_open(setup):
    env = setup
    ask(env)
    owner_says(env, "no, I still don't have my kit")
    assert env.closed == [] and env.channel.id not in env.tickets._resolve_asked
    owner_says(env, "yes")                                         # "yes" without an open question: no close
    assert env.closed == []


def test_buttons(setup):
    env = setup
    ask(env)
    answers = []

    def interaction(user):
        async def edit_message(content=None, view="same", **kw):
            answers.append((content, view))

        async def send_message(text, **kw):
            answers.append((text, "ephemeral"))
        return SimpleNamespace(user=user, channel_id=env.channel.id, channel=env.channel,
                               response=SimpleNamespace(edit_message=edit_message, send_message=send_message))
    stranger = SimpleNamespace(id=999, roles=[])
    asyncio.run(env.tickets.resolve_answer(interaction(stranger), True))
    assert "Only the ticket creator" in answers[-1][0] and env.closed == []
    asyncio.run(env.tickets.resolve_answer(interaction(env.buyer), False))
    assert "**No**" in answers[-1][0] and answers[-1][1] is None and env.closed == []
    ask(env)
    asyncio.run(env.tickets.resolve_answer(interaction(env.buyer), True))
    assert "**Yes**" in answers[-1][0] and env.closed[0][1] is env.buyer


def test_not_asked_when_staff_involved_or_purchase_pending_or_player_wrote(setup):
    env = setup
    t = env.tickets
    gen = t._generation[env.channel.id]
    t._generation[env.channel.id] += 1                            # the player wrote again meanwhile
    asyncio.run(t._resolve_check(env.channel, gen))
    assert env.posted == []

    env.db.add_kit_request(env.ticket["id"], 7, "Ak Kit", 0, "Tebex #1")
    ask(env)
    assert env.posted == []                                        # purchase waits for staff

    env.db.decide_kit_request(1, "rejected", "Mod")
    env.db.update_ticket(env.ticket["id"], escalated=1)
    ask(env)
    assert env.posted == []

    env.db.update_ticket(env.ticket["id"], escalated=0)
    from dataclasses import replace
    env.bot.config = replace(env.bot.config, ticket_resolve_minutes=0)
    ask(env)
    assert env.posted == []                                        # turned off
    env.bot.config = replace(env.bot.config, ticket_resolve_minutes=5)
    ask(env)
    assert "Is your issue resolved?" in env.posted[-1][0]
