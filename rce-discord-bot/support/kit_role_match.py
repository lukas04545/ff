"""Which existing Discord role belongs to which kit (cogs/custom_kits.py, "Kit roles").

Kit names and role names rarely match exactly ("m4 spawn kit" vs the role "M4 Spawn", "mp5 roadsgin" vs
"MP5 Roadsign"), so names are compared loosely: lower case, without punctuation and filler words like "kit", and
small typos are tolerated. What that can't decide, the AI (DeepSeek) matches – it only picks a role when it is
clearly the same kit and otherwise says none.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

FILLER = {"kit", "kits", "role", "roles", "the", "access", "pack"}
CLOSE_ENOUGH = 0.86            # similarity of the cleaned names that still counts as "the same" (typos)


def normalize(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split()
    return " ".join(w for w in words if w not in FILLER)


def name_match(kit: str, roles: list[tuple[int, str]]) -> int | None:
    """The role id whose (cleaned) name is the kit's – or clearly the same with a small typo. None if unsure."""
    key = normalize(kit)
    if not key:
        return None
    exact = [rid for rid, name in roles if normalize(name) == key]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None                                   # several look the same – let a human (or the AI) decide
    scored = sorted(((SequenceMatcher(None, key, normalize(name)).ratio(), rid) for rid, name in roles),
                    reverse=True)
    if scored and scored[0][0] >= CLOSE_ENOUGH and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
        return scored[0][1]
    return None


PROMPT = """You connect the kits of a Rust (console edition) game server to the server's existing Discord roles.
A kit's role is the role that gives a player that kit (often the kit's name with different spelling, capitals,
extra words like "kit", "VIP", an emoji or a small typo). Only pick a role if it clearly belongs to that kit – never
a general role like Admin, Moderator, Member, Verified, Booster or a different kit's role. One role per kit; a role
may fit more than one kit only if it obviously covers them. If unsure, use null.

Answer with JSON only: {"matches": [{"kit": "<kit name exactly as given>", "role_id": "<role id or null>"}]}"""


async def ai_match(ai, kits: list[str], roles: list[tuple[int, str]]) -> dict[str, int | None]:
    """Asks the AI which role belongs to which kit. -> {kit: role id or None}; only ids from `roles`."""
    if not kits or not roles:
        return {}
    listing = {"kits": kits, "roles": [{"id": str(rid), "name": name} for rid, name in roles]}
    reply = await ai.chat([{"role": "system", "content": PROMPT},
                           {"role": "user", "content": json.dumps(listing, ensure_ascii=False)}],
                          json_mode=True, max_tokens=2000, temperature=0.1)
    try:
        data = json.loads(reply["content"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}
    valid = {rid for rid, _ in roles}
    wanted = {k.lower(): k for k in kits}
    out: dict[str, int | None] = {}
    for m in data.get("matches", []) if isinstance(data, dict) else []:
        if not isinstance(m, dict):
            continue
        kit = wanted.get(str(m.get("kit") or "").lower())
        rid = str(m.get("role_id") or "")
        if kit is None:
            continue
        out[kit] = int(rid) if rid.isdigit() and int(rid) in valid else None
    return out
