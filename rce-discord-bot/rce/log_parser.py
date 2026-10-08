"""Erkennt Ereignisse in den Live-Konsolenzeilen der Rust Console Edition.

ANNAHME: Die Muster unten entsprechen dem Log-Format, das RCE-Server über
WebRCON ausgeben (abgeleitet aus der Community-Bibliothek rce.js, Stand 2025).
Facepunch kann das Format mit Updates ändern. Dann hier die Regex anpassen –
der restliche Bot muss nicht angefasst werden. Benannte Gruppen (?P<...>)
müssen erhalten bleiben.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import quickchat
from .kill_sources import KillerType, classify

# --------------------------------------------------------------------------
# Muster – hier anpassen, falls sich das Log-Format ändert
# --------------------------------------------------------------------------
PATTERNS: dict[str, re.Pattern[str]] = {
    # [CHAT LOCAL] Spieler : d11_quick_chat_responses_slot_5
    "chat": re.compile(r"\[CHAT (?P<channel>LOCAL|TEAM|SERVER|GLOBAL)\] (?P<player>.+?) : (?P<message>.+)$"),
    # Opfer was killed by Täter
    "kill": re.compile(r"^(?P<victim>.+?) was killed by (?P<killer>.+?)$"),
    # Spieler was suicide by Suicide
    "suicide": re.compile(r"^(?P<player>.+?) was suicide by Suicide"),
    # Spieler [SCARLETT] has entered the game   (SCARLETT = Xbox, sonst PlayStation)
    "respawn": re.compile(r"^(?P<player>.+?) \[(?P<platform>[^\]]*)\] has entered the game"),
    # [Admin] Added [Spieler] to [Banned]
    "ban": re.compile(r"\[(?P<admin>[^\]]+)\] Added \[(?P<player>[^\]]+)\] to \[Banned\]"),
    "unban": re.compile(r"\[(?P<admin>[^\]]+)\] Removed \[(?P<player>[^\]]+)\] from \[Banned\]"),
    # [Admin] Added [Spieler] to Group [Admin]
    "role_add": re.compile(r"\[(?P<admin>[^\]]+)\] Added \[(?P<player>[^\]]+)\] to Group \[(?P<role>[^\]]+)\]"),
    "role_remove": re.compile(
        r"\[(?P<admin>[^\]]+)\] Removed \[(?P<player>[^\]]+)\] from Group \[(?P<role>[^\]]+)\]"
    ),
    "team_create": re.compile(r"\[(?P<player>[^\]]+)\] created a new team, ID: (?P<team>\d+)"),
    "team_join": re.compile(r"\[(?P<player>[^\]]+)\] has joined \[(?P<owner>[^\]]+)\]s team, ID: \[(?P<team>\d+)\]"),
    "team_leave": re.compile(r"\[(?P<player>[^\]]+)\] has left \[(?P<owner>[^\]]+)\]s team, ID: \[(?P<team>\d+)\]"),
    "team_promote": re.compile(
        r"\[(?P<player>[^\]]+)\] promoted \[(?P<target>[^\]]+)\] as their new leader of the team, ID: \[(?P<team>\d+)\]"
    ),
    # [ServerVar] Admin giving Spieler kit starter
    "kit_give": re.compile(r"\[ServerVar\] (?P<admin>.+?) giving (?P<player>.+?) kit (?P<kit>\S+)"),
    # giving Spieler 1000 x Wood
    "item_spawn": re.compile(r"\bgiving (?P<player>.+?) (?P<amount>[\d.]+) x (?P<item>.+)$"),
    # Executing console system command 'kick "x"'
    "command": re.compile(r"Executing console system command '(?P<command>[^']+)'"),
    "save": re.compile(r"^\[ SAVE \] Saved (?P<entities>\d+) ents"),
}

# Zeilen, die mit "[EVENT" beginnen und einen dieser Bezeichner enthalten
EVENT_NAMES = {
    "event_airdrop": "📦 Airdrop",
    "event_cargoship": "🚢 Cargo Ship",
    "event_cargoheli": "🚁 Chinook",
    "event_helicopter": "🚁 Patrouillen-Heli",
    "event_halloween": "🎃 Halloween-Event",
    "event_xmas": "🎄 Weihnachts-Event",
    "event_easter": "🐣 Oster-Event",
}


# --------------------------------------------------------------------------
# Ereignis-Typen
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ChatEvent:
    channel: str
    player: str
    raw: str
    text: str


@dataclass(slots=True)
class KillEvent:
    victim: str
    victim_type: KillerType
    killer: str
    killer_type: KillerType

    @property
    def is_pvp(self) -> bool:
        return self.victim_type is KillerType.PLAYER and self.killer_type is KillerType.PLAYER


@dataclass(slots=True)
class SuicideEvent:
    player: str


@dataclass(slots=True)
class RespawnEvent:
    player: str
    platform: str  # "Xbox" / "PlayStation"


@dataclass(slots=True)
class BanEvent:
    admin: str
    player: str
    banned: bool


@dataclass(slots=True)
class RoleEvent:
    admin: str
    player: str
    role: str
    added: bool


@dataclass(slots=True)
class TeamEvent:
    kind: str  # create / join / leave / promote
    player: str
    team_id: int
    other: str | None = None


@dataclass(slots=True)
class AdminActionEvent:
    kind: str  # kit / item / command
    text: str


@dataclass(slots=True)
class ServerEvent:
    key: str
    name: str


@dataclass(slots=True)
class SaveEvent:
    entities: int


Event = (
    ChatEvent | KillEvent | SuicideEvent | RespawnEvent | BanEvent | RoleEvent
    | TeamEvent | AdminActionEvent | ServerEvent | SaveEvent
)


def parse_line(line: str) -> Event | None:
    """Wandelt eine Konsolenzeile in ein Ereignis um (oder None)."""
    line = line.strip()
    if not line:
        return None

    if m := PATTERNS["chat"].search(line):
        raw = m["message"].strip()
        return ChatEvent(m["channel"], m["player"].strip(), raw, quickchat.translate(raw))

    if m := PATTERNS["suicide"].search(line):
        return SuicideEvent(m["player"].strip())

    if " was killed by " in line and (m := PATTERNS["kill"].search(line)):
        v_type, v_name = classify(m["victim"])
        k_type, k_name = classify(m["killer"])
        return KillEvent(v_name, v_type, k_name, k_type)

    if m := PATTERNS["respawn"].search(line):
        platform = "Xbox" if "SCARLETT" in m["platform"].upper() else "PlayStation"
        return RespawnEvent(m["player"].strip(), platform)

    for key, banned in (("ban", True), ("unban", False)):
        if m := PATTERNS[key].search(line):
            return BanEvent(m["admin"], m["player"], banned)

    for key, added in (("role_add", True), ("role_remove", False)):
        if m := PATTERNS[key].search(line):
            return RoleEvent(m["admin"], m["player"], m["role"], added)

    if m := PATTERNS["team_create"].search(line):
        return TeamEvent("create", m["player"], int(m["team"]))
    for kind in ("join", "leave"):
        if m := PATTERNS[f"team_{kind}"].search(line):
            return TeamEvent(kind, m["player"], int(m["team"]), m["owner"])
    if m := PATTERNS["team_promote"].search(line):
        return TeamEvent("promote", m["player"], int(m["team"]), m["target"])

    if m := PATTERNS["kit_give"].search(line):
        return AdminActionEvent("kit", f"{m['admin']} gibt {m['player']} Kit `{m['kit']}`")
    if m := PATTERNS["item_spawn"].search(line):
        return AdminActionEvent("item", f"{m['player']} erhält {m['amount']} × {m['item']}")
    if m := PATTERNS["command"].search(line):
        return AdminActionEvent("command", m["command"])

    if m := PATTERNS["save"].search(line):
        return SaveEvent(int(m["entities"]))

    if line.upper().startswith("[EVENT"):
        lowered = line.lower()
        for key, name in EVENT_NAMES.items():
            if key in lowered:
                return ServerEvent(key, name)

    return None
