"""Discord-Rechte des Bots prüfen und „403 Forbidden“ verständlich erklären.

* explain_forbidden(): macht aus discord.Forbidden eine Meldung, WAS verweigert wurde und WELCHES Recht fehlt.
* check_permissions(): prüft beim Start alle konfigurierten Channels/Rollen und listet fehlende Rechte
  (Bot-Fenster + Webinterface-Übersicht).
Die Rechte-Namen entsprechen der deutschen Discord-Oberfläche.
"""
from __future__ import annotations

import re

import discord

PERM_LABELS = {
    "view_channel": "Kanäle ansehen",
    "send_messages": "Nachrichten senden",
    "embed_links": "Links einbetten",
    "attach_files": "Dateien anhängen",
    "read_message_history": "Nachrichtenverlauf anzeigen",
    "manage_channels": "Kanäle verwalten",
    "manage_roles": "Rollen verwalten",
    "manage_messages": "Nachrichten verwalten",
    "kick_members": "Mitglieder kicken",
    "ban_members": "Mitglieder bannen",
    "moderate_members": "Mitglieder moderieren",
    "add_reactions": "Reaktionen hinzufügen",
}

SEND = ("view_channel", "send_messages", "embed_links")
FIX_HINT = ("Beheben: Servereinstellungen → Rollen → Rolle des Bots → Rechte einschalten und die Bot-Rolle "
            "möglichst weit nach oben ziehen. Hat der Kanal/die Kategorie eigene Berechtigungen, dort die "
            "Bot-Rolle ebenfalls erlauben.")

# (HTTP-Methode, Pfad-Regex, Aktion, nötige Rechte, Rollen-Hierarchie relevant)
_ROUTES: list[tuple[str, str, str, tuple[str, ...], bool]] = [
    ("POST", r"/guilds/\d+/channels$", "Kanal erstellen",
     ("view_channel", "manage_channels", "send_messages", "embed_links", "attach_files", "read_message_history"), False),
    ("PUT", r"/channels/(?P<ch>\d+)/permissions/\d+$", "Kanal-Berechtigungen setzen",
     ("view_channel", "manage_roles", "manage_channels"), False),
    ("DELETE", r"/channels/(?P<ch>\d+)/permissions/\d+$", "Kanal-Berechtigungen entfernen",
     ("view_channel", "manage_roles", "manage_channels"), False),
    ("PATCH", r"/channels/(?P<ch>\d+)$", "Kanal bearbeiten", ("view_channel", "manage_channels"), False),
    ("DELETE", r"/channels/(?P<ch>\d+)$", "Kanal löschen", ("view_channel", "manage_channels"), False),
    ("POST", r"/channels/(?P<ch>\d+)/messages/bulk-delete$", "Nachrichten löschen",
     ("view_channel", "manage_messages", "read_message_history"), False),
    ("POST", r"/channels/(?P<ch>\d+)/messages$", "Nachricht senden", SEND + ("attach_files",), False),
    ("PATCH", r"/channels/(?P<ch>\d+)/messages/\d+$", "Nachricht bearbeiten", SEND, False),
    ("DELETE", r"/channels/(?P<ch>\d+)/messages/\d+$", "Nachricht löschen", ("view_channel", "manage_messages"), False),
    ("GET", r"/channels/(?P<ch>\d+)/messages", "Nachrichten lesen", ("view_channel", "read_message_history"), False),
    ("PUT", r"/channels/(?P<ch>\d+)/messages/\d+/reactions/", "Reaktion hinzufügen",
     ("view_channel", "add_reactions", "read_message_history"), False),
    ("PUT", r"/guilds/\d+/members/\d+/roles/\d+$", "Rolle vergeben", ("manage_roles",), True),
    ("DELETE", r"/guilds/\d+/members/\d+/roles/\d+$", "Rolle entfernen", ("manage_roles",), True),
    ("PUT", r"/guilds/\d+/bans/\d+$", "Mitglied bannen", ("ban_members",), True),
    ("DELETE", r"/guilds/\d+/bans/\d+$", "Mitglied entbannen", ("ban_members",), False),
    ("DELETE", r"/guilds/\d+/members/\d+$", "Mitglied kicken", ("kick_members",), True),
    ("PATCH", r"/guilds/\d+/members/\d+$", "Timeout setzen/aufheben", ("moderate_members",), True),
]


def perm_names(perms) -> str:
    return ", ".join(f"„{PERM_LABELS.get(p, p)}“" for p in perms)


def main_guild(bot) -> discord.Guild | None:
    """Der Discord-Server des Bots: GUILD_ID, sonst der einzige Server."""
    if bot.config.guild_id:
        return bot.get_guild(bot.config.guild_id)
    return bot.guilds[0] if len(bot.guilds) == 1 else None


