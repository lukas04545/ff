"""Location teleports: the 8 Location quick chat emotes (North, Northeast … Northwest) are teleport points.

An admin sets each one's coordinates – typed in, or captured from where an admin stands in game (server.printpos) –
and a player who uses that emote in game is teleported there (global.teleportpos). Location emotes can only be
teleports, never kits. Used by the Teleports cog (/teleport …), the web panel and the management AI.
"""
from __future__ import annotations

import re

from rce import quickchat

LOCATION_PREFIX = "d11_quick_chat_location_slot_"
LOCATIONS = [f"{LOCATION_PREFIX}{i}" for i in range(8)]
SHORT = {"n": 0, "ne": 1, "e": 2, "se": 3, "s": 4, "sw": 5, "w": 6, "nw": 7}

TELEPORT_DEFAULT = 'global.teleportpos ({x},{y},{z}) "{name}"'
POSITION_DEFAULT = 'server.printpos "{name}"'
MAX_COORD = 10_000.0                         # Rust maps are at most ~6 km; anything bigger is a typo

_NUMBER = r"(-?\d+(?:\.\d+)?)"
POSITION_PAREN = re.compile(r"\(\s*" + _NUMBER + r"\s*,\s*" + _NUMBER + r"\s*,\s*" + _NUMBER + r"\s*\)")
POSITION_FALLBACK = re.compile(_NUMBER + r"[\s,;]+" + _NUMBER + r"[\s,;]+" + _NUMBER)


def is_location(raw: str | None) -> bool:
    return bool(raw) and str(raw).startswith(LOCATION_PREFIX)


def label(raw: str) -> str:
    return quickchat.translate(raw)


def locations() -> list[dict]:
    return [{"raw": raw, "text": label(raw)} for raw in LOCATIONS]


def find(query: str) -> str | None:
    """'north', 'NE', 'North-west', 'Location › South' or the raw id -> raw id."""
    q = re.sub(r"[^a-z]", "", str(query or "").lower().replace("location", ""))
    if str(query or "").strip() in LOCATIONS:
        return str(query).strip()
    if q in SHORT:
        return LOCATIONS[SHORT[q]]
    for raw in LOCATIONS:
        if re.sub(r"[^a-z]", "", label(raw).lower()) == q:
            return raw
    return None


def parse_position(reply: str) -> tuple[float, float, float] | None:
    """x, y, z from the server's answer to server.printpos – "(12.3, 45.6, -78.9)" or three numbers."""
    m = POSITION_PAREN.search(reply or "") or POSITION_FALLBACK.search(reply or "")
    if m is None:
        return None
    try:
        return float(m[1]), float(m[2]), float(m[3])
    except (TypeError, ValueError):
        return None


def check_coords(x, y, z) -> tuple[float, float, float]:
    """Numbers within the map, else ValueError with a readable message."""
    try:
        out = (float(x), float(y), float(z))
    except (TypeError, ValueError):
        raise ValueError("x, y and z must be numbers.") from None
    if any(v != v or abs(v) > MAX_COORD for v in out):
        raise ValueError(f"Coordinates must be between -{MAX_COORD:.0f} and {MAX_COORD:.0f}.")
    return out


def fmt(x: float, y: float, z: float) -> str:
    return f"({x:.1f}, {y:.1f}, {z:.1f})"
