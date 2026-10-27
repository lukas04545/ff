"""One Discord bot for several Rust servers (servers.py).

Extra servers set to "use the main Discord bot" log in with the main server's bot token, so every server's process
receives every Discord event. This router (installed in each of them) lets exactly one process handle each event:

* Slash commands have an optional "server" choice (added by the main server when it registers the commands).
  Without it, the command goes to the server whose channel it was used in, else to the main server; a command only
  one server has goes there. The option is removed from the event before discord.py sees it, so no command needs
  to know about it.
* Buttons, menus and modals go to the server that sent that message (every message with buttons a process sends is
  noted in a shared list), else the server whose channel it is.
* Messages, edits and reactions go to the server whose channel it is: the channels set in that server's settings and
  the channels it created (tickets). Every other channel belongs to the main server.
* Member and server events reach every server – each acts only on what its own settings ask for.
Only the main server registers slash commands with Discord: the union of every server's commands, each with the
"server" choice when more than one server has it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import servers
from envfile import read_env
from settings_schema import FIELDS

log = logging.getLogger("rce.router")

OPTION = "server"
OPTION_HELP = "Which Rust server (default: the one of this channel, else the main server)"
CHANNEL_KEYS = tuple(f.key for f in FIELDS if f.pick in ("text_channel", "category"))
CHANNEL_EVENTS = ("MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE", "MESSAGE_DELETE_BULK", "TYPING_START",
                  "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE", "MESSAGE_REACTION_REMOVE_ALL",
                  "MESSAGE_REACTION_REMOVE_EMOJI", "MESSAGE_POLL_VOTE_ADD", "MESSAGE_POLL_VOTE_REMOVE")
REFRESH = 30                 # seconds: re-read which channels belong to which server, and the commands
INTERACTION_DAYS = 2         # answered commands are remembered this long (buttons on their replies)


# ------------------------------------------------------------ Who takes part

def shared_bot_servers() -> list[str]:
    """Extra servers that use the main server's Discord bot."""
    return [s["id"] for s in servers.load_registry() if s.get("discord") == "shared" and s.get("enabled", True)]


def participants() -> list[str]:
    return [servers.MAIN_ID] + shared_bot_servers()


def uses_main_bot() -> bool:
    """This process is an extra server on the main server's Discord bot."""
    import os
    return servers.is_child() and os.environ.get(servers.ENV_SHARED_BOT) == "1"


def commands_dir() -> Path:
    return servers.shared_data() / "commands"


# ------------------------------------------------------------ Shared list: messages, answered commands, channels

class RouteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            for table in ("messages", "interactions", "channels"):
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, sid TEXT NOT NULL, "
                                 "at INTEGER NOT NULL)")

    def note(self, table: str, item_id: int | str, sid: str) -> None:
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO {table} (id, sid, at) VALUES (?, ?, ?)",
                             (int(item_id), sid, int(time.time())))

    def owner(self, table: str, item_id: Any) -> str | None:
        try:
            key = int(item_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._db.execute(f"SELECT sid FROM {table} WHERE id = ?", (key,)).fetchone()
        return row[0] if row else None

    def prune(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM interactions WHERE at < ?", (int(time.time()) - INTERACTION_DAYS * 86400,))

    def close(self) -> None:
        self._db.close()


# ------------------------------------------------------------ Slash command payloads (main server registers them)

def write_commands(sid: str, payload: list[dict]) -> None:
    folder = commands_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{sid}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(folder / f"{sid}.json")


def read_commands(sid: str) -> list[dict] | None:
    """The slash commands a server registered (None: it hasn't yet – e.g. still starting)."""
    try:
        data = json.loads((commands_dir() / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return [c for c in data if isinstance(c, dict) and c.get("name")] if isinstance(data, list) else []


def _invocables(command: dict) -> list[dict]:
    """The parts of a slash command that take options: the command itself, or each (nested) subcommand."""
    options = command.get("options") or []
    if command.get("type", 1) != 1:
        return []                                  # context menus take no options
    if options and options[0].get("type") in (1, 2):
        out = []
        for sub in options:
            if sub.get("type") == 1:
                out.append(sub)
            elif sub.get("type") == 2:
                out.extend(s for s in sub.get("options") or [] if s.get("type") == 1)
        return out
    return [command]


def merged_payload(own: list[dict], others: dict[str, list[dict]], names: dict[str, str]) -> list[dict]:
    """The main server's commands plus those only extra servers have; a "server" choice on every command that more
    than one server has. `others`: sid -> payload, `names`: sid -> server name."""
    by_name: dict[str, dict] = {}
    holders: dict[str, list[str]] = {}
    for sid, payload in [(servers.MAIN_ID, own), *others.items()]:
        for cmd in payload:
            key = f"{cmd.get('type', 1)}:{cmd['name']}"
            by_name.setdefault(key, json.loads(json.dumps(cmd)))
            holders.setdefault(key, []).append(sid)
    out = []
    for key, cmd in by_name.items():
        sids = list(dict.fromkeys(holders[key]))
        if len(sids) > 1:
            option = {"type": 3, "name": OPTION, "description": OPTION_HELP, "required": False,
                      "choices": [{"name": names.get(s, servers.MAIN_NAME if s == servers.MAIN_ID else s)[:100],
                                   "value": s} for s in sids][:25]}
            for part in _invocables(cmd):
                opts = part.setdefault("options", [])
                if len(opts) < 25 and not any(o.get("name") == OPTION for o in opts):
                    opts.append(dict(option))
        out.append(cmd)
    return out


def take_server_option(data: dict) -> str | None:
    """Removes the "server" option from an incoming command (wherever it is) and returns its value."""
    def walk(options: list) -> str | None:
        for opt in list(options):
            if opt.get("type") in (1, 2):
                found = walk(opt.get("options") or [])
                if found is not None:
                    return found
            elif opt.get("name") == OPTION and opt.get("type") == 3:
                options.remove(opt)
                return str(opt.get("value") or "")
        return None
    return walk(data.get("options") or [])


# ------------------------------------------------------------ The bot's status line (the main server shows them all)

def write_presence(sid: str, name: str, state: dict) -> None:
    folder = servers.shared_data() / "presence"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{sid}.tmp"
    tmp.write_text(json.dumps({"name": name, "state": state, "at": time.time()}), encoding="utf-8")
    tmp.replace(folder / f"{sid}.json")


def read_presences(max_age: float) -> list[tuple[str, dict]]:
    """(name, state) of every extra server on this bot; one that stopped reporting counts as offline."""
    out = []
    for sid in shared_bot_servers():
        try:
            data = json.loads((servers.shared_data() / "presence" / f"{sid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        fresh = time.time() - float(data.get("at") or 0) < max_age
        out.append((str(data.get("name") or servers.server_name(sid))[:40],
                    data.get("state") if fresh and isinstance(data.get("state"), dict) else {"online": False}))
    return out


# ------------------------------------------------------------ The router

class Router:
    def __init__(self, bot, sid: str | None = None, store: RouteStore | None = None) -> None:
        self.bot = bot
        self.sid = sid or servers.server_id()
        self.store = store or RouteStore(servers.shared_data() / "discord_routes.db")
        self._channels: dict[int, str] = {}
        self._commands: dict[str, set[str] | None] = {}
        self._loaded = 0.0
        self.installed = False

    # --- who owns what

    def _refresh(self) -> None:
        if time.monotonic() - self._loaded < REFRESH and self._loaded:
            return
        self._loaded = time.monotonic()
        channels: dict[int, str] = {}
        commands: dict[str, set[str] | None] = {}
        for sid in participants():                   # main first: a channel set on two servers stays the main's
            env = read_env(servers.env_path(sid))
            for key in CHANNEL_KEYS:
                for part in str(env.get(key) or "").split(","):
                    if part.strip().isdigit():
                        channels.setdefault(int(part.strip()), sid)
            listed = read_commands(sid)
            commands[sid] = None if listed is None else {c["name"] for c in listed}
        self._channels, self._commands = channels, commands

    def channel_owner(self, channel_id: Any) -> str:
        self._refresh()
        made = self.store.owner("channels", channel_id)
        if made in participants():
            return made
        try:
            return self._channels.get(int(channel_id), servers.MAIN_ID)
        except (TypeError, ValueError):
            return servers.MAIN_ID

    def has_command(self, sid: str, name: str) -> bool:
        if sid == self.sid:
            return any(c.name == name for c in self.bot.tree.get_commands())
        listed = self._commands.get(sid)
        return True if listed is None else name in listed       # not listed yet: assume it has every command

    def command_target(self, payload: dict) -> str:
        data = payload.get("data") or {}
        wanted = take_server_option(data)
        members = participants()
        if wanted in members:
            return wanted
        target = self.channel_owner(payload.get("channel_id") or (payload.get("channel") or {}).get("id"))
        name = data.get("name", "")
        if target in members and (self.has_command(target, name) or target == servers.MAIN_ID
                                  and not any(self.has_command(s, name) for s in members)):
            return target
        return next((s for s in members if self.has_command(s, name)), servers.MAIN_ID)

    def _has_view_for(self, message_id: Any) -> bool:
        store = getattr(getattr(self.bot, "_connection", None), "_view_store", None)
        if store is None:
            return False
        try:
            key = int(message_id)
        except (TypeError, ValueError):
            return False
        return key in getattr(store, "_synced_message_views", {}) or key in getattr(store, "_views", {})

    def interaction_owner(self, payload: dict) -> str | None:
        kind = payload.get("type")
        if kind in (2, 4):
            return self.command_target(payload)
        message = payload.get("message") or {}
        if kind == 5:                                 # modal: the process that opened it knows its custom_id
            store = getattr(getattr(self.bot, "_connection", None), "_view_store", None)
            custom_id = (payload.get("data") or {}).get("custom_id")
            return self.sid if store is not None and custom_id in getattr(store, "_modals", {}) else None
        if message and self._has_view_for(message.get("id")):
            return self.sid
        owner = self.store.owner("messages", message.get("id"))
        meta = message.get("interaction_metadata") or message.get("interaction") or {}
        owner = owner or self.store.owner("interactions", meta.get("id"))
        if owner in participants():
            return owner
        return self.channel_owner(payload.get("channel_id") or message.get("channel_id"))

    def event_owner(self, event: str, payload: dict) -> str:
        if event.startswith("MESSAGE_REACTION"):
            owner = self.store.owner("messages", payload.get("message_id"))
            if owner in participants():
                return owner
        return self.channel_owner(payload.get("channel_id"))

    # --- hooks into discord.py

    def install(self) -> None:
        """Filters incoming events and notes what this process sends. Safe to call more than once."""
        if self.installed:
            return
        self.installed = True
        parsers: dict[str, Callable] = self.bot._connection.parsers
        original = parsers.get("INTERACTION_CREATE")

        def interaction(data: dict) -> None:
            try:
                owner = self.interaction_owner(data)
            except Exception:
                log.exception("Routing an interaction failed – handling it on the main server")
                owner = servers.MAIN_ID
            if owner != self.sid:
                return
            if data.get("type") == 2:
                try:
                    self.store.note("interactions", data["id"], self.sid)   # buttons on its reply come back here
                except (KeyError, ValueError, sqlite3.Error):
                    pass
            original(data)
        if original is not None:
            parsers["INTERACTION_CREATE"] = interaction

        for event in CHANNEL_EVENTS:
            handler = parsers.get(event)
            if handler is None:
                continue

            def filtered(data: dict, _handler=handler, _event=event) -> None:
                try:
                    mine = self.event_owner(_event, data) == self.sid
                except Exception:
                    mine = self.sid == servers.MAIN_ID
                if mine:
                    _handler(data)
            parsers[event] = filtered

        http = self.bot.http
        request = http.request

        async def noting_request(route, **kwargs):
            result = await request(route, **kwargs)
            try:
                self.note_sent(route.method, route.path, result)
            except (sqlite3.Error, TypeError, ValueError, KeyError):
                log.debug("Could not note a sent message", exc_info=True)
            return result
        http.request = noting_request

    def note_sent(self, method: str, path: str, result: Any) -> None:
        """What this process created: messages with buttons (and replies to commands), channels, threads."""
        if method not in ("POST", "PATCH") or not isinstance(result, dict):
            return
        if path in ("/guilds/{guild_id}/channels", "/channels/{channel_id}/threads",
                    "/channels/{channel_id}/messages/{message_id}/threads"):
            if "id" in result and "type" in result:
                self.store.note("channels", result["id"], self.sid)
            return
        message = (result.get("resource") or {}).get("message") if "resource" in result else result
        if isinstance(message, dict) and message.get("id") and message.get("components") and "channel_id" in message:
            self.store.note("messages", message["id"], self.sid)


def active_router(bot) -> Router | None:
    return getattr(bot, "router", None)


def setup(bot) -> Router | None:
    """Installs the router if this process shares its Discord bot: an extra server on the main bot, or the main
    server while extra servers use its bot."""
    router = getattr(bot, "router", None)
    if router is not None:
        return router
    if not (uses_main_bot() or (not servers.is_child() and shared_bot_servers())):
        return None
    router = Router(bot)
    router.install()
    bot.router = router
    log.info("One Discord bot for several servers: routing events (this is %s).", servers.server_name(router.sid))
    return router
