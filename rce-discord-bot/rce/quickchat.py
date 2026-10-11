"""Translation of Console Edition quick chat phrases.

IMPORTANT: Rust Console Edition has no free-text chat. Players can only send
predefined quick chat phrases; in the console these show up as identifiers
like "d11_quick_chat_combat_slot_0". This file turns them into text.
Unknown identifiers are passed through unchanged.
"""
from __future__ import annotations

_SLOTS: dict[str, list[str]] = {
    "combat": [
        "We're under attack!", "Retreat!", "Go, go, go!", "Don't shoot!",
        "Be careful!", "They're better armed!", "I'm out of ammo!", "I'm hurt!",
    ],
    "building": [
        "Upgrade the walls!", "We need beds!", "I need building privilege!", "What's the door code?",
        "Can I have a key?", "We need a better door!", "Upkeep is running low!",
        "Which box is free?",
    ],
    "questions": [
        "Are you friendly?", "Can I build here?", "Want to team up?", "Do you need anything?",
        "Can you help me?", "Want to trade?", "Who's there?", "Can I come in?",
    ],
    "responses": ["Yes", "No", "OK", "Thanks", "No problem", "Hello", "Bye", "Sorry"],
    "orders": [
        "Follow me!", "Go away!", "Repair this!", "Wait here!",
        "Come in!", "Let's go!", "Here, take this!", "Hurry up!",
    ],
    "location": ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"],
}

_ITEMS = {
    "d11_stone": "Stone", "stones": "Stone", "d11_wood": "Wood", "d11_metal": "Metal",
    "d11_food": "Food", "d11_water": "Water", "d11_scrap": "Scrap",
    "d11_metal_fragments": "Metal Fragments", "d11_medicine": "Medicine",
    "lowgradefuel": "Low Grade Fuel", "metal.refined": "HQ Metal",
    "bow.hunting": "Hunting Bow", "pickaxe": "Pickaxe", "hatchet": "Hatchet",
}

_FORMATS = {
    "d11_quick_chat_activities_phrase_format": "I'm going to farm {item}.",
    "d11_quick_chat_i_need_phrase_format": "I need {item}.",
    "d11_quick_chat_i_have_phrase_format": "I have {item}.",
}


def translate(raw: str) -> str:
    text = raw.strip()
    parts = text.split(" ", 1)
    if parts[0] in _FORMATS and len(parts) == 2:
        item = _ITEMS.get(parts[1].lower(), parts[1])
        return _FORMATS[parts[0]].format(item=item)

    prefix = "d11_quick_chat_"
    if text.startswith(prefix) and "_slot_" in text:
        category, _, slot = text[len(prefix):].rpartition("_slot_")
        phrases = _SLOTS.get(category)
        if phrases and slot.isdigit() and int(slot) < len(phrases):
            return phrases[int(slot)]
    return text


# Valid combinations of the format phrases (original spelling as in the log)
_FORMAT_ITEMS = {
    "d11_quick_chat_activities_phrase_format": [
        "d11_Stone", "d11_Wood", "d11_Metal", "d11_Food", "d11_Water", "d11_Scrap",
        "d11_Metal_Fragments", "d11_Medicine",
    ],
    "d11_quick_chat_i_need_phrase_format": [
        "d11_Scrap", "lowgradefuel", "d11_Food", "d11_Water", "d11_Wood", "stones",
        "d11_Metal_Fragments", "metal.refined",
    ],
    "d11_quick_chat_i_have_phrase_format": [
        "d11_Scrap", "lowgradefuel", "d11_Food", "d11_Water", "bow.hunting", "pickaxe",
        "hatchet", "metal.refined",
    ],
}


def all_phrases() -> dict[str, str]:
    """All known quick chat identifiers -> English text (e.g. for autokit triggers)."""
    phrases: dict[str, str] = {}
    for category, texts in _SLOTS.items():
        for slot in range(len(texts)):
            raw = f"d11_quick_chat_{category}_slot_{slot}"
            phrases[raw] = translate(raw)
    for fmt, items in _FORMAT_ITEMS.items():
        for item in items:
            raw = f"{fmt} {item}"
            phrases[raw] = translate(raw)
    return phrases
