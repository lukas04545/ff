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


def test_parse_close_flag():
    from support.prompt import parse_answer
    assert parse_answer('{"reply": "Glad it works, bye!", "escalate": false, "close": true}').close
    assert parse_answer('{"reply": "Bye", "escalate": false, "close": "true"}').close
    assert not parse_answer('{"reply": "Hi", "escalate": false}').close
    assert not parse_answer('{"reply": "Getting staff", "escalate": true, "reason": "x", "close": true}').close


def _ai_says(env, monkeypatch, raw):
    async def chat_json(messages):
        return raw

    async def history(channel, ticket):
        return [("user", "Bob: all fixed now, thanks!")]
    env.tickets.ai = SimpleNamespace(enabled=True, model="m", chat_json=chat_json)
    monkeypatch.setattr(env.tickets, "_ai_tools", lambda: [])
    monkeypatch.setattr(env.tickets, "_history", history)
    env.channel.typing = lambda: _NoCtx()
    env.channel.guild.get_member = {7: env.buyer}.get
    asyncio.run(env.tickets._ai_reply(env.channel))


class _NoCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_player_says_resolved_closes(setup, monkeypatch):
    env = setup
    _ai_says(env, monkeypatch, '{"reply": "Great, glad it is fixed! Closing the ticket.", "escalate": false, '
                               '"reason": "", "close": true}')
    assert "glad it is fixed" in env.posted[-1][0]
    assert env.closed == [(env.ticket["id"], env.buyer, "Resolved (confirmed by the player)")]


def test_player_says_resolved_but_purchase_pending(setup, monkeypatch):
    env = setup
    env.db.add_kit_request(env.ticket["id"], 7, "Ak Kit", 0, "Tebex #1")
    _ai_says(env, monkeypatch, '{"reply": "Bye!", "escalate": false, "reason": "", "close": true}')
    assert env.closed == [] and "waiting for staff approval" in env.posted[-1][0]
    owner_says(env, "yes")                                         # typed yes doesn't close either
    assert env.closed == []


def test_parse_keep_open_flag():
    from support.prompt import build_system_prompt, parse_answer
    a = parse_answer('{"reply": "Sure, it stays open.", "escalate": false, "keep_open": true}')
    assert a.keep_open and not a.close
    both = parse_answer('{"reply": "x", "escalate": false, "keep_open": true, "close": true}')
    assert both.keep_open and not both.close                       # keeping open wins over a mixed answer
    prompt = build_system_prompt(server_name="S", topic="t", ingame_name=None, status="", knowledge="",
                                 keep_open=True)
    assert "Kept open on request: yes" in prompt and '"keep_open"' in prompt


def test_player_asks_to_keep_open(setup, monkeypatch):
    env = setup
    ask(env)                                                       # an open "resolved?" question …
    _ai_says(env, monkeypatch, '{"reply": "No problem, I will keep it open.", "escalate": false, '
                               '"reason": "", "keep_open": true}')
    assert env.db.ticket_by_channel(100)["keep_open"] == 1
    assert env.channel.id not in env.tickets._resolve_asked and env.edits[-1][1] is None   # … loses its buttons
    n = len(env.posted)
    ask(env)
    assert len(env.posted) == n and env.closed == []               # no more questions
    # Later they explicitly ask to close -> closes
    _ai_says(env, monkeypatch, '{"reply": "Closing it now, bye!", "escalate": false, "reason": "", "close": true}')
    assert env.closed and env.closed[0][2] == "Resolved (confirmed by the player)"


def test_keep_open_button(setup):
    env = setup
    ask(env)
    answers = []

    async def edit_message(content=None, view="same", **kw):
        answers.append((content, view))
    interaction = SimpleNamespace(user=env.buyer, channel_id=env.channel.id, channel=env.channel,
                                  response=SimpleNamespace(edit_message=edit_message))
    asyncio.run(env.tickets.resolve_answer(interaction, None))
    assert "stays open" in answers[-1][0] and answers[-1][1] is None and env.closed == []
    assert env.db.ticket_by_channel(100)["keep_open"] == 1
    n = len(env.posted)
    ask(env)
    assert len(env.posted) == n
