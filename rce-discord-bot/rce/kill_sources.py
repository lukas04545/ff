"""Einordnung von Todesursachen aus der Konsolenzeile "<Opfer> was killed by <Täter>".

Auf der Console Edition stehen in Kill-Zeilen nur Namen bzw. Prefab-Bezeichner,
KEINE Waffen, Distanzen oder Spieler-IDs. Diese Liste übersetzt bekannte
Bezeichner in lesbare Namen (Quelle: Community-Bibliothek rce.js) und kann
jederzeit erweitert werden – Schlüssel immer kleingeschrieben.
"""
from __future__ import annotations

from enum import Enum


class KillerType(str, Enum):
    PLAYER = "player"
    NATURAL = "natural"   # Hunger, Kälte, Sturz, ...
    ENTITY = "entity"     # Fallen, Turrets, Wände, Fahrzeuge
    NPC = "npc"           # Tiere, Scientists, Heli, Bradley


_NATURAL = {
    "thirst": "Durst", "hunger": "Hunger", "cold": "Kälte", "bleeding": "Verbluten",
    "fall": "Sturz", "fall!": "Sturz", "drowned": "Ertrunken", "radiation": "Strahlung",
    "pee pee 9000": "Pee Pee 9000",
}

_NPC = {
    "bear": "Bär", "bear (bear)": "Bär", "boar": "Wildschwein", "boar (boar)": "Wildschwein",
    "wolf": "Wolf", "wolf (wolf)": "Wolf",
    "bradleyapc (entity)": "Bradley APC", "patrolhelicopter (entity)": "Patrouillen-Heli",
}

_ENTITY = {
    "guntrap.deployed": "Schrotflintenfalle", "guntrap.deployed (entity)": "Schrotflintenfalle",
    "autoturret_deployed": "Auto-Turret", "autoturret_deployed (entity)": "Auto-Turret",
    "flameturret.deployed (entity)": "Flammenturret", "flameturret_fireball (entity)": "Flammenturret",
    "sam_site_turret_deployed (entity)": "SAM-Site", "sam_static (entity)": "SAM-Site",
    "sentry.bandit.static (entity)": "Bandit-Sentry", "sentry.scientist.static (entity)": "Scientist-Sentry",
    "teslacoil.deployed (entity)": "Teslaspule", "landmine (entity)": "Landmine",
    "beartrap (entity)": "Bärenfalle", "spikes.floor (entity)": "Bodenspieße",
    "barricade.wood": "Holzbarrikade", "barricade.wood (entity)": "Holzbarrikade",
    "barricade.woodwire (entity)": "Holzbarrikade", "barricade.metal (entity)": "Metallbarrikade",
    "wall.external.high": "Hohe Holzmauer", "wall.external.high (entity)": "Hohe Holzmauer",
    "wall.external.high.wood (entity)": "Hohe Holzmauer",
    "wall.external.high.stone": "Hohe Steinmauer", "wall.external.high.stone (entity)": "Hohe Steinmauer",
    "wall.external.high.ice (entity)": "Hohe Eismauer", "wall.external.high.adobe (entity)": "Hohe Lehmmauer",
    "gates.external.high.wood": "Hohes Holztor", "gates.external.high.wood (entity)": "Hohes Holztor",
    "gates.external.high.stone": "Hohes Steintor", "gates.external.high.stone (entity)": "Hohes Steintor",
    "gates.external.high.adobe (entity)": "Hohes Lehmtor", "icewall (entity)": "Eiswand",
    "graveyardfence (entity)": "Friedhofszaun", "lock.code (entity)": "Codeschloss",
    "campfire (entity)": "Lagerfeuer", "fireball (entity)": "Feuer", "fireball_small (entity)": "Feuer",
    "oilfireballsmall (entity)": "Ölfeuer", "napalm (entity)": "Napalm",
    "cargoshipdynamic1 (entity)": "Cargo Ship", "cargoshipdynamic2 (entity)": "Cargo Ship",
    "rocket_crane_lift_trigger (entity)": "Kranlift", "rowboat (entity)": "Ruderboot",
    "hotairballoon (entity)": "Heißluftballon", "minicopter.entity (entity)": "Minicopter",
    "attackhelicopter.entity (entity)": "Kampfhubschrauber",
    "scraptransporthelicopter (entity)": "Scrap-Heli", "ch47scientists.entity (entity)": "Chinook",
    "submarinesolo.entity (entity)": "U-Boot", "submarineduo.entity (entity)": "U-Boot",
}
_ENTITY.update({f"cactus-{i} (entity)": "Kaktus" for i in range(1, 8)})


def classify(raw: str) -> tuple[KillerType, str]:
    """Gibt (Typ, Anzeigename) für einen Namen aus einer Kill-Zeile zurück."""
    key = raw.strip().lower()
    if key in _NATURAL:
        return KillerType.NATURAL, _NATURAL[key]
    if key in _NPC:
        return KillerType.NPC, _NPC[key]
    if key in _ENTITY:
        return KillerType.ENTITY, _ENTITY[key]
    if key.isdigit():
        # Scientists erscheinen in den Logs nur als Zahl
        return KillerType.NPC, "Scientist"
    if key.endswith("(entity)"):
        # Unbekanntes Objekt: Prefab-Namen ohne Zusatz anzeigen
        return KillerType.ENTITY, raw.strip()[: -len("(entity)")].strip()
    return KillerType.PLAYER, raw.strip()
