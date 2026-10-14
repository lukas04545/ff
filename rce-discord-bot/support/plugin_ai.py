"""Let the integrated AI (DeepSeek) write a plugin from a plain-language description.

The web interface → 🧩 Plugins → "Create with AI" sends a description here; the AI returns one plugin file
(a single class derived from Plugin, see plugin_api.py / PLUGINS.md). The code is NOT installed automatically –
the owner reviews it in the window and then clicks "Create", which runs the normal upload (so the plugin lands
switched off in data/plugins/ and is validated like any uploaded file).
"""
from __future__ import annotations

import ast
import json
import re

from support.deepseek import DeepSeekClient, DeepSeekError

# A compact reference of the plugin API, given to the model so it only uses things that really exist.
PLUGIN_REFERENCE = """A plugin is ONE Python file with exactly ONE class derived from `Plugin`:

    from plugin_api import Plugin, Setting
    import discord
    from discord import app_commands
    from discord.ext import tasks

    class Example(Plugin):
        NAME = "Example"                 # short human name
        DESCRIPTION = "What it does."     # one sentence
        VERSION = "1.0"
        ICON = "🧩"                       # one emoji
        INTENTS = ()                      # add "members" and/or "message_content" ONLY if really needed
        DEFAULT_ENABLED = False           # keep False for generated plugins
        SETTINGS = (
            Setting("message", "Message", default="Hi {player}!"),
            Setting("channel", "Channel", kind="channel"),
            Setting("amount", "Amount", kind="int", default=10, min=1, max=1000),
            Setting("mode", "Mode", kind="choice", choices=("a", "b"), default="a"),
        )

        async def on_load(self): ...                 # optional: called when switched on
        async def on_settings_changed(self): ...      # optional: after settings are saved in the web UI
        def problems(self): return []                 # optional: setup warnings shown in the web UI
        def status(self): return {"sent": 0}          # optional: numbers on the plugin card

        @Plugin.listener()
        async def on_rce_player_join(self, player): ...   # player is the in-game name (str)

        @app_commands.command(name="example", description="...")
        async def example(self, interaction: discord.Interaction): ...

SETTING kinds: text, textarea, int (min/max), bool, choice (choices=(...)), channel, role, kit, image.
Read a setting with self.setting("key"). An "image" setting's file: self.file("key") -> Path | None.

Helpers on self (use ONLY these, they all exist):
  await self.say(text)                 -> in-game chat message from the server
  await self.give_kit(player, kit)      -> give a custom or in-game kit to a player
  self.channel(id, *fallbacks)          -> a discord channel object from a channel id (or None)
  await self.post(channel, content, embed=None, ping_role=None)  -> send a message (safe mentions)
  self.online_players()                 -> list[str] of online in-game names
  self.online_name(player)              -> exact online name (case-insensitive) or None
  self.linked_user(player)              -> Discord user id linked to an in-game player, or None
  self.linked_player(user_id)           -> in-game name linked to a Discord user, or None
  self.points_enabled                   -> bool (points & shop feature on)
  self.give_points(user_id, n, reason)  -> add points, returns new balance or None
  self.take_points(user_id, n, reason)  -> spend points if enough, new balance or None
  self.fmt_points(n)                    -> "1 000 points"
  self.data.get/set/delete/items/clear  -> persistent key/value storage for this plugin (JSON values)
  self.guild                            -> the discord.Guild (or None)
  self.server_name                      -> str
  self.audit(text)                      -> write to the admin log
  self.log.info(...)                    -> logging
  self.now()                            -> int epoch seconds
  self.md(text)                         -> escape Discord markdown

Game events you can listen to (@Plugin.listener()):
  on_rce_player_join(player), on_rce_player_leave(player), on_rce_kill(killer, victim),
  plus all normal discord.py events (on_member_join(member), on_message(message), ...).
tasks.loop works and is started/stopped automatically.

RULES:
- Output ONE file, ONE class derived from Plugin. No top-level code that runs on import (no network, no file
  writes, no `while True`, no `open(...)`, no `os.system`, no `subprocess`, no `eval`/`exec`).
- Only import: plugin_api, discord, discord.ext (commands, tasks), discord.app_commands, and the Python standard
  library (asyncio, time, random, datetime, json, re, math). Do NOT import anything else.
- Keep it self-contained and safe. Prefer settings over hard-coded values. Comments and text in English."""

