"""Übersetzung der Quick-Chat-Phrasen der Console Edition.

WICHTIG: Rust Console Edition hat keinen freien Text-Chat. Spieler können nur
vorgefertigte Quick-Chat-Phrasen senden; in der Konsole erscheinen diese als
Bezeichner wie "d11_quick_chat_combat_slot_0". Diese Datei macht daraus Text.
Unbekannte Bezeichner werden unverändert durchgereicht.
"""
from __future__ import annotations

_SLOTS: dict[str, list[str]] = {
    "combat": [
        "Wir werden angegriffen!", "Rückzug!", "Los geht's!", "Nicht schießen!",
        "Sei vorsichtig!", "Die sind besser bewaffnet!", "Ich habe keine Munition mehr!", "Ich bin verletzt!",
    ],
    "building": [
        "Wände aufwerten!", "Wir brauchen Betten!", "Ich brauche Bauberechtigung!", "Wie ist der Türcode?",
        "Kann ich einen Schlüssel haben?", "Wir brauchen eine bessere Tür!", "Upkeep wird knapp!",
        "Welche Kiste ist frei?",
    ],
    "questions": [
        "Bist du freundlich?", "Darf ich hier bauen?", "Willst du ein Team bilden?", "Brauchst du etwas?",
        "Kannst du mir helfen?", "Willst du handeln?", "Wer ist da?", "Darf ich reinkommen?",
    ],
    "responses": ["Ja", "Nein", "OK", "Danke", "Kein Problem", "Hallo", "Tschüss", "Tut mir leid"],
    "orders": [
        "Folge mir!", "Verschwinde!", "Repariere das!", "Warte hier!",
        "Komm rein!", "Lass uns gehen!", "Hier, nimm das!", "Beeil dich!",
    ],
    "location": ["Norden", "Nordosten", "Osten", "Südosten", "Süden", "Südwesten", "Westen", "Nordwesten"],
}

_ITEMS = {
    "d11_stone": "Stein", "stones": "Stein", "d11_wood": "Holz", "d11_metal": "Metall",
    "d11_food": "Essen", "d11_water": "Wasser", "d11_scrap": "Scrap",
    "d11_metal_fragments": "Metallfragmente", "d11_medicine": "Medizin",
    "lowgradefuel": "Low Grade Fuel", "metal.refined": "HQ-Metall",
    "bow.hunting": "Jagdbogen", "pickaxe": "Spitzhacke", "hatchet": "Beil",
}

_FORMATS = {
    "d11_quick_chat_activities_phrase_format": "Ich gehe {item} farmen.",
    "d11_quick_chat_i_need_phrase_format": "Ich brauche {item}.",
    "d11_quick_chat_i_have_phrase_format": "Ich habe {item}.",
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


# Gültige Kombinationen der Format-Phrasen (Original-Schreibweise wie im Log)
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
    """Alle bekannten Quick-Chat-Bezeichner -> deutscher Text (z. B. für Autokit-Auslöser)."""
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
