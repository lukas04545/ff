"""Guild fence for tenants on the SHARED bot (one Discord bot token for many tenants).

Every tenant on the shared bot runs its own process with the same token. Without sharding, Discord sends every
connection the events of EVERY guild the shared bot is in – and lets every connection act in every guild. The fence
makes each process blind and powerless outside its own guild (SAAS_GUILD_ID), at the lowest level:

* incoming: every gateway event parser is wrapped. Events of other guilds are dropped before discord.py builds
  objects – also slash commands, buttons and modals (those bypass the normal event dispatch) – so another tenant's
  messages, members and button clicks never reach this process. READY keeps only its own guild; events without a
  guild (DMs) are dropped, except a few account-level ones.
* outgoing: every REST call is checked. Routes with a guild id must be its own guild; routes with a channel id must
  be a channel of its own guild (or a DM it opened); global application commands can't be changed (they'd change
  every tenant's commands) and the list of the bot's guilds can't be read.
A tenant with its OWN bot (custom token, paid add-on) doesn't need the fence – nobody else is on that token.
"""
from __future__ import annotations

import logging
import os
import re

import discord

log = logging.getLogger("rce.fence")

SHARED_ENV, GUILD_ENV = "SAAS_SHARED_BOT", "SAAS_GUILD_ID"
ID_IS_GUILD = {"GUILD_CREATE", "GUILD_UPDATE", "GUILD_DELETE"}
NO_GUILD_OK = {"READY", "RESUMED", "USER_UPDATE", "APPLICATION_COMMAND_PERMISSIONS_UPDATE"}
# matched against the real request URL (route.path is the unformatted template, e.g. "{application_id}")
GLOBAL_COMMANDS = re.compile(r"/applications/\d+/commands(?:/|\?|$)")
MY_GUILDS = re.compile(r"/users/@me/guilds(?:/|\?|$)")


class FenceError(discord.DiscordException):
    """A request outside this tenant's guild was refused."""


def active() -> bool:
    return os.environ.get("RCE_SAAS") == "1" and os.environ.get(SHARED_ENV) == "1"


def guild_id() -> int | None:
    raw = os.environ.get(GUILD_ENV, "")
    return int(raw) if raw.isdigit() else None


def _event_guild(name: str, data) -> str | None:
    if not isinstance(data, dict):
        return None
    if name in ID_IS_GUILD:
        return data.get("id")
    return data.get("guild_id")


def wrap_parsers(parsers: dict, gid: int) -> None:
    for name, fn in list(parsers.items()):
        if getattr(fn, "_fenced", False):
            continue

        def parser(data, _name=name, _fn=fn):
            if _name == "READY" and isinstance(data, dict):
                data["guilds"] = [g for g in data.get("guilds", []) if str(g.get("id")) == str(gid)]
                return _fn(data)
            g = _event_guild(_name, data)
            if g is None:
                return _fn(data) if _name in NO_GUILD_OK else None
            if str(g) != str(gid):
                return None
            return _fn(data)
        parser._fenced = True
        parsers[name] = parser


def allowed(route, bot, gid: int) -> bool:
    url = getattr(route, "url", "") or getattr(route, "path", "")
    method = getattr(route, "method", "GET").upper()
    if route.guild_id is not None and str(route.guild_id) != str(gid):
        return False
    if route.channel_id is not None:
        channel = bot.get_channel(int(route.channel_id))
        if isinstance(channel, discord.DMChannel):
            return True
        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != gid:
            return False
    if MY_GUILDS.search(url):
        return False
    if GLOBAL_COMMANDS.search(url) and method != "GET":
        return False
    return True


def install(bot) -> bool:
    """Puts the fence around the bot (before it connects). Returns True if active."""
    gid = guild_id()
    if not active():
        return False
    if gid is None:
        raise RuntimeError("Shared-bot tenant without SAAS_GUILD_ID – refusing to start unfenced.")
    wrap_parsers(bot._connection.parsers, gid)
    original = bot.http.request

    async def request(route, **kwargs):
        if not allowed(route, bot, gid):
            log.warning("Fence: refused %s %s (outside guild %s)", route.method, route.path, gid)
            raise FenceError(f"Refused: {route.method} {route.path} is outside this server's Discord.")
        return await original(route, **kwargs)
    bot.http.request = request
    log.info("Shared bot: fenced to guild %s", gid)
    return True
