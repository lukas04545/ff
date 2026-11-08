"""An emote bind fires however the server spelled the emote (d11_Wood / d11_wood / wood / the English text …),
and the bot shows the last emotes it received with what happened."""
from types import SimpleNamespace as NS

import pytest

from rce import log_parser as lp
from rce import quickchat

WOOD = "d11_quick_chat_i_need_phrase_format d11_Wood"


@pytest.mark.parametrize("logged", [
    WOOD, "d11_quick_chat_i_need_phrase_format d11_wood", "d11_quick_chat_i_need_phrase_format wood",
    "d11_quick_chat_i_need_phrase_format  d11_Wood ", "D11_QUICK_CHAT_I_NEED_PHRASE_FORMAT D11_WOOD", "I need Wood.",
    "I need wood"])
def test_every_spelling_is_the_same_emote(logged):
    ev = lp.parse_line(f"[CHAT GLOBAL] Ryvex : {logged}")
    assert ev.raw == WOOD and ev.text == "I need Wood."


def test_other_items_and_unknown_messages():
    assert quickchat.normalize("d11_quick_chat_i_need_phrase_format d11_Stone") == \
        "d11_quick_chat_i_need_phrase_format stones"
    assert quickchat.normalize("d11_quick_chat_activities_phrase_format stones") == \
        "d11_quick_chat_activities_phrase_format d11_Stone"
    assert quickchat.normalize("d11_quick_chat_responses_slot_2") == "d11_quick_chat_responses_slot_2"
    assert quickchat.normalize("something else") == "something else"
    assert quickchat.normalize("Yes") == "d11_quick_chat_responses_slot_0"


def test_the_bind_fires_and_the_board_shows_what_came_in(monkeypatch):
    import asyncio

    from cogs.kits import Kits
    tried = []
    rules = [{"id": 1, "kit": "Wood kit", "phrase": "d11_quick_chat_i_need_phrase_format d11_wood",   # odd saved
              "required_role_id": None, "cooldown_minutes": 0, "max_claims": 0, "kit_type": "ingame"}]
    db = NS(kit_rules=lambda **kw: rules)
    bot = NS(db=db)
    kits = Kits.__new__(Kits)
    kits.bot = bot
    from collections import deque
    kits.attempts, kits.seen = deque(maxlen=150), deque(maxlen=40)

    async def claim(rule, player, source):
        tried.append(rule["kit"])
        kits._attempt(rule, player, source, "given")
    monkeypatch.setattr(kits, "_try_claim", claim)
    asyncio.run(kits.on_rce_chat(lp.parse_line("[CHAT TEAM] Ryvex : d11_quick_chat_i_need_phrase_format wood")))
    asyncio.run(kits.on_rce_chat(lp.parse_line("[CHAT GLOBAL] Ryvex : d11_quick_chat_responses_slot_1")))
    assert tried == ["Wood kit"]
    assert [s["result"] for s in kits.seen] == ["Wood kit: given", "no kit bound to this emote"]
    assert kits.seen[0]["emote"] == "I need Wood."


def test_the_servers_own_messages_stay_as_they_are():
    assert lp.parse_line("[CHAT SERVER] SERVER : Hello").raw == "Hello"