SYSTEM = (
    "You write plugins for a Rust Console Edition Discord bot. You are given a description and must reply with "
    "ONE working plugin file.\n\n" + PLUGIN_REFERENCE + "\n\n"
    'Reply ONLY with JSON: {"filename": "snake_case.py", "name": "Human Name", "summary": "one sentence on what '
    'it does and any setup the owner must do", "code": "the full Python file"}. The filename uses a-z, 0-9 and _ '
    "only. The code must be the complete file, ready to save."
)

# Imports a generated plugin is allowed to use (checked before we hand the code back).
ALLOWED_IMPORTS = {
    "plugin_api", "discord", "discord.ext", "discord.app_commands", "asyncio", "time", "random", "datetime",
    "json", "re", "math", "collections", "dataclasses", "typing", "enum", "string", "textwrap", "itertools",
    "functools", "__future__",
}
# Names that have no place in a small game-server plugin – a strong signal of unsafe code.
FORBIDDEN_NAMES = ("os.system", "subprocess", "eval(", "exec(", "__import__", "importlib", "open(", "socket",
                   "shutil", "pickle", "marshal", "ctypes", "pty", "compile(")

FILENAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(name or "").strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug[:40]


def extract(raw: str) -> dict:
    """Parses the model's JSON reply into {filename, name, summary, code}; raises ValueError if unusable."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        start, end = (raw or "").find("{"), (raw or "").rfind("}")
        try:
            data = json.loads(raw[start:end + 1]) if 0 <= start < end else {}
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        raise ValueError("The AI did not return a plugin.")
    code = str(data.get("code") or "").strip()
    if not code:
        raise ValueError("The AI returned no code – try again or rephrase the description.")
    name = str(data.get("name") or "").strip()[:60]
    raw_name = str(data.get("filename") or name)
    raw_name = raw_name[:-3] if raw_name.lower().endswith(".py") else raw_name
    filename = _slug(raw_name) or "ai_plugin"
    if not FILENAME_RE.match(filename):
        filename = "ai_plugin"
    summary = " ".join(str(data.get("summary") or "").split())[:400]
    return {"filename": f"{filename}.py", "name": name or filename, "summary": summary, "code": code}


def review(code: str) -> list[str]:
    """Light safety/sanity checks on generated code. Returns a list of warnings (empty = looks fine).

    This is a convenience check for the review window, not a sandbox – the owner still reviews the code and the
    normal plugin loader validates it on install."""
    warnings: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Python error in line {exc.lineno}: {exc.msg}"]
    has_plugin_class = any(isinstance(node, ast.ClassDef) and node.bases for node in ast.walk(tree))
    if not has_plugin_class:
        warnings.append("No class found – a plugin needs one class derived from Plugin.")
    # Imported top-level modules must be on the allow-list.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            continue
        for mod in mods:
            root = mod.split(".")[0]
            if mod not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                warnings.append(f"Unusual import '{mod}' – check this before you create the plugin.")
    for bad in FORBIDDEN_NAMES:
        if bad in code:
            warnings.append(f"Contains '{bad}' – check this before you create the plugin.")
    # De-duplicate, keep order
    seen: set[str] = set()
    return [w for w in warnings if not (w in seen or seen.add(w))]


async def generate_plugin(ai: DeepSeekClient, description: str) -> dict:
    """Asks the AI for a plugin. Returns {filename, name, summary, code, warnings}."""
    description = " ".join(str(description or "").split())
    if len(description) < 5:
        raise ValueError("Describe the plugin in a bit more detail.")
    if ai is None or not getattr(ai, "enabled", False):
        raise ValueError("The AI is off – set a DeepSeek API key first.")
    try:
        raw = await ai.chat_json([{"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": f"Create a plugin: {description[:2000]}"}],
                                 max_tokens=4000, temperature=0.3)
    except DeepSeekError as exc:
        raise ValueError(f"The AI could not create the plugin: {exc}") from None
    out = extract(raw)
    out["warnings"] = review(out["code"])
    return out
