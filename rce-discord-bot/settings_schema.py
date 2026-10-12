"""Description of every setting for the settings page in the web interface.

Values are still stored in .env (see envfile.py) – the web interface is just a more
convenient front end for it. Defaults live in config.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Field kinds: text, secret, int, bool, textarea, id (one Discord ID), ids (several IDs)
# pick: choose from Discord when the bot is connected: guild, text_channel, category, role


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str = "text"
    help: str = ""
    required: bool = False
    pick: str | None = None
    advanced: bool = False
    placeholder: str = ""
    choices: tuple[str, ...] = field(default_factory=tuple)


GROUPS = [
    ("discord", "🤖 Discord"),
    ("rcon", "🛢️ Rust server (RCON)"),
    ("rights", "🛡️ Permissions"),
    ("channels", "📺 Channels"),
    ("behaviour", "⚙️ Behaviour"),
    ("kits", "🎒 Kits"),
    ("tickets", "🎫 Tickets & AI"),
    ("community", "🎉 Community"),
    ("economy", "🪙 Points & shop"),
    ("wipe", "🧹 Wipe"),
    ("alerts", "🚨 Alerts & backups"),
    ("abuse", "🛡️ Admin abuse protection"),
    ("web", "🌐 Web interface"),
    ("updates", "🔄 Updates"),
    ("templates", "🧩 Command templates (experts)"),
]

FIELDS: list[Field] = [
    # Discord
    Field("DISCORD_TOKEN", "Bot token", "discord", "secret", required=True,
          help="Developer Portal → your application → Bot → Reset Token. Never share it!"),
    Field("GUILD_ID", "Discord server", "discord", "id", pick="guild",
          help="With a server set, slash commands show up instantly."),
    # RCON
    Field("RCON_HOST", "Server IP", "rcon", required=True, placeholder="e.g. 147.93.162.77",
          help="Just the IP, no port. G-Portal panel → Status/Network."),
    Field("RCON_PORT", "RCON port", "rcon", "int", required=True,
          help="The RCON port from the G-Portal panel – NOT the game port."),
    Field("RCON_PASSWORD", "RCON password", "rcon", "secret", required=True,
          help="G-Portal → Settings → Basic settings. Letters and numbers only; restart the server afterwards."),
    # Permissions
    Field("ADMIN_ROLE_IDS", "Admin roles", "rights", "ids", pick="role",
          help="These roles may use server commands (RCON, kits, in-game kick/ban)."),
    Field("ADMIN_USER_IDS", "Admin users (IDs)", "rights", "ids",
          help="Additional individual users by ID (right-click a user → Copy User ID)."),
    Field("ALLOW_RAW_RCON", "Allow arbitrary RCON commands (/rcon, console)", "rights", "bool"),
    # Channels
    Field("STATUS_CHANNEL_ID", "Status channel", "channels", "id", pick="text_channel",
          help="Live server status message."),
    Field("CHAT_CHANNEL_ID", "Chat bridge", "channels", "id", pick="text_channel",
          help="Quick chat in-game ↔ Discord."),
    Field("KILLFEED_CHANNEL_ID", "Killfeed", "channels", "id", pick="text_channel"),
    Field("EVENTS_CHANNEL_ID", "Events (joins, heli, cargo …)", "channels", "id", pick="text_channel"),
    Field("ADMIN_LOG_CHANNEL_ID", "Admin log", "channels", "id", pick="text_channel",
          help="Bans, kits, bot actions. Keep it private!"),
    Field("CONSOLE_LOG_CHANNEL_ID", "Server console", "channels", "id", pick="text_channel",
          help="Lots of messages – use a separate private channel."),
    # Behaviour
    Field("SERVER_DISPLAY_NAME", "Server display name", "behaviour",
          help="Empty = use the name from the server."),
    Field("STATUS_INTERVAL", "Update status every … seconds", "behaviour", "int", placeholder="60"),
    Field("PLAYERLIST_INTERVAL", "Poll player list every … seconds", "behaviour", "int", placeholder="30"),
    Field("CHAT_BRIDGE_TO_GAME", "Send Discord messages into the game", "behaviour", "bool"),
    Field("CHAT_SHOW_SERVER_MESSAGES", "Show [CHAT SERVER] lines in Discord", "behaviour", "bool"),
    Field("SHOW_RESPAWNS", "Show respawns in the events channel", "behaviour", "bool"),
    Field("SAY_PREFIX", "Prefix for Discord messages in-game", "behaviour",
          placeholder="<color=#5865F2>[Discord]</color>"),
    # Kits
    Field("AUTOKIT_DELAY", "Autokit delay after respawn (seconds)", "kits", "int", placeholder="3"),
    Field("KIT_CLAIM_ANNOUNCE", "Confirm quick chat kits in-game", "kits", "bool"),
    Field("LINK_VERIFY_QUICKCHAT", "Linking: also verify with quick chat in-game", "kits", "bool",
          help="Off (default): members pick their online player from a dropdown in Discord and are linked right "
               "away – nothing proves the player is theirs. On: after picking, they must send two quick chat "
               "phrases in-game (emote wheel) to prove it."),
    # Tickets
    Field("TICKET_CATEGORY_ID", "Ticket category", "tickets", "id", pick="category",
          help="Empty = tickets off."),
    Field("TICKET_LOG_CHANNEL_ID", "Ticket log (transcripts)", "tickets", "id", pick="text_channel"),
    Field("SUPPORT_ROLE_IDS", "Support roles", "tickets", "ids", pick="role",
          help="Can see tickets and get pinged. Empty = admin roles."),
    Field("DEEPSEEK_API_KEY", "DeepSeek API key", "tickets", "secret",
          help="platform.deepseek.com → API Keys. Empty = tickets without AI."),
    Field("DEEPSEEK_MODEL", "DeepSeek model", "tickets", placeholder="deepseek-flash"),
    Field("TICKET_AI_MAX_REPLIES", "Max. AI replies per ticket", "tickets", "int", placeholder="20"),
    Field("TICKET_RESOLVE_MINUTES", "Ask \"Is it resolved?\" after (minutes)", "tickets", "int", placeholder="5",
          help="After the AI answered and the player hasn't written for this long, the AI asks if the issue is "
               "resolved (Yes/No buttons; typing \"yes\" works too). Yes = the ticket is closed. Not asked when "
               "staff took over, staff was called or a kit purchase waits for approval. 0 = off."),
    Field("TICKET_AI_KIT_TOOLS", "AI may check and claim kits for the ticket owner", "tickets", "bool",
          help="On (default): the AI can look up which custom kits the ticket owner's Discord roles allow, "
               "when they last claimed them, and claim one for them – same rules as /customkit claim "
               "(role, cooldown, linked player). Never for anyone else."),
    Field("KIT_APPROVER_ROLE_IDS", "Roles that approve kit purchases", "tickets", "ids", pick="role",
          help="When a player says they bought a kit, the approve request in the ticket pings these roles and only "
               "they (and bot admins) can press Approve / Reject. They also get access to that ticket. "
               "Empty = support roles."),
    Field("DEEPSEEK_BASE_URL", "DeepSeek API URL", "tickets", advanced=True,
          placeholder="https://api.deepseek.com"),
    Field("SUPPORT_KNOWLEDGE_FILE", "Knowledge base file", "tickets", advanced=True,
          placeholder="support_knowledge.md"),
    # Community
    Field("WELCOME_CHANNEL_ID", "Welcome channel", "community", "id", pick="text_channel"),
    Field("WELCOME_MESSAGE", "Welcome message", "community", "textarea",
          help="Placeholders: {member} {server} {count} {rules}"),
    Field("AUTO_ROLE_ID", "Auto role for new members", "community", "id", pick="role"),
    Field("DISCORD_LOG_CHANNEL_ID", "Discord log (joins, messages, moderation)", "community", "id",
          pick="text_channel"),
    Field("RULES_CHANNEL_ID", "Rules channel", "community", "id", pick="text_channel"),
    Field("GIVEAWAY_WINNER_ROLE_ID", "Role for giveaway winners", "community", "id", pick="role"),
    Field("GIVEAWAY_WIN_CHANNEL_ID", "Channel with giveaway winners (Helios, Kryvon …)", "tickets", "id",
          pick="text_channel",
          help="The ticket AI looks up a player's wins here. Lifetime kit prizes: it gives the kit's role "
               "(once per win). Other prizes go to staff. 0 = off."),
    Field("GIVEAWAY_AUTO_TICKET", "Prize ticket for /giveaway winners", "tickets", "bool",
          help="On (default): when a /giveaway ends, every winner automatically gets a ticket in which the AI "
               "redeems the prize (lifetime kit -> kit role) or calls staff. Needs tickets to be set up."),
    Field("GIVEAWAY_PRIZE_DAYS", "Days for kit prizes without a duration", "tickets", "int", placeholder="0",
          help="A kit prize that is neither lifetime nor names a duration (e.g. \"Vip Standard\"): the AI gives the "
               "kit role for this many days. 0 = staff decides. Prizes like \"VIP 7 days\" always use their own "
               "duration."),
    Field("RUST_SERVER_PLATFORM", "Platform (for /rust)", "community",
          placeholder="Rust Console Edition (Xbox & PlayStation)"),
    Field("RUST_SERVER_WIPE", "Wipe schedule (for /rust)", "community", placeholder="e.g. Thursdays 6 pm"),
    Field("RULES_FILE", "Rules file", "community", advanced=True, placeholder="rules.md"),
    # Points & shop
    Field("ECONOMY_ENABLED", "Points & shop", "economy", "bool",
          help="On (default): members with a linked player (/customkit link) earn points for playtime, kills and "
               "/daily and spend them in /shop. Shop items are set up in the web interface → 🪙 Shop."),
    Field("POINTS_NAME", "Name of the points", "economy", placeholder="points",
          help="Shown everywhere, e.g. coins, scrap, RP."),
    Field("POINTS_PER_HOUR", "Points per hour online", "economy", "int", placeholder="10"),
    Field("POINTS_PER_KILL", "Points per PvP kill", "economy", "int", placeholder="5",
          help="The same killer/victim pair counts at most once every 10 minutes (no farming)."),
    Field("DAILY_POINTS", "Points for /daily", "economy", "int", placeholder="50"),
    Field("DAILY_STREAK_BONUS", "Extra points per day in a row", "economy", "int", placeholder="10",
          help="/daily on consecutive days (max. 48 h apart) adds this per day, up to 7 days."),
    Field("PLAYTIME_REWARD_HOURS", "Playtime reward every … hours", "economy", "int", placeholder="0",
          help="Every N hours online (linked players) the player gets the kit and/or points below. 0 = off."),
    Field("PLAYTIME_REWARD_KIT", "Playtime reward: custom kit", "economy", placeholder="e.g. Starter",
          help="Name of a custom kit, sent in-game. Empty = no kit."),
    Field("PLAYTIME_REWARD_POINTS", "Playtime reward: points", "economy", "int", placeholder="0"),
    # Wipe
    Field("WIPE_TIMEZONE", "Time zone of the wipe time", "wipe", placeholder="Europe/Berlin",
          help="Time zone for /wipe set and the web interface, e.g. Europe/Berlin, Europe/London, America/New_York."),
    Field("WIPE_ANNOUNCE_CHANNEL_ID", "Wipe announcement channel", "wipe", "id", pick="text_channel",
          help="Countdown posts 24 h and 1 h before and \"Server wiped!\". Empty = events channel."),
    Field("WIPE_PING_ROLE_ID", "Role to ping for the wipe", "wipe", "id", pick="role",
          help="Pinged 1 h before and at the wipe. Empty = no ping."),
    Field("WIPE_RESET_KITS", "Reset kit cooldowns at the wipe", "wipe", "bool",
          help="On (default): autokit claims and custom kit cooldowns start fresh."),
    Field("WIPE_RESET_STATS", "Reset leaderboard at the wipe", "wipe", "bool",
          help="Kills, deaths and playtime back to 0 (a backup is made first). Default off."),
    Field("WIPE_RESET_POINTS", "Reset points at the wipe", "wipe", "bool", help="Default off."),
    Field("REPORT_CHANNEL_ID", "Channel for /report and watchlist alerts", "alerts", "id", pick="text_channel",
          help="Empty = alert channel (or admin log). Support roles are pinged."),
    Field("WATCHLIST_CHANNEL_ID", "Channel for watchlist alerts", "alerts", "id", pick="text_channel",
          help="Where \"Watched player joined\" is posted. Empty = the report channel above."),
    # Admin abuse protection
    Field("ABUSE_PROTECTION", "Punish admins who spawn raid items", "abuse", "bool",
          help="On (default): when someone who is NOT allowed spawns rockets or C4 with the in-game admin panel, the "
               "bot removes their admin and "
               "kills them in-game, and pings staff. Only active once you set who IS allowed (the next two fields) – "
               "so the owner can never be hit by accident."),
    Field("ABUSE_EXEMPT_ROLE_IDS", "Allowed Discord roles (owner, co-owner)", "abuse", "ids", pick="role",
          help="Members with one of these roles may spawn – the bot knows their in-game name from /customkit link."),
    Field("ABUSE_EXEMPT_PLAYERS", "Allowed in-game names", "abuse", placeholder="e.g. MyGamertag, CoOwnerPSN",
          help="Comma-separated, upper/lower case doesn't matter. Works without linking – put the owner's name here."),
    Field("ABUSE_ITEMS", "Forbidden items", "abuse", advanced=True, placeholder="rocket,c4,timed explosive,explosive.timed",
          help="Comma-separated words matched in the spawned item's name (rocket launchers don't count)."),
    Field("ABUSE_REMOVE_ADMIN_COMMANDS", "Commands to remove admin", "abuse", advanced=True,
          placeholder='removeowner "{name}";removemoderator "{name}"',
          help="Separated by ; – {name} = player. Test them first with /rcon on a test account; the server's reply is "
               "shown in the alert."),
    Field("ABUSE_BEFORE_KILL_COMMANDS", "Commands before the kill", "abuse", advanced=True,
          placeholder='global.god 0 "{name}"',
          help="Run right before the kill (default: switch god mode off so the kill works). Separated by ; – "
               "{name} = player, - = nothing."),
    Field("ABUSE_KILL_COMMAND", "Kill command", "abuse", advanced=True, placeholder='global.killplayer "{name}"'),
    Field("ABUSE_AFTER_KILL_COMMANDS", "Commands right after the kill", "abuse", advanced=True, placeholder="-",
          help="Run automatically right after the kill, separated by ; – {name} = player. Default: nothing."),
    Field("ABUSE_APPROVAL_COMMANDS", "Commands only after staff approval", "abuse", advanced=True,
          placeholder='entity.deleteby "{name}"',
          help="NOT run automatically: staff approves or rejects them with the buttons on the Discord alert or here in "
               "the web interface. Default entity.deleteby deletes everything the player owns (bases, deployables). "
               "Separated by ; – {name} = player, - = nothing."),
    Field("ABUSE_ON_RESPAWN_COMMANDS", "Commands when they respawn", "abuse", advanced=True,
          placeholder='global.sleep "{name}"',
          help="Run once the punished player respawns (\"has entered the game\" in the log, within 1 hour). "
               "Separated by ; – {name} = player, - = nothing."),
    # Alerts & backups
    Field("SERVER_DOWN_ALERT_MINUTES", "Server-down alert after … minutes", "alerts", "int", placeholder="10",
          help="Pings staff once when there has been no RCON connection for this long, and again when the server is "
               "back. 0 = off."),
    Field("ALERT_CHANNEL_ID", "Alert channel", "alerts", "id", pick="text_channel",
          help="Empty = admin log channel."),
    Field("ALERT_ROLE_IDS", "Roles to ping on alerts", "alerts", "ids", pick="role",
          help="Empty = admin roles."),
    Field("BACKUP_ENABLED", "Daily backup", "alerts", "bool",
          help="On (default): once a day and before every automatic update the bot saves the database, web "
               "logins, rules and knowledge base to data/backups/ (your .env with the tokens is not included)."),
    Field("BACKUP_KEEP_DAYS", "Keep backups for … days", "alerts", "int", placeholder="7"),
    # Web
    Field("WEB_PASSWORD", "Password for this web interface", "web", "secret",
          help="Empty = a login link in the bot window on every start."),
    Field("WEB_HOST", "Reachable from", "web", choices=("127.0.0.1", "0.0.0.0"),
          help="127.0.0.1 = this PC only, 0.0.0.0 = also phone/laptop on your home network. "
               "Never expose it to the internet!"),
    Field("WEB_PORT", "Port", "web", "int", placeholder="8080"),
    Field("WEB_TUNNEL", "Reachable from the internet (Cloudflare tunnel)", "web", "bool",
          help="On (default) = free, encrypted https:// address that works from anywhere – no router settings "
               "needed. The link is shown in the bot window, in PUBLIC_LINK.txt, on the Overview and in the admin "
               "log channel. Without a WEB_PASSWORD the bot generates one and saves it."),
    Field("WEB_TUNNEL_TOKEN", "Cloudflare tunnel token (fixed address)", "web", "secret", advanced=True,
          help="Optional. Empty = random address (changes when the bot window is closed). For a fixed address "
               "create a tunnel in the Cloudflare dashboard (needs your own domain) and paste its token."),
    Field("WEB_PUBLIC_URL", "Fixed public address", "web", advanced=True, placeholder="https://panel.example.com",
          help="Only with a tunnel token: the address you configured for it in Cloudflare."),
    Field("WEB_OPEN_BROWSER", "Open browser on start", "web", "bool"),
    Field("WEB_ENABLED", "Web interface enabled", "web", "bool",
          help="Careful: turning this off locks you out of the web interface (then only via .env)."),
    # Updates
    Field("AUTO_UPDATE", "Update automatically", "updates", "bool",
          help="On (default): the bot checks GitHub regularly, installs a new version by itself and restarts "
               "(about 10 seconds offline). Your .env, data/, logs/ and edited rules/knowledge base are never "
               "touched."),
    Field("AUTO_UPDATE_INTERVAL", "Check every … minutes", "updates", "int", placeholder="5"),
    Field("AUTO_UPDATE_REPO", "GitHub repository", "updates", advanced=True, placeholder="lukas04545/ff",
          help="owner/name of the public repository the bot updates from."),
    Field("AUTO_UPDATE_BRANCH", "Branch", "updates", advanced=True, placeholder="main"),
    # Templates
    Field("KICK_COMMAND_TEMPLATE", "Kick command", "templates", placeholder='kick "{name}"'),
    Field("BAN_COMMAND_TEMPLATE", "Ban command", "templates", placeholder='banid "{name}"'),
    Field("UNBAN_COMMAND_TEMPLATE", "Unban command", "templates", placeholder='unbanid "{name}"'),
    Field("KIT_GIVE_TEMPLATE", "Kit to player", "templates", placeholder='kit givetoplayer "{kit}" "{name}"'),
    Field("KIT_GIVE_GROUP_TEMPLATE", "Kit to group", "templates", placeholder='kit givetogroup "{kit}" "{group}"'),
    Field("KIT_GIVE_ALL_TEMPLATE", "Kit to everyone", "templates", placeholder='kit giveall "{kit}"'),
    Field("KIT_ADD_TEMPLATE", "Add item to kit", "templates",
          placeholder='kit add "{kit}" "{item}" "{amount}" "{condition}" "{container}"'),
    Field("KIT_REMOVE_TEMPLATE", "Remove item from kit", "templates", placeholder='kit remove "{kit}" "{id}"'),
    Field("ITEM_GIVE_TEMPLATE", "Item to player (custom kits)", "templates",
          placeholder='inventory.giveto "{name}" "{item}" {amount}'),
    Field("DATABASE_PATH", "Database file", "templates", placeholder="data/bot.db"),
]

FIELDS_BY_KEY = {f.key: f for f in FIELDS}
SECRET_KEYS = {f.key for f in FIELDS if f.kind == "secret"}


def validate(key: str, value: str) -> str | None:
    """Error message for a value, or None if it is valid."""
    f = FIELDS_BY_KEY.get(key)
    if f is None:
        return "Unknown setting."
    value = value.strip()
    if not value:
        return f'"{f.label}" is required.' if f.required else None
    if f.kind in ("int",) and not value.isdigit():
        return f'"{f.label}" must be a number.'
    if f.kind == "id" and not value.isdigit():
        return f'"{f.label}" must be a Discord ID (digits only).'
    if f.kind == "ids" and not all(p.strip().isdigit() for p in value.split(",") if p.strip()):
        return f'"{f.label}" must be a comma-separated list of IDs.'
    if f.kind == "bool" and value.lower() not in ("true", "false"):
        return f'"{f.label}" must be true or false.'
    if f.choices and value not in f.choices:
        return f'"{f.label}": allowed values are {", ".join(f.choices)}.'
    if key == "RCON_HOST" and (":" in value or "/" in value):
        return 'Enter the server IP without a port and without "/".'
    if key == "AUTO_UPDATE_REPO" and not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        return f'"{f.label}" must look like owner/repository.'
    if key == "AUTO_UPDATE_BRANCH" and not re.fullmatch(r"[\w./-]+", value):
        return f'"{f.label}" is not a valid branch name.'
    if key == "WEB_PUBLIC_URL" and not value.startswith(("https://", "http://")):
        return f'"{f.label}" must start with https://'
    if "\n" in value and f.kind != "textarea":
        return f'"{f.label}" must not contain line breaks.'
    return None
