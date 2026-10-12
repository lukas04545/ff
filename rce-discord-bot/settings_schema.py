"""Description of every setting for the settings page in the web interface.

Values are still stored in .env (see envfile.py) – the web interface is just a more
convenient front end for it. Defaults live in config.py.
"""
from __future__ import annotations

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
    ("web", "🌐 Web interface"),
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
    Field("RUST_SERVER_PLATFORM", "Platform (for /rust)", "community",
          placeholder="Rust Console Edition (Xbox & PlayStation)"),
    Field("RUST_SERVER_WIPE", "Wipe schedule (for /rust)", "community", placeholder="e.g. Thursdays 6 pm"),
    Field("RULES_FILE", "Rules file", "community", advanced=True, placeholder="rules.md"),
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
    if key == "WEB_PUBLIC_URL" and not value.startswith(("https://", "http://")):
        return f'"{f.label}" must start with https://'
    if "\n" in value and f.kind != "textarea":
        return f'"{f.label}" must not contain line breaks.'
    return None