def explain_forbidden(exc: discord.HTTPException, bot=None) -> str:
    """Meldung für Discord-Fehler 403: welche Aktion, wo, welches Recht fehlt."""
    response = getattr(exc, "response", None)
    method = str(getattr(response, "method", "") or "").upper()
    url = getattr(response, "url", None)
    path = re.sub(r"^/api/v\d+", "", url.path if url is not None else "")
    code = getattr(exc, "code", 0)
    detail = f"Discord-Fehler {code}: {exc.text}" if code else "Discord-Fehler 403"

    if code == 50007:
        return f"⚠️ Discord lässt keine Direktnachricht an diesen Nutzer zu (DMs geschlossen). ({detail})"

    route = next(((action, perms, hierarchy, re.search(rx, path))
                  for m, rx, action, perms, hierarchy in _ROUTES
                  if m == method and re.search(rx, path)), None)
    if route is None:
        return (f"⚠️ Discord hat die Aktion verweigert ({detail}, {method} {path or '?'}). "
                "Dem Bot fehlen Rechte oder seine Rolle steht zu tief.\n" + FIX_HINT)

    action, perms, hierarchy, match = route
    channel_id = match.groupdict().get("ch") if match else None
    where = f" in <#{channel_id}>" if channel_id else ""
    lines = [f"⚠️ Discord hat **{action}**{where} verweigert ({detail})."]

    # Wenn möglich, die tatsächlich fehlenden Rechte ermitteln (aus dem Cache des Bots)
    missing: list[str] | None = None
    guild = main_guild(bot) if bot is not None else None
    if guild is not None and guild.me is not None:
        scope = guild.get_channel(int(channel_id)) if channel_id else None
        have = scope.permissions_for(guild.me) if scope is not None else guild.me.guild_permissions
        missing = [p for p in perms if not getattr(have, p, True)]
    if code == 50001:
        lines.append(f"Der Bot kann den Kanal nicht sehen – ihm fehlt dort {perm_names(['view_channel'])}.")
    elif missing:
        lines.append(f"Dem Bot fehlt{where or ' auf dem Server'}: {perm_names(missing)}.")
    elif missing is None or not hierarchy:
        lines.append(f"Nötige Rechte{where}: {perm_names(perms)}.")
    if hierarchy:
        lines.append("Wichtig: Die Rolle des Bots muss in der Rollenliste **über** der Rolle bzw. dem Mitglied "
                     "stehen, das er ändern soll. Den Server-Eigentümer kann kein Bot ändern.")
    lines.append(FIX_HINT)
    return "\n".join(lines)


def check_permissions(bot) -> list[str]:
    """Prüft alle konfigurierten Channels/Rollen. Liefert verständliche Problem-Texte (leer = alles ok)."""
    cfg = bot.config
    guild = main_guild(bot)
    if guild is None:
        if cfg.guild_id:
            return [f"Der Bot ist nicht auf dem Discord-Server mit der GUILD_ID {cfg.guild_id} – "
                    "über den Einladungslink einladen oder GUILD_ID prüfen."]
        return []
    me = guild.me
    if me is None:
        return []
    problems: list[str] = []

    channels: list[tuple[str, int | None, tuple[str, ...]]] = [
        ("Status-Channel", cfg.status_channel_id, SEND + ("read_message_history",)),
        ("Chat-Bridge-Channel", cfg.chat_channel_id, SEND + ("read_message_history", "add_reactions")),
        ("Killfeed-Channel", cfg.killfeed_channel_id, SEND),
        ("Event-Channel", cfg.events_channel_id, SEND),
        ("Admin-Log-Channel", cfg.admin_log_channel_id, SEND),
        ("Konsolen-Log-Channel", cfg.console_log_channel_id, SEND),
        ("Ticket-Log-Channel", cfg.ticket_log_channel_id, SEND + ("attach_files",)),
        ("Discord-Log-Channel", cfg.discord_log_channel_id, SEND),
        ("Willkommens-Channel", cfg.welcome_channel_id, SEND),
        ("Regel-Channel", cfg.rules_channel_id, SEND),
        ("Ticket-Kategorie", cfg.ticket_category_id,
         ("view_channel", "manage_channels", "send_messages", "embed_links", "attach_files",
          "read_message_history")),
    ]
    for label, channel_id, perms in channels:
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:
            problems.append(f"{label} (ID {channel_id}) nicht gefunden – gelöscht, falsche ID oder für den Bot "
                            f"unsichtbar (dort {perm_names(['view_channel'])} erlauben).")
            continue
        have = channel.permissions_for(me)
        missing = [p for p in perms if not getattr(have, p)]
        if missing:
            problems.append(f"{label} #{channel.name}: Dem Bot fehlt {perm_names(missing)}.")

    roles = [("Auto-Rolle", cfg.auto_role_id), ("Giveaway-Gewinnerrolle", cfg.giveaway_winner_role_id)]
    for label, role_id in roles:
        if not role_id:
            continue
        role = guild.get_role(role_id)
        if role is None:
            problems.append(f"{label} (ID {role_id}) nicht gefunden.")
            continue
        if not me.guild_permissions.manage_roles:
            problems.append(f"{label} @{role.name}: Dem Bot fehlt {perm_names(['manage_roles'])}.")
        elif role >= me.top_role:
            problems.append(f"{label} @{role.name} steht über der Bot-Rolle @{me.top_role.name} – "
                            "Bot-Rolle in Servereinstellungen → Rollen darüber ziehen.")

    mod = [p for p in ("kick_members", "ban_members", "moderate_members", "manage_messages")
           if not getattr(me.guild_permissions, p)]
    if mod:
        problems.append(f"Für die /mod-Befehle fehlt dem Bot auf dem Server: {perm_names(mod)}.")
    return problems
