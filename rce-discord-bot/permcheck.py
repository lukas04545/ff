"""Check the bot's Discord permissions and explain "403 Forbidden" in plain words.

* explain_forbidden(): turns discord.Forbidden into a message saying WHAT was denied and WHICH permission is missing.
* check_permissions(): checks all configured channels/roles at startup and lists missing permissions
  (bot window + web interface Overview).
The permission names match Discord's English UI.
"""
from __future__ import annotations

import re

import discord

PERM_LABELS = {
    "view_channel": "View Channels",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "read_message_history": "Read Message History",
    "manage_channels": "Manage Channels",
    "manage_roles": "Manage Roles",
    "manage_messages": "Manage Messages",
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "moderate_members": "Timeout Members",
    "add_reactions": "Add Reactions",
}

SEND = ("view_channel", "send_messages", "embed_links")
FIX_HINT = ("Fix: Server Settings → Roles → the bot's role → enable the permissions and drag the role "
            "as high as possible. If the channel/category has its own permissions, allow the bot's role "
            "there too.")

# (HTTP method, path regex, action, required permissions, role hierarchy relevant)
_ROUTES: list[tuple[str, str, str, tuple[str, ...], bool]] = [
    ("POST", r"/guilds/\d+/channels$", "create channel",
     ("view_channel", "manage_channels", "send_messages", "embed_links", "attach_files", "read_message_history"), False),
    ("PUT", r"/channels/(?P<ch>\d+)/permissions/\d+$", "set channel permissions",
     ("view_channel", "manage_roles", "manage_channels"), False),
    ("DELETE", r"/channels/(?P<ch>\d+)/permissions/\d+$", "remove channel permissions",
     ("view_channel", "manage_roles", "manage_channels"), False),
    ("PATCH", r"/channels/(?P<ch>\d+)$", "edit channel", ("view_channel", "manage_channels"), False),
    ("DELETE", r"/channels/(?P<ch>\d+)$", "delete channel", ("view_channel", "manage_channels"), False),
    ("POST", r"/channels/(?P<ch>\d+)/messages/bulk-delete$", "delete messages",
     ("view_channel", "manage_messages", "read_message_history"), False),
    ("POST", r"/channels/(?P<ch>\d+)/messages$", "send message", SEND + ("attach_files",), False),
    ("PATCH", r"/channels/(?P<ch>\d+)/messages/\d+$", "edit message", SEND, False),
    ("DELETE", r"/channels/(?P<ch>\d+)/messages/\d+$", "delete message", ("view_channel", "manage_messages"), False),
    ("GET", r"/channels/(?P<ch>\d+)/messages", "read messages", ("view_channel", "read_message_history"), False),
    ("PUT", r"/channels/(?P<ch>\d+)/messages/\d+/reactions/", "add reaction",
     ("view_channel", "add_reactions", "read_message_history"), False),
    ("PUT", r"/guilds/\d+/members/\d+/roles/\d+$", "assign role", ("manage_roles",), True),
    ("DELETE", r"/guilds/\d+/members/\d+/roles/\d+$", "remove role", ("manage_roles",), True),
    ("PUT", r"/guilds/\d+/bans/\d+$", "ban member", ("ban_members",), True),
    ("DELETE", r"/guilds/\d+/bans/\d+$", "unban member", ("ban_members",), False),
    ("DELETE", r"/guilds/\d+/members/\d+$", "kick member", ("kick_members",), True),
    ("PATCH", r"/guilds/\d+/members/\d+$", "set/remove timeout", ("moderate_members",), True),
]


def perm_names(perms) -> str:
    return ", ".join(f'"{PERM_LABELS.get(p, p)}"' for p in perms)


def main_guild(bot) -> discord.Guild | None:
    """The bot's Discord server: GUILD_ID, otherwise its only server."""
    if bot.config.guild_id:
        return bot.get_guild(bot.config.guild_id)
    return bot.guilds[0] if len(bot.guilds) == 1 else None


