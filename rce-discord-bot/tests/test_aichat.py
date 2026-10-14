"""AI chat: the AI talks with members in chosen channels, with the owner's own instructions."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from cogs.aichat import AIChat, clean_reply, guard_prompt, system_prompt, whole_sentences
from tests.test_kit_requests import env  # noqa: F401


def test_prompt_and_clean_reply():
    p = system_prompt("Rusty", "Kryvon", "Be sarcastic.", "Wipes are on Thursdays.", "Server online, 5/50")
    assert "You are Rusty" in p and "Kryvon" in p and "INSTRUCTIONS FROM THE SERVER OWNER" in p
    assert p.index("Be sarcastic.") < p.index("Wipes are on Thursdays.") and "Server online, 5/50" in p
    assert "INSTRUCTIONS FROM" not in system_prompt("x", "y")
    assert clean_reply('"Rusty: hey @everyone <@123> <@&5> look"', "Rusty") == "hey look"
    assert clean_reply("mail me a@b.c") == "mail me a@​b.c"
    assert clean_reply("   ") is None and len(clean_reply("word " * 1000)) <= 1802


@pytest.fixture
def chat(env, monkeypatch):
    env.bot.config = replace(env.bot.config, ai_chat_channel_ids=frozenset({600}), ai_chat_mode="mentions",
                             ai_chat_cooldown=10, ai_chat_name="Rusty", ai_chat_instructions="Talk like a pirate.",
                             ai_chat_knowledge=True)
    monkeypatch.setattr(type(env.bot), "user", property(lambda self: NS(id=999, display_name="RCE Bot")))
    cog = AIChat(env.bot)
    calls = []

    async def ai_chat(messages, **kw):
        calls.append(messages)
        return {"content": f"Arr, answer {len(calls)} @everyone", "tool_calls": []}
    env.tickets.ai = NS(enabled=True, chat=ai_chat)
    env.tickets._knowledge = lambda: "Wipes every Thursday."
    replies = []

    @asynccontextmanager
    async def typing():
        yield

    channel = NS(id=600, typing=typing)
    other = NS(id=601, typing=typing)
    guild = NS(id=1, name="Kryvon")

    def message(mid, text, uid=7, *, ch=channel, mentions=(), reply_to=None):
        async def reply(t, **kw):
            replies.append((mid, t, kw))
        author = NS(id=uid, bot=False, display_name=f"user{uid}")
        ref = NS(resolved=reply_to) if reply_to is not None else None
        return NS(id=mid, content=text, author=author, guild=guild, channel=ch, webhook_id=None,
                  mentions=list(mentions), reference=ref, reply=reply)
    return NS(cog=cog, env=env, calls=calls, replies=replies, message=message, other=other)


def send(c, msg):
    asyncio.run(c.cog.on_message(msg))


def test_answers_mentions_and_replies_only(chat, monkeypatch):
    import cogs.aichat as ac
    monkeypatch.setattr(ac, "CHANNEL_GAP", 0)
    me = NS(id=999)
    send(chat, chat.message(1, "anyone up for a raid?"))                      # not addressed: only remembered
    assert chat.calls == [] and chat.replies == []
    send(chat, chat.message(2, "<@999> when is the wipe?", mentions=[me]))
    assert len(chat.calls) == 1
    system, history, question = chat.calls[0]
    assert "You are Rusty" in system["content"] and "Talk like a pirate." in system["content"]
    assert "Wipes every Thursday." in system["content"]
    assert "user7: anyone up for a raid?" in history["content"]
    assert question["content"] == "user7 wrote to you: Rusty when is the wipe?"
    mid, text, kw = chat.replies[-1]
    assert mid == 2 and text == "Arr, answer 1" and kw["allowed_mentions"].everyone is False
    send(chat, chat.message(3, "and kits?", mentions=[me]))                    # cooldown per member (10 s)
    assert len(chat.calls) == 1
    bot_msg = NS(author=NS(id=999))
    monkeypatch.setattr(ac.discord, "Message", NS)                             # the fake counts as a message
    send(chat, chat.message(4, "thanks!", uid=8, reply_to=bot_msg))            # a reply to the bot counts
    assert len(chat.calls) == 2 and "Rusty: Arr, answer 1" in chat.calls[1][1]["content"]
    send(chat, chat.message(5, "<@999> hi", uid=9, ch=chat.other, mentions=[me]))   # not a chosen channel
    assert len(chat.calls) == 2


def test_mode_all_and_switched_off(chat, monkeypatch):
    import cogs.aichat as ac
    monkeypatch.setattr(ac, "CHANNEL_GAP", 0)
    chat.env.bot.config = replace(chat.env.bot.config, ai_chat_mode="all", ai_chat_cooldown=0,
                                  ai_chat_knowledge=False)
    send(chat, chat.message(1, "hello everyone"))
    send(chat, chat.message(2, "what's up", uid=8))
    assert len(chat.calls) == 2 and "KNOWLEDGE BASE" not in chat.calls[0][0]["content"]
    chat.env.tickets.ai = NS(enabled=False)
    send(chat, chat.message(3, "hello?"))
    assert len(chat.calls) == 2
    from config import required_intents
    assert "AI chat" in required_intents(chat.env.bot.config)["message_content"]


def test_guard_prompt_builds():
    p = guard_prompt("Warden", "Kryvon", "Be extra strict.", "Reason for jail: griefing. Time left: about 1h.")
    assert "You are Warden" in p and "prison guard" in p and "Reason for jail: griefing." in p
    assert "INSTRUCTIONS FROM THE SERVER OWNER" in p and "Be extra strict." in p


def test_prison_guard_answers_in_prison_channel(chat, monkeypatch):
    import cogs.aichat as ac
    monkeypatch.setattr(ac, "CHANNEL_GAP", 0)
    # Guard on, prison channel 700. AI chat itself stays on "mentions" and this channel is NOT in ai_chat_channel_ids.
    chat.env.bot.config = replace(chat.env.bot.config, ai_mod_guard=True, prison_channel_id=700, ai_chat_cooldown=0,
                                  ai_mod_instructions="Keep order firmly.")
    chat.env.db.jail(1, 7, [], "griefing other bases", "AI moderation")
    prison = NS(id=700)

    @asynccontextmanager
    async def typing():
        yield
    prison.typing = typing
    send(chat, chat.message(1, "let me out!!", ch=prison))                     # no @mention needed – guard answers all
    assert len(chat.calls) == 1
    system = chat.calls[0][0]["content"]
    assert "prison guard" in system and "Reason for jail: griefing other bases." in system
    assert "Keep order firmly." in system and "KNOWLEDGE BASE" not in system   # guard persona, not the chat persona
    assert chat.replies[-1][0] == 1
    # The prison channel is watched only while guard mode is on
    assert chat.cog.watching(700) and chat.cog.is_prison(700)
    chat.env.bot.config = replace(chat.env.bot.config, ai_mod_guard=False)
    assert not chat.cog.watching(700)
    from config import required_intents
    cfg = replace(chat.env.bot.config, ai_mod_guard=True)
    assert "AI prison guard" in required_intents(cfg)["message_content"]


def test_cut_off_reply_keeps_whole_sentences(chat, monkeypatch):
    import cogs.aichat as ac
    monkeypatch.setattr(ac, "CHANNEL_GAP", 0)
    assert whole_sentences("Pings are off the table. 🙂 That") == "Pings are off the table."
    assert whole_sentences("All good!") == "All good!" and whole_sentences("short") == "short"

    async def cut(messages, **kw):
        assert kw["max_tokens"] >= 1000
        return {"content": "Sure, wipes are on Thursday. Kits reset then too. And the", "finish_reason": "length"}
    chat.env.tickets.ai = NS(enabled=True, chat=cut)
    send(chat, chat.message(1, "<@999> wipe?", mentions=[NS(id=999)]))
    assert chat.replies[-1][1] == "Sure, wipes are on Thursday. Kits reset then too."
