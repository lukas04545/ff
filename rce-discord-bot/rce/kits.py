"""Auswertung der Kit-Befehle der Console Edition.

ANNAHME (Format aus rce.js, Stand 2025):
  * "kit list"          -> ein Kit-Name pro Zeile, Statuszeilen beginnen mit "[KITMANAGER]"
  * "kit info \"Name\"" -> pro Item eine Zeile wie
        Shortname: rifle.ak Amount: [1] Condition: [100] Container: [Belt]
  * "getauthlevels"     -> Gruppenname (z. B. "Admin") als eigene Zeile, darunter die Mitglieder
Ändert sich das Format, nur die Regex/Funktionen in dieser Datei anpassen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ITEM_PATTERN = re.compile(
    r"Shortname:\s*(?P<short>\S+)\s+Amount:\s*\[?(?P<amount>\d+)\]?\s+"
    r"Condition:\s*\[?(?P<condition>\d+)\]?\s+Container:\s*\[?(?P<container>Main|Belt|Wear)\]?",
    re.IGNORECASE,
)
# Item-ID (für "kit remove"), falls der Server sie in derselben Zeile ausgibt – unverifiziert
ITEM_ID_PATTERN = re.compile(r"\bID:\s*\[?(?P<id>\d+)\]?", re.IGNORECASE)

CONTAINERS = ("Main", "Belt", "Wear")
CONTAINER_LABELS = {"main": "Inventar", "belt": "Hotbar", "wear": "Kleidung"}


@dataclass(slots=True)
class KitItem:
    short_name: str
    amount: int
    condition: int
    container: str
    item_id: int | None = None


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.replace("\\n", "\n").splitlines() if line.strip()]


def parse_kit_list(raw: str) -> list[str]:
    names: list[str] = []
    for line in _lines(raw):
        if line.startswith("[KITMANAGER]"):
            continue
        name = line.lstrip("-•* ").strip().strip('"')
        if name and name not in names:
            names.append(name)
    return names


def parse_kit_info(raw: str) -> list[KitItem]:
    items: list[KitItem] = []
    for line in _lines(raw):
        m = ITEM_PATTERN.search(line)
        if not m:
            continue
        id_match = ITEM_ID_PATTERN.search(line)
        items.append(KitItem(
            short_name=m["short"],
            amount=int(m["amount"]),
            condition=int(m["condition"]),
            container=m["container"].capitalize(),
            item_id=int(id_match["id"]) if id_match else None,
        ))
    return items


def parse_auth_groups(raw: str) -> list[str]:
    """Gruppennamen aus "getauthlevels" (Zeilen aus einem einzelnen Wort mit Großbuchstaben)."""
    groups: list[str] = []
    for line in _lines(raw):
        if re.fullmatch(r"[A-Z][A-Za-z]*", line) and line not in groups:
            groups.append(line)
    return groups


def clean_arg(value: str, limit: int = 64) -> str:
    """Argument für Kit-Befehle: keine Anführungszeichen/Zeilenumbrüche."""
    return re.sub(r"[\"\r\n]", "", value).strip()[:limit]
