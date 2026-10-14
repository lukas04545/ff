"""The AI plugin creator: parse the model's reply, review it for safety, and the generate helper."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from support.plugin_ai import extract, generate_plugin, review

GOOD_CODE = '''from plugin_api import Plugin, Setting


class Greeter(Plugin):
    NAME = "Greeter"
    DESCRIPTION = "Greets players."
    SETTINGS = (Setting("text", "Message", default="Hi {player}!"),)

    @Plugin.listener()
    async def on_rce_player_join(self, player):
        await self.say(self.setting("text").format(player=player))
'''


def test_extract_handles_clean_and_messy_json():
    raw = json.dumps({"filename": "My Greeter!.py", "name": "Greeter", "summary": "Greets joiners.  Set a text.",
                      "code": GOOD_CODE})
    out = extract(raw)
    assert out["filename"] == "my_greeter.py" and out["name"] == "Greeter"       # slugged, .py added once
    assert out["summary"] == "Greets joiners. Set a text." and out["code"].startswith("from plugin_api")
    # Wrapped in prose / code fences: the JSON is still pulled out
    messy = "Sure! Here you go:\n```json\n" + raw + "\n```"
    assert extract(messy)["name"] == "Greeter"
    # No code -> error
    with pytest.raises(ValueError):
        extract(json.dumps({"name": "x"}))
    with pytest.raises(ValueError):
        extract("not json at all")
    # A bad filename falls back to a safe default
    assert extract(json.dumps({"filename": "!!!", "code": "x = 1"}))["filename"] == "ai_plugin.py"


def test_review_flags_unsafe_and_broken_code():
    assert review(GOOD_CODE) == []                                               # clean
    assert any("line" in w for w in review("def broken(:\n"))                    # syntax error reported
    bad = "import os\nimport subprocess\nos.system('rm -rf /')\n"
    warnings = " ".join(review(bad))
    assert "subprocess" in warnings and "os.system" in warnings                  # forbidden names flagged
    assert "requests" in " ".join(review("import requests\nx = 1\n"))            # non-allowlisted import flagged
    assert review("import discord\nimport asyncio\nx = 1\n") and "No class" in review("x = 1\n")[0]


def test_generate_plugin_happy_path():
    seen = []

    async def chat_json(messages, **kw):
        seen.append(messages)
        return json.dumps({"filename": "greeter.py", "name": "Greeter", "summary": "Greets players.",
                           "code": GOOD_CODE})
    ai = NS(enabled=True, chat_json=chat_json)
    out = asyncio.run(generate_plugin(ai, "greet players when they join"))
    assert out["filename"] == "greeter.py" and out["warnings"] == [] and "class Greeter" in out["code"]
    assert "Create a plugin: greet players when they join" in seen[0][1]["content"]
    assert "derived from `Plugin`" in seen[0][0]["content"]                       # the API reference is sent


def test_generate_plugin_errors():
    ai = NS(enabled=True, chat_json=None)
    with pytest.raises(ValueError, match="more detail"):
        asyncio.run(generate_plugin(ai, "hi"))
    with pytest.raises(ValueError, match="AI is off"):
        asyncio.run(generate_plugin(NS(enabled=False), "a real description here"))

    async def boom(messages, **kw):
        from support.deepseek import DeepSeekError
        raise DeepSeekError("no balance")
    with pytest.raises(ValueError, match="could not create"):
        asyncio.run(generate_plugin(NS(enabled=True, chat_json=boom), "a real description here"))
