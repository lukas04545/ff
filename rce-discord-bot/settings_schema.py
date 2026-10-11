"""Beschreibung aller Einstellungen für die Einstellungsseite im Webinterface.

Gespeichert wird weiterhin in der .env (siehe envfile.py) – das Webinterface ist nur
eine bequemere Oberfläche dafür. Standardwerte stehen in config.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Feldarten: text, secret, int, bool, textarea, id (eine Discord-ID), ids (mehrere IDs)
# pick: Auswahl aus Discord, wenn der Bot verbunden ist: guild, text_channel, category, role


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
    ("rcon", "🛢️ Rust-Server (RCON)"),
    ("rights", "🛡️ Berechtigungen"),
    ("channels", "📺 Channels"),
    ("behaviour", "⚙️ Verhalten"),
    ("kits", "🎒 Kits"),
    ("tickets", "🎫 Tickets & KI"),
    ("community", "🎉 Community"),
    ("web", "🌐 Webinterface"),
    ("templates", "🧩 Befehls-Vorlagen (Experten)"),
]

FIELDS: list[Field] = [
    # Discord
    Field("DISCORD_TOKEN", "Bot-Token", "discord", "secret", required=True,
          help="Developer Portal → deine Anwendung → Bot → Reset Token. Niemals weitergeben!"),
    Field("GUILD_ID", "Discord-Server", "discord", "id", pick="guild",
          help="Mit gesetztem Server erscheinen die Slash-Commands sofort."),
    # RCON
    Field("RCON_HOST", "Server-IP", "rcon", required=True, placeholder="z. B. 147.93.162.77",
          help="Nur die IP, ohne Port. G-Portal-Panel → Status/Netzwerk."),
    Field("RCON_PORT", "RCON-Port", "rcon", "int", required=True,
          help="Der RCON-Port aus dem G-Portal-Panel – NICHT der Spielport."),
    Field("RCON_PASSWORD", "RCON-Passwort", "rcon", "secret", required=True,
          help="G-Portal → Einstellungen → Grundeinstellungen. Nur Buchstaben und Zahlen; danach Server neu starten."),
    # Rechte
    Field("ADMIN_ROLE_IDS", "Admin-Rollen", "rights", "ids", pick="role",
          help="Diese Rollen dürfen Server-Befehle (RCON, Kits, Kick/Ban ingame) nutzen."),
    Field("ADMIN_USER_IDS", "Admin-Nutzer (IDs)", "rights", "ids",
          help="Zusätzlich einzelne Nutzer per ID (Rechtsklick auf Nutzer → ID kopieren)."),
    Field("ALLOW_RAW_RCON", "Beliebige RCON-Befehle erlauben (/rcon, Konsole)", "rights", "bool"),
    # Channels
    Field("STATUS_CHANNEL_ID", "Status-Channel", "channels", "id", pick="text_channel",
          help="Live-Status-Nachricht des Servers."),
    Field("CHAT_CHANNEL_ID", "Chat-Bridge", "channels", "id", pick="text_channel",
          help="Quick-Chat ingame ↔ Discord."),
    Field("KILLFEED_CHANNEL_ID", "Killfeed", "channels", "id", pick="text_channel"),
    Field("EVENTS_CHANNEL_ID", "Events (Joins, Heli, Cargo …)", "channels", "id", pick="text_channel"),
    Field("ADMIN_LOG_CHANNEL_ID", "Admin-Log", "channels", "id", pick="text_channel",
          help="Bans, Kits, Bot-Aktionen. Privat halten!"),
    Field("CONSOLE_LOG_CHANNEL_ID", "Server-Konsole", "channels", "id", pick="text_channel",
          help="Sehr viele Nachrichten – eigener privater Channel."),
    # Verhalten
    Field("SERVER_DISPLAY_NAME", "Anzeigename des Servers", "behaviour",
          help="Leer = Name vom Server übernehmen."),
    Field("STATUS_INTERVAL", "Status aktualisieren alle … Sekunden", "behaviour", "int", placeholder="60"),
    Field("PLAYERLIST_INTERVAL", "Spielerliste abfragen alle … Sekunden", "behaviour", "int", placeholder="30"),
    Field("CHAT_BRIDGE_TO_GAME", "Discord-Nachrichten ins Spiel senden", "behaviour", "bool"),
    Field("CHAT_SHOW_SERVER_MESSAGES", "[CHAT SERVER]-Zeilen in Discord zeigen", "behaviour", "bool"),
    Field("SHOW_RESPAWNS", "Respawns im Events-Channel zeigen", "behaviour", "bool"),
    Field("SAY_PREFIX", "Präfix für Discord-Nachrichten im Spiel", "behaviour",
          placeholder="<color=#5865F2>[Discord]</color>"),
    # Kits
    Field("AUTOKIT_DELAY", "Autokit-Verzögerung nach Respawn (Sekunden)", "kits", "int", placeholder="3"),
    Field("KIT_CLAIM_ANNOUNCE", "Quick-Chat-Kits ingame bestätigen", "kits", "bool"),
    # Tickets
    Field("TICKET_CATEGORY_ID", "Ticket-Kategorie", "tickets", "id", pick="category",
          help="Leer = Tickets aus."),
    Field("TICKET_LOG_CHANNEL_ID", "Ticket-Log (Transkripte)", "tickets", "id", pick="text_channel"),
    Field("SUPPORT_ROLE_IDS", "Support-Rollen", "tickets", "ids", pick="role",
          help="Sehen Tickets und werden gepingt. Leer = Admin-Rollen."),
    Field("DEEPSEEK_API_KEY", "DeepSeek-API-Key", "tickets", "secret",
          help="platform.deepseek.com → API Keys. Leer = Tickets ohne KI."),
    Field("DEEPSEEK_MODEL", "DeepSeek-Modell", "tickets", placeholder="deepseek-flash"),
    Field("TICKET_AI_MAX_REPLIES", "Max. KI-Antworten pro Ticket", "tickets", "int", placeholder="20"),
    Field("DEEPSEEK_BASE_URL", "DeepSeek-API-Adresse", "tickets", advanced=True,
          placeholder="https://api.deepseek.com"),
    Field("SUPPORT_KNOWLEDGE_FILE", "Datei der Wissensbasis", "tickets", advanced=True,
          placeholder="support_knowledge.md"),
    # Community
    Field("WELCOME_CHANNEL_ID", "Willkommens-Channel", "community", "id", pick="text_channel"),
    Field("WELCOME_MESSAGE", "Willkommensnachricht", "community", "textarea",
          help="Platzhalter: {member} {server} {count} {rules}"),
    Field("AUTO_ROLE_ID", "Auto-Rolle für neue Mitglieder", "community", "id", pick="role"),
    Field("DISCORD_LOG_CHANNEL_ID", "Discord-Log (Joins, Nachrichten, Moderation)", "community", "id",
          pick="text_channel"),
    Field("RULES_CHANNEL_ID", "Regel-Channel", "community", "id", pick="text_channel"),
    Field("GIVEAWAY_WINNER_ROLE_ID", "Rolle für Giveaway-Gewinner", "community", "id", pick="role"),
    Field("RUST_SERVER_PLATFORM", "Plattform (für /rust)", "community",
          placeholder="Rust Console Edition (Xbox & PlayStation)"),
    Field("RUST_SERVER_WIPE", "Wipe-Zeiten (für /rust)", "community", placeholder="z. B. Donnerstags 18 Uhr"),
    Field("RULES_FILE", "Datei mit den Regeln", "community", advanced=True, placeholder="rules.md"),
    # Web
    Field("WEB_PASSWORD", "Passwort für dieses Webinterface", "web", "secret",
          help="Leer = bei jedem Start ein Login-Link im Bot-Fenster."),
    Field("WEB_HOST", "Erreichbar für", "web", choices=("127.0.0.1", "0.0.0.0"),
          help="127.0.0.1 = nur dieser PC, 0.0.0.0 = auch Handy/Laptop im Heimnetz. Nie ins Internet freigeben!"),
    Field("WEB_PORT", "Port", "web", "int", placeholder="8080"),
    Field("WEB_OPEN_BROWSER", "Browser beim Start öffnen", "web", "bool"),
    Field("WEB_ENABLED", "Webinterface aktiv", "web", "bool",
          help="Achtung: ausschalten sperrt dich aus dem Webinterface aus (dann nur noch über die .env)."),
    # Vorlagen
    Field("KICK_COMMAND_TEMPLATE", "Kick-Befehl", "templates", placeholder='kick "{name}"'),
    Field("BAN_COMMAND_TEMPLATE", "Ban-Befehl", "templates", placeholder='banid "{name}"'),
    Field("UNBAN_COMMAND_TEMPLATE", "Unban-Befehl", "templates", placeholder='unbanid "{name}"'),
    Field("KIT_GIVE_TEMPLATE", "Kit an Spieler", "templates", placeholder='kit givetoplayer "{kit}" "{name}"'),
    Field("KIT_GIVE_GROUP_TEMPLATE", "Kit an Gruppe", "templates", placeholder='kit givetogroup "{kit}" "{group}"'),
    Field("KIT_GIVE_ALL_TEMPLATE", "Kit an alle", "templates", placeholder='kit giveall "{kit}"'),
    Field("KIT_ADD_TEMPLATE", "Item zu Kit hinzufügen", "templates",
          placeholder='kit add "{kit}" "{item}" "{amount}" "{condition}" "{container}"'),
    Field("KIT_REMOVE_TEMPLATE", "Item aus Kit entfernen", "templates", placeholder='kit remove "{kit}" "{id}"'),
    Field("ITEM_GIVE_TEMPLATE", "Item an Spieler (Custom Kits)", "templates",
          placeholder='inventory.giveto "{name}" "{item}" {amount}'),
    Field("DATABASE_PATH", "Datenbank-Datei", "templates", placeholder="data/bot.db"),
]

FIELDS_BY_KEY = {f.key: f for f in FIELDS}
SECRET_KEYS = {f.key for f in FIELDS if f.kind == "secret"}


def validate(key: str, value: str) -> str | None:
    """Fehlermeldung für einen Wert oder None, wenn er passt."""
    f = FIELDS_BY_KEY.get(key)
    if f is None:
        return "Unbekannte Einstellung."
    value = value.strip()
    if not value:
        return f"„{f.label}“ ist ein Pflichtfeld." if f.required else None
    if f.kind in ("int",) and not value.isdigit():
        return f"„{f.label}“ muss eine Zahl sein."
    if f.kind == "id" and not value.isdigit():
        return f"„{f.label}“ muss eine Discord-ID (nur Ziffern) sein."
    if f.kind == "ids" and not all(p.strip().isdigit() for p in value.split(",") if p.strip()):
        return f"„{f.label}“ muss eine kommagetrennte Liste von IDs sein."
    if f.kind == "bool" and value.lower() not in ("true", "false"):
        return f"„{f.label}“ muss true oder false sein."
    if f.choices and value not in f.choices:
        return f"„{f.label}“: erlaubt sind {', '.join(f.choices)}."
    if key == "RCON_HOST" and (":" in value or "/" in value):
        return "Server-IP bitte ohne Port und ohne „/“ eintragen."
    if "\n" in value and f.kind != "textarea":
        return f"„{f.label}“ darf keine Zeilenumbrüche enthalten."
    return None