def explain_forbidden(exc: discord.HTTPException, bot=None) -> str:
    """Message for Discord error 403: which action, where, which permission is missing."""
    response = getattr(exc, "response", None)
    method = str(getattr(response, "method", "") or "").upper()
    url = getattr(response, "url", None)
    path = re.sub(r"^/api/v\d+", "", url.path if url is not None else "")
    code = getattr(exc, "code", 0)
    detail = f"Discord error {code}: {exc.text}" if code else "Discord error 403"

    if code == 50007:
        return f"⚠️ Discord won't allow a direct message to this user (DMs closed). ({detail})"

    route = next(((action, perms, hierarchy, re.search(rx, path))
                  for m, rx, action, perms, hierarchy in _ROUTES
                  if m == method and re.search(rx, path)), None)
    if route is None:
        return (f"⚠️ Discord denied the action ({detail}, {method} {path or '?'}). "
                "The bot is missing permissions or its role is too low.\n" + FIX_HINT)

    action, perms, hierarchy, match = route
    channel_id = match.groupdict().get("ch") if match else None
    where = f" in <#{channel_id}>" if channel_id else ""
    lines = [f"⚠️ Discord denied **{action}**{where} ({detail})."]

    # If possible, determine the permissions actually missing (from the bot's cache)
    missing: list[str] | None = None
    guild = main_guild(bot) if bot is not None else None
    if guild is not None and guild.me is not None:
        scope = guild.get_channel(int(channel_id)) if channel_id else None
        have = scope.permissions_for(guild.me) if scope is not None else guild.me.guild_permissions
        missing = [p for p in perms if not getattr(have, p, True)]
    if code == 50001:
        lines.append(f"The bot can't see the channel – it's missing {perm_names(['view_channel'])} there.")
    elif missing:
        lines.append(f"The bot is missing{where or ' on the server'}: {perm_names(missing)}.")
    elif missing is None or not hierarchy:
        lines.append(f"Required permissions{where}: {perm_names(perms)}.")
    if hierarchy:
        lines.append("Important: in the role list, the bot's role must be **above** the role or member "
                     "it's supposed to change. No bot can change the server owner.")
    lines.append(FIX_HINT)
    return "\n".join(lines)


def check_permissions(bot) -> list[str]:
    """Checks all configured channels/roles. Returns readable problem texts (empty = all OK)."""
    cfg = bot.config
    guild = main_guild(bot)
    if guild is None:
        if cfg.guild_id:
            return [f"The bot is not on the Discord server with GUILD_ID {cfg.guild_id} – "
                    "invite it via the invite link or check GUILD_ID."]
        return []
    me = guild.me
    if me is None:
        return []
    problems: list[str] = []

    channels: list[tuple[str, int | None, tuple[str, ...]]] = [
        ("Status channel", cfg.status_channel_id, SEND + ("read_message_history",)),
        ("Chat bridge channel", cfg.chat_channel_id, SEND + ("read_message_history", "add_reactions")),
        ("Killfeed channel", cfg.killfeed_channel_id, SEND),
        ("Event channel", cfg.events_channel_id, SEND),
        ("Admin log channel", cfg.admin_log_channel_id, SEND),
        ("Console log channel", cfg.console_log_channel_id, SEND),
        ("Ticket log channel", cfg.ticket_log_channel_id, SEND + ("attach_files",)),
        ("Discord log channel", cfg.discord_log_channel_id, SEND),
        ("Welcome channel", cfg.welcome_channel_id, SEND),
        ("Rules channel", cfg.rules_channel_id, SEND),
        ("Ticket category", cfg.ticket_category_id,
         ("view_channel", "manage_channels", "send_messages", "embed_links", "attach_files",
          "read_message_history")),
    ]
    for label, channel_id, perms in channels:
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:
            problems.append(f"{label} (ID {channel_id}) not found – deleted, wrong ID or invisible to the bot "
                            f"(allow {perm_names(['view_channel'])} there).")
            continue
        have = channel.permissions_for(me)
        missing = [p for p in perms if not getattr(have, p)]
        if missing:
            problems.append(f"{label} #{channel.name}: The bot is missing {perm_names(missing)}.")

    roles = [("Auto role", cfg.auto_role_id), ("Giveaway winner role", cfg.giveaway_winner_role_id)]
    for label, role_id in roles:
        if not role_id:
            continue
        role = guild.get_role(role_id)
        if role is None:
            problems.append(f"{label} (ID {role_id}) not found.")
            continue
        if not me.guild_permissions.manage_roles:
            problems.append(f"{label} @{role.name}: The bot is missing {perm_names(['manage_roles'])}.")
        elif role >= me.top_role:
            problems.append(f"{label} @{role.name} is above the bot role @{me.top_role.name} – "
                            "drag the bot role above it in Server Settings → Roles.")

    mod = [p for p in ("kick_members", "ban_members", "moderate_members", "manage_messages")
           if not getattr(me.guild_permissions, p)]
    if mod:
        problems.append(f"For the /mod commands, the bot is missing on the server: {perm_names(mod)}.")
    return problems
