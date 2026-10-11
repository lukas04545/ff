"""Parsing of the Console Edition kit commands.

ASSUMPTION (format from rce.js, as of 2025):
  * "kit list"          -> one kit name per line, status lines start with "[KITMANAGER]"
  * "kit info \"Name\"" -> one line per item, like
        Shortname: rifle.ak Amount: [1] Condition: [100] Container: [Belt]
  * "getauthlevels"     -> group name (e.g. "Admin") on its own line, followed by its members
If the format changes, only adjust the regexes/functions in this file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ITEM_PATTERN = re.compile(
    r"Shortname:\s*(?P<short>\S+)\s+Amount:\s*\[?(?P<amount>\d+)\]?\s+"
    r"Condition:\s*\[?(?P<condition>\d+)\]?\s+Container:\s*\[?(?P<container>Main|Belt|Wear)\]?",
    re.IGNORECASE,
)
# Item ID (for "kit remove"), if the server prints it on the same line – unverified
ITEM_ID_PATTERN = re.compile(r"\bID:\s*\[?(?P<id>\d+)\]?", re.IGNORECASE)

CONTAINERS = ("Main", "Belt", "Wear")
CONTAINER_LABELS = {"main": "Inventory", "belt": "Hotbar", "wear": "Clothing"}


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
    """Group names from "getauthlevels" (lines consisting of a single capitalized word)."""
    groups: list[str] = []
    for line in _lines(raw):
        if re.fullmatch(r"[A-Z][A-Za-z]*", line) and line not in groups:
            groups.append(line)
    return groups


def clean_arg(value: str, limit: int = 64) -> str:
    """Argument for kit commands: no quotes/line breaks."""
    return re.sub(r"[\"\r\n]", "", value).strip()[:limit]
