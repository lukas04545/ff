"""Loads and validates the configuration from the .env file.

All credentials (Discord token, RCON IP/port/password) come exclusively
from environment variables or the .env file – never from the code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Missing or invalid configuration."""


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _required(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"Required value '{name}' is missing from the .env file.")
    return value


def _int(name: str, default: int | None = None) -> int | None:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"'{name}' must be a number, but is '{raw}'.") from None


def _bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "ja", "on")


def _id_set(name: str) -> frozenset[int]:
    raw = _get(name)
    if not raw:
        return frozenset()
    try:
        return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)
    except ValueError:
        raise ConfigError(f"'{name}' must be a comma-separated list of IDs.") from None


def _commands(name: str, default: str) -> tuple[str, ...]:
    """Console command templates separated by ";" ("-" = none)."""
    return tuple(c.strip() for c in _get(name, default).split(";") if c.strip() and c.strip() != "-")


@dataclass(frozen=True)
class Config:
    # Discord
    discord_token: str
    guild_id: int | None

    # RCON (G-Portal)
    rcon_host: str
    rcon_port: int
    rcon_password: str

    # Permissions
    admin_role_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    allow_raw_rcon: bool

    # Channels (None = feature disabled)
    status_channel_id: int | None
    chat_channel_id: int | None
    killfeed_channel_id: int | None
    events_channel_id: int | None
    admin_log_channel_id: int | None
    console_log_channel_id: int | None

    # Behaviour
    status_interval: int
    playerlist_interval: int
    chat_bridge_to_game: bool
    chat_show_server_messages: bool
    show_respawns: bool
    say_prefix: str
    server_display_name: str

    # Command templates (assumption about the RCE syntax, see README)
    kick_template: str
    ban_template: str
    unban_template: str

    # Kit manager (assumption about the RCE syntax, see README)
    kit_give_template: str
    kit_give_group_template: str
    kit_give_all_template: str
    kit_add_template: str
    kit_remove_template: str
    item_give_template: str
    autokit_delay: int
    kit_claim_announce: bool

    # Support tickets with DeepSeek AI
    ticket_category_id: int | None
    ticket_log_channel_id: int | None
    support_role_ids: frozenset[int]
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    support_knowledge_file: Path
    ticket_ai_max_replies: int
    ticket_ai_kit_tools: bool   # AI may check/claim the ticket owner's kits

    # Community (welcome, auto role, Discord logs, rules, giveaways)
    welcome_channel_id: int | None
    welcome_message: str
    auto_role_id: int | None
    discord_log_channel_id: int | None
    rules_channel_id: int | None
    rules_file: Path
    rust_server_platform: str
    rust_server_wipe: str
    giveaway_winner_role_id: int | None
    giveaway_win_channel_id: int | None   # channel where another bot (Helios, Kryvon …) announces winners
    giveaway_auto_ticket: bool            # /giveaway ended -> prize ticket per winner, AI redeems the prize
    giveaway_prize_days: int              # kit prize without "lifetime"/duration -> role for N days (0 = staff)

    # Points & shop (economy)
    economy_enabled: bool
    points_name: str                      # e.g. "points", "coins", "scrap"
    points_per_hour: int                  # for linked players while online
    points_per_kill: int                  # PvP kill of a linked player
    daily_points: int                     # /daily
    daily_streak_bonus: int               # extra per streak day (max. 7 days)
    playtime_reward_hours: int            # every N hours online -> reward (0 = off)
    playtime_reward_kit: str              # custom kit for the reward (empty = none)
    playtime_reward_points: int           # points for the reward

    # Wipe manager
    wipe_timezone: str                    # for /wipe set (e.g. Europe/Berlin)
    wipe_channel_id: int | None           # announcements (empty = events channel)
    wipe_ping_role_id: int | None         # role pinged at "wipe in 1 hour" and "wiped"
    wipe_reset_kits: bool                 # reset autokit + custom kit cooldowns at the wipe
    wipe_reset_stats: bool                # reset kills/deaths/playtime at the wipe
    wipe_reset_points: bool               # reset point balances at the wipe

    # Reports
    report_channel_id: int | None         # /report + watchlist alerts (empty = alert channel / admin log)
    watchlist_channel_id: int | None      # watchlist alerts only (empty = report channel)

    # Admin abuse protection (raid items spawned by admins)
    abuse_protection: bool
    abuse_exempt_role_ids: frozenset[int]  # Discord roles (owner, co-owner) – their linked players may spawn
    abuse_exempt_players: frozenset[str]   # in-game names that may spawn (lowercase)
    abuse_items: tuple[str, ...]           # keywords of forbidden items (lowercase)
    abuse_remove_admin_commands: tuple[str, ...]   # templates with {name}
    abuse_before_kill_commands: tuple[str, ...]  # right before the kill, e.g. global.god 0 "{name}"
    abuse_kill_command: str                # template with {name}
    abuse_after_kill_commands: tuple[str, ...]   # run after the kill, e.g. entity.deleteby "{name}"
    abuse_on_respawn_commands: tuple[str, ...]   # when the punished player respawns, e.g. global.sleep "{name}"

    # Staff alerts & backups
    server_down_alert_minutes: int        # ping staff after the server is offline this long (0 = off)
    alert_channel_id: int | None          # empty = admin log channel
    alert_role_ids: frozenset[int]        # pinged on alerts (empty = admin roles)
    backup_enabled: bool
    backup_keep_days: int

    # Web interface
    web_enabled: bool
    web_host: str
    web_port: int
    web_password: str
    web_open_browser: bool
    web_tunnel: bool          # reachable from the internet via Cloudflare tunnel
    web_tunnel_token: str     # optional: named tunnel (fixed address)
    web_public_url: str       # optional: fixed address of the named tunnel

    # Automatic updates from GitHub (updater.py)
    auto_update: bool
    auto_update_interval: int   # minutes between checks
    auto_update_repo: str       # owner/name on GitHub
    auto_update_branch: str

    database_path: Path


