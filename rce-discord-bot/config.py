"""Lädt und prüft die Konfiguration aus der .env-Datei.

Alle Zugangsdaten (Discord-Token, RCON-IP/Port/Passwort) kommen ausschließlich
aus Umgebungsvariablen bzw. der .env-Datei – niemals aus dem Code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Fehlende oder ungültige Konfiguration."""


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _required(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"Pflichtwert '{name}' fehlt in der .env-Datei.")
    return value


def _int(name: str, default: int | None = None) -> int | None:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"'{name}' muss eine Zahl sein, ist aber '{raw}'.") from None


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
        raise ConfigError(f"'{name}' muss eine kommagetrennte Liste von IDs sein.") from None


@dataclass(frozen=True)
class Config:
    # Discord
    discord_token: str
    guild_id: int | None

    # RCON (G-Portal)
    rcon_host: str
    rcon_port: int
    rcon_password: str

    # Berechtigungen
    admin_role_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    allow_raw_rcon: bool

    # Channels (None = Feature deaktiviert)
    status_channel_id: int | None
    chat_channel_id: int | None
    killfeed_channel_id: int | None
    events_channel_id: int | None
    admin_log_channel_id: int | None
    console_log_channel_id: int | None

    # Verhalten
    status_interval: int
    playerlist_interval: int
    chat_bridge_to_game: bool
    chat_show_server_messages: bool
    show_respawns: bool
    say_prefix: str
    server_display_name: str

    # Befehls-Vorlagen (Annahme über die RCE-Syntax, siehe README)
    kick_template: str
    ban_template: str
    unban_template: str

    # Kitmanager (Annahme über die RCE-Syntax, siehe README)
    kit_give_template: str
    kit_give_group_template: str
    kit_give_all_template: str
    kit_add_template: str
    kit_remove_template: str
    item_give_template: str
    autokit_delay: int
    kit_claim_announce: bool

    # Support-Tickets mit DeepSeek-KI
    ticket_category_id: int | None
    ticket_log_channel_id: int | None
    support_role_ids: frozenset[int]
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    support_knowledge_file: Path
    ticket_ai_max_replies: int

    # Webinterface
    web_enabled: bool
    web_host: str
    web_port: int
    web_password: str
    web_open_browser: bool

    database_path: Path


def load_config(env_file: str | os.PathLike = ".env") -> Config:
    load_dotenv(env_file)

    port = _int("RCON_PORT")
    if port is None:
        raise ConfigError("Pflichtwert 'RCON_PORT' fehlt in der .env-Datei.")

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
        # Ohne eigene Support-Rollen übernehmen die Admin-Rollen den Support
        support_role_ids=_id_set("SUPPORT_ROLE_IDS") or _id_set("ADMIN_ROLE_IDS"),
        deepseek_api_key=_get("DEEPSEEK_API_KEY"),
        deepseek_model=_get("DEEPSEEK_MODEL", "deepseek-flash"),
        deepseek_base_url=_get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        support_knowledge_file=Path(_get("SUPPORT_KNOWLEDGE_FILE", "support_knowledge.md")),
        ticket_ai_max_replies=max(1, _int("TICKET_AI_MAX_REPLIES", 20)),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=_get("WEB_HOST", "127.0.0.1"),
        web_port=_int("WEB_PORT", 8080),
        web_password=_get("WEB_PASSWORD"),
        web_open_browser=_bool("WEB_OPEN_BROWSER", True),
        database_path=Path(_get("DATABASE_PATH", "data/bot.db")),
    )

    if not cfg.admin_role_ids and not cfg.admin_user_ids:
        raise ConfigError(
            "Weder ADMIN_ROLE_IDS noch ADMIN_USER_IDS gesetzt – ohne diese Angabe "
            "könnte niemand (oder jeder) Admin-Befehle nutzen. Bitte mindestens eins eintragen."
        )
    for env_name, value in (
        ("KICK_COMMAND_TEMPLATE", cfg.kick_template),
        ("BAN_COMMAND_TEMPLATE", cfg.ban_template),
        ("UNBAN_COMMAND_TEMPLATE", cfg.unban_template),
    ):
        if "{name}" not in value:
            raise ConfigError(f"Die Vorlage {env_name} muss den Platzhalter {{name}} enthalten.")
    # Kit-Vorlagen: Pflicht-Platzhalter vorhanden, keine unbekannten Platzhalter
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
            raise ConfigError(f"Die Vorlage {env_name} braucht die Platzhalter {', '.join(missing)}.")
        try:
            value.format(**dummy)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(f"Die Vorlage {env_name} enthält einen unbekannten Platzhalter: {exc}") from None
    return cfg
