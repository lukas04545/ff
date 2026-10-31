"""Emote binds: a kit given when a player uses a quick chat emote/phrase in game (an autokit with trigger
"quickchat"). One place for the Discord commands (/kit bind …), the web panel's emote board and the management AI.

Binding an emote that already gives a kit replaces that bind – one emote, one kit – so binding is a single step.
Emotes can be named the way people say them: "thanks", "Responses > Thanks", "i need scrap" or the raw console id.
The Location emotes (North … Northwest) are teleport points only (support/teleports.py) – they never give kits.
"""
from __future__ import annotations

import difflib
import re

from rce import quickchat
from support import teleports

CATEGORY_LABELS = {
    "combat": "Combat", "building": "Building", "questions": "Questions", "responses": "Responses",
    "orders": "Orders", "location": "Location",
    "activities_phrase_format": "Going to farm", "i_need_phrase_format": "I need", "i_have_phrase_format": "I have",
}
_PREFIX = "d11_quick_chat_"


def category(raw: str) -> str:
    head = raw.split(" ", 1)[0]
    if head.startswith(_PREFIX):
        rest = head[len(_PREFIX):]
        if "_slot_" in rest:
            rest = rest.rpartition("_slot_")[0]
        return CATEGORY_LABELS.get(rest, rest.replace("_", " ").title())
    return "Other"


def emotes() -> list[dict]:
    """Every emote that can give a kit: {"raw", "text", "category", "label"} in the in-game order (no Location
    emotes – those are teleports)."""
    out = []
    for raw, text in quickchat.all_phrases().items():
        if teleports.is_location(raw):
            continue
        cat = category(raw)
        out.append({"raw": raw, "text": text, "category": cat, "label": f"{cat} › {text}"})
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def find(query: str) -> tuple[str | None, list[str]]:
    """The raw emote id for what someone typed. Returns (raw or None, suggestions as labels)."""
    query = str(query or "").strip()
    if not query:
        return None, []
    if teleports.is_location(query) or teleports.find(query):
        return None, ["Location emotes are teleports – set them with /teleport set or Kits → 📍 Teleports"]
    all_ = emotes()
    for e in all_:                                       # exact: raw id
        if e["raw"] == query:
            return e["raw"], []
    q = _norm(query)
    for key in ("label", "text"):                        # exact label/text, any case/punctuation
        hits = [e for e in all_ if _norm(e[key]) == q]
        if len(hits) == 1:
            return hits[0]["raw"], []
        if hits:
            return None, [h["label"] for h in hits]
    for e in all_:                                       # "category > text" with a loose text part
        if _norm(e["category"]) and q.startswith(_norm(e["category"]) + " ") and \
                _norm(e["text"]) == q[len(_norm(e["category"])) + 1:]:
            return e["raw"], []
    contains = [e for e in all_ if q and q in _norm(e["label"])]
    if len(contains) == 1:
        return contains[0]["raw"], []
    labels = {_norm(e["label"]): e["label"] for e in all_}
    close = difflib.get_close_matches(q, list(labels), n=5, cutoff=0.55)
    suggestions = [e["label"] for e in contains[:5]] or [labels[c] for c in close]
    return None, suggestions


def label(raw: str) -> str:
    return f"{category(raw)} › {quickchat.translate(raw)}"


def binds(db) -> list[dict]:
    """All emote binds: {"rule_id", "raw", "label", "kit", "kit_type", "cooldown_minutes", "max_claims",
    "required_role_id", "enabled"}."""
    return [{"rule_id": r["id"], "raw": r["phrase"], "label": label(r["phrase"] or ""), "kit": r["kit"],
             "kit_type": r["kit_type"], "cooldown_minutes": r["cooldown_minutes"], "max_claims": r["max_claims"],
             "required_role_id": r["required_role_id"], "enabled": bool(r["enabled"])}
            for r in db.kit_rules(trigger="quickchat")]


def bound_to(db, raw: str) -> list:
    return [r for r in db.kit_rules(trigger="quickchat") if r["phrase"] == raw]


def bind(db, raw: str, kit: str, *, kit_type: str = "ingame", cooldown_minutes: int = 0, max_claims: int = 0,
         required_role_id: int | None = None, by: str = "") -> tuple[int, list[str]]:
    """Binds the kit to the emote, replacing what the emote gave before. Returns (rule id, replaced kits).
    Location emotes are teleports only: ValueError."""
    if teleports.is_location(raw):
        raise ValueError("Location emotes are teleports only – they can't give kits.")
    replaced = []
    for old in bound_to(db, raw):
        replaced.append(old["kit"])
        db.delete_kit_rule(old["id"])
    rule_id = db.add_kit_rule(kit, "quickchat", raw, max(0, int(cooldown_minutes)), max(0, int(max_claims)),
                              by or "bot", "custom" if kit_type == "custom" else "ingame", required_role_id)
    return rule_id, replaced


def unbind(db, raw: str) -> list[str]:
    """Removes every kit bound to this emote. Returns the kits that were bound."""
    removed = []
    for old in bound_to(db, raw):
        removed.append(old["kit"])
        db.delete_kit_rule(old["id"])
    return removed