def required_intents(cfg: Config) -> dict[str, list[str]]:
    """Privileged Discord intents -> features that need them (enabled features only)."""
    message_content, members = [], []
    if cfg.chat_channel_id and cfg.chat_bridge_to_game:
        message_content.append("Chat bridge")
    if cfg.ticket_category_id:
        message_content.append("Support tickets")
        if getattr(cfg, "giveaway_win_channel_id", None):
            message_content.append("Giveaway wins (ticket AI)")
    if cfg.discord_log_channel_id:
        message_content.append("Discord log")
        members.append("Discord log (joins/leaves)")
    if cfg.welcome_channel_id:
        members.append("Welcome message")
    if cfg.auto_role_id:
        members.append("Auto role")
    return {"message_content": message_content, "members": members}


def load_config(env_file: str | os.PathLike = ".env") -> Config:
    # override=True: changes from the web interface are picked up when reloading
    load_dotenv(env_file, override=True)

    port = _int("RCON_PORT")
    if port is None:
        raise ConfigError("Required value 'RCON_PORT' is missing from the .env file.")

    cfg = Config(
        discord_token=_required("DISCORD_TOKEN"),
        guild_id=_int("GUILD_ID"),
        rcon_host=_required("RCON_HOST"),
        rcon_port=port,
        rcon_password=_required("RCON_PASSWORD"),
        admin_role_ids=_id_set("ADMIN_ROLE_IDS"),
        admin_user_ids=_id_set("ADMIN_USER_IDS"),
        allow_raw_rcon=_bool("ALLOW_RAW_RCON", True),
        status_channel_id=_int("STATUS_CHANNEL_ID"),
        chat_channel_id=_int("CHAT_CHANNEL_ID"),
        killfeed_channel_id=_int("KILLFEED_CHANNEL_ID"),
        events_channel_id=_int("EVENTS_CHANNEL_ID"),
        admin_log_channel_id=_int("ADMIN_LOG_CHANNEL_ID"),
        console_log_channel_id=_int("CONSOLE_LOG_CHANNEL_ID"),
        status_interval=max(30, _int("STATUS_INTERVAL", 60)),
        playerlist_interval=max(10, _int("PLAYERLIST_INTERVAL", 30)),
        chat_bridge_to_game=_bool("CHAT_BRIDGE_TO_GAME", True),
        chat_show_server_messages=_bool("CHAT_SHOW_SERVER_MESSAGES", False),
        show_respawns=_bool("SHOW_RESPAWNS", False),
        say_prefix=_get("SAY_PREFIX", "<color=#5865F2>[Discord]</color>"),
        server_display_name=_get("SERVER_DISPLAY_NAME"),
        kick_template=_get("KICK_COMMAND_TEMPLATE", 'kick "{name}"'),
        ban_template=_get("BAN_COMMAND_TEMPLATE", 'banid "{name}"'),
        unban_template=_get("UNBAN_COMMAND_TEMPLATE", 'unbanid "{name}"'),
        kit_give_template=_get("KIT_GIVE_TEMPLATE", 'kit givetoplayer "{kit}" "{name}"'),
        kit_give_group_template=_get("KIT_GIVE_GROUP_TEMPLATE", 'kit givetogroup "{kit}" "{group}"'),
        kit_give_all_template=_get("KIT_GIVE_ALL_TEMPLATE", 'kit giveall "{kit}"'),
        kit_add_template=_get(
            "KIT_ADD_TEMPLATE", 'kit add "{kit}" "{item}" "{amount}" "{condition}" "{container}"'),
        kit_remove_template=_get("KIT_REMOVE_TEMPLATE", 'kit remove "{kit}" "{id}"'),
        item_give_template=_get("ITEM_GIVE_TEMPLATE", 'inventory.giveto "{name}" "{item}" {amount}'),
        autokit_delay=max(0, _int("AUTOKIT_DELAY", 3)),
        kit_claim_announce=_bool("KIT_CLAIM_ANNOUNCE", True),
        ticket_category_id=_int("TICKET_CATEGORY_ID"),
        ticket_log_channel_id=_int("TICKET_LOG_CHANNEL_ID"),
        # Without dedicated support roles, the admin roles handle support
        support_role_ids=_id_set("SUPPORT_ROLE_IDS") or _id_set("ADMIN_ROLE_IDS"),
        deepseek_api_key=_get("DEEPSEEK_API_KEY"),
        deepseek_model=_get("DEEPSEEK_MODEL", "deepseek-flash"),
        deepseek_base_url=_get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        support_knowledge_file=Path(_get("SUPPORT_KNOWLEDGE_FILE", "support_knowledge.md")),
        ticket_ai_max_replies=max(1, _int("TICKET_AI_MAX_REPLIES", 20)),
        ticket_ai_kit_tools=_bool("TICKET_AI_KIT_TOOLS", True),
        welcome_channel_id=_int("WELCOME_CHANNEL_ID"),
        welcome_message=_get("WELCOME_MESSAGE",
                             "Welcome {member} to **{server}**! Please read the rules{rules} – have fun!"),
        auto_role_id=_int("AUTO_ROLE_ID"),
        discord_log_channel_id=_int("DISCORD_LOG_CHANNEL_ID"),
        rules_channel_id=_int("RULES_CHANNEL_ID"),
        rules_file=Path(_get("RULES_FILE", "rules.md")),
        rust_server_platform=_get("RUST_SERVER_PLATFORM", "Rust Console Edition (Xbox & PlayStation)"),
        rust_server_wipe=_get("RUST_SERVER_WIPE", "See the announcements on Discord"),
        giveaway_winner_role_id=_int("GIVEAWAY_WINNER_ROLE_ID"),
        # Default: this server's Helios winner channel (change or clear it in the settings)
        giveaway_win_channel_id=_int("GIVEAWAY_WIN_CHANNEL_ID", 1558525305004757032),
        giveaway_auto_ticket=_bool("GIVEAWAY_AUTO_TICKET", True),
        giveaway_prize_days=max(0, _int("GIVEAWAY_PRIZE_DAYS", 0)),
        economy_enabled=_bool("ECONOMY_ENABLED", True),
        points_name=_get("POINTS_NAME", "points") or "points",
        points_per_hour=max(0, _int("POINTS_PER_HOUR", 10)),
        points_per_kill=max(0, _int("POINTS_PER_KILL", 5)),
        daily_points=max(0, _int("DAILY_POINTS", 50)),
        daily_streak_bonus=max(0, _int("DAILY_STREAK_BONUS", 10)),
        playtime_reward_hours=max(0, _int("PLAYTIME_REWARD_HOURS", 0)),
        playtime_reward_kit=_get("PLAYTIME_REWARD_KIT"),
        playtime_reward_points=max(0, _int("PLAYTIME_REWARD_POINTS", 0)),
        wipe_timezone=_get("WIPE_TIMEZONE", "Europe/Berlin") or "UTC",
        wipe_channel_id=_int("WIPE_ANNOUNCE_CHANNEL_ID"),
        wipe_ping_role_id=_int("WIPE_PING_ROLE_ID"),
        wipe_reset_kits=_bool("WIPE_RESET_KITS", True),
        wipe_reset_stats=_bool("WIPE_RESET_STATS", False),
        wipe_reset_points=_bool("WIPE_RESET_POINTS", False),
        report_channel_id=_int("REPORT_CHANNEL_ID"),
        watchlist_channel_id=_int("WATCHLIST_CHANNEL_ID"),
        abuse_protection=_bool("ABUSE_PROTECTION", True),
        abuse_exempt_role_ids=_id_set("ABUSE_EXEMPT_ROLE_IDS"),
        abuse_exempt_players=frozenset(p.strip().lower() for p in _get("ABUSE_EXEMPT_PLAYERS").split(",") if p.strip()),
        abuse_items=tuple(k.strip().lower() for k in (_get("ABUSE_ITEMS") or
                          "rocket,c4,timed explosive,explosive.timed").split(",") if k.strip()),
        abuse_remove_admin_commands=tuple(c.strip() for c in (_get("ABUSE_REMOVE_ADMIN_COMMANDS") or
                                          'removeowner "{name}";removemoderator "{name}"').split(";") if c.strip()),
        abuse_before_kill_commands=_commands("ABUSE_BEFORE_KILL_COMMANDS", 'global.god 0 "{name}"'),
        abuse_kill_command=_get("ABUSE_KILL_COMMAND", 'global.killplayer "{name}"'),
        abuse_after_kill_commands=_commands("ABUSE_AFTER_KILL_COMMANDS", 'entity.deleteby "{name}"'),
        abuse_on_respawn_commands=_commands("ABUSE_ON_RESPAWN_COMMANDS", 'global.sleep "{name}"'),
        server_down_alert_minutes=max(0, _int("SERVER_DOWN_ALERT_MINUTES", 10)),
        alert_channel_id=_int("ALERT_CHANNEL_ID"),
        alert_role_ids=_id_set("ALERT_ROLE_IDS") or _id_set("ADMIN_ROLE_IDS"),
        backup_enabled=_bool("BACKUP_ENABLED", True),
        backup_keep_days=max(1, _int("BACKUP_KEEP_DAYS", 7)),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=_get("WEB_HOST", "127.0.0.1"),
        web_port=_int("WEB_PORT", 8080),
        web_password=_get("WEB_PASSWORD"),
        web_open_browser=_bool("WEB_OPEN_BROWSER", True),
        web_tunnel=_bool("WEB_TUNNEL", True),
        web_tunnel_token=_get("WEB_TUNNEL_TOKEN"),
        web_public_url=_get("WEB_PUBLIC_URL"),
        auto_update=_bool("AUTO_UPDATE", True),
        auto_update_interval=max(1, _int("AUTO_UPDATE_INTERVAL", 5)),
        auto_update_repo=_get("AUTO_UPDATE_REPO", "lukas04545/ff"),
        auto_update_branch=_get("AUTO_UPDATE_BRANCH", "main"),
        database_path=Path(_get("DATABASE_PATH", "data/bot.db")),
    )

    if not cfg.admin_role_ids and not cfg.admin_user_ids:
        raise ConfigError(
            "Neither ADMIN_ROLE_IDS nor ADMIN_USER_IDS is set – without them nobody "
            "(or everybody) could use admin commands. Please set at least one."
        )
    for env_name, value in (
        ("KICK_COMMAND_TEMPLATE", cfg.kick_template),
        ("BAN_COMMAND_TEMPLATE", cfg.ban_template),
        ("UNBAN_COMMAND_TEMPLATE", cfg.unban_template),
    ):
        if "{name}" not in value:
            raise ConfigError(f"The template {env_name} must contain the placeholder {{name}}.")
    # Kit templates: required placeholders present, no unknown placeholders
    dummy = {"kit": "k", "name": "n", "group": "g", "item": "i", "amount": 1,
             "condition": 100, "container": "Main", "id": 1}
    for env_name, value, required in (
        ("KIT_GIVE_TEMPLATE", cfg.kit_give_template, ("{kit}", "{name}")),
        ("KIT_GIVE_GROUP_TEMPLATE", cfg.kit_give_group_template, ("{kit}", "{group}")),
        ("KIT_GIVE_ALL_TEMPLATE", cfg.kit_give_all_template, ("{kit}",)),
        ("KIT_ADD_TEMPLATE", cfg.kit_add_template, ("{kit}", "{item}")),
        ("KIT_REMOVE_TEMPLATE", cfg.kit_remove_template, ("{kit}", "{id}")),
        ("ITEM_GIVE_TEMPLATE", cfg.item_give_template, ("{name}", "{item}", "{amount}")),
    ):
        missing = [p for p in required if p not in value]
        if missing:
            raise ConfigError(f"The template {env_name} needs the placeholders {', '.join(missing)}.")
        try:
            value.format(**dummy)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(f"The template {env_name} contains an unknown placeholder: {exc}") from None
    return cfg
