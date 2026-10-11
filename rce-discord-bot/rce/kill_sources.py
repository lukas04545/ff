"""Classifies causes of death from the console line "<victim> was killed by <killer>".

On Console Edition kill lines only contain names or prefab identifiers –
NO weapons, distances or player IDs. This list maps known identifiers to
readable names (source: community library rce.js) and can be extended at
any time – keys always lowercase.
"""
from __future__ import annotations

from enum import Enum


class KillerType(str, Enum):
    PLAYER = "player"
    NATURAL = "natural"   # hunger, cold, falling, ...
    ENTITY = "entity"     # traps, turrets, walls, vehicles
    NPC = "npc"           # animals, scientists, heli, Bradley


_NATURAL = {
    "thirst": "Thirst", "hunger": "Hunger", "cold": "Cold", "bleeding": "Bleeding",
    "fall": "Fall", "fall!": "Fall", "drowned": "Drowned", "radiation": "Radiation",
    "pee pee 9000": "Pee Pee 9000",
}

_NPC = {
    "bear": "Bear", "bear (bear)": "Bear", "boar": "Boar", "boar (boar)": "Boar",
    "wolf": "Wolf", "wolf (wolf)": "Wolf",
    "bradleyapc (entity)": "Bradley APC", "patrolhelicopter (entity)": "Patrol Helicopter",
}

_ENTITY = {
    "guntrap.deployed": "Shotgun Trap", "guntrap.deployed (entity)": "Shotgun Trap",
    "autoturret_deployed": "Auto-Turret", "autoturret_deployed (entity)": "Auto-Turret",
    "flameturret.deployed (entity)": "Flame Turret", "flameturret_fireball (entity)": "Flame Turret",
    "sam_site_turret_deployed (entity)": "SAM Site", "sam_static (entity)": "SAM Site",
    "sentry.bandit.static (entity)": "Bandit Sentry", "sentry.scientist.static (entity)": "Scientist Sentry",
    "teslacoil.deployed (entity)": "Tesla Coil", "landmine (entity)": "Landmine",
    "beartrap (entity)": "Bear Trap", "spikes.floor (entity)": "Floor Spikes",
    "barricade.wood": "Wooden Barricade", "barricade.wood (entity)": "Wooden Barricade",
    "barricade.woodwire (entity)": "Wooden Barricade", "barricade.metal (entity)": "Metal Barricade",
    "wall.external.high": "High External Wooden Wall", "wall.external.high (entity)": "High External Wooden Wall",
    "wall.external.high.wood (entity)": "High External Wooden Wall",
    "wall.external.high.stone": "High External Stone Wall",
    "wall.external.high.stone (entity)": "High External Stone Wall",
    "wall.external.high.ice (entity)": "High Ice Wall", "wall.external.high.adobe (entity)": "High External Adobe Wall",
    "gates.external.high.wood": "High External Wooden Gate",
    "gates.external.high.wood (entity)": "High External Wooden Gate",
    "gates.external.high.stone": "High External Stone Gate",
    "gates.external.high.stone (entity)": "High External Stone Gate",
    "gates.external.high.adobe (entity)": "High External Adobe Gate", "icewall (entity)": "Ice Wall",
    "graveyardfence (entity)": "Graveyard Fence", "lock.code (entity)": "Code Lock",
    "campfire (entity)": "Campfire", "fireball (entity)": "Fire", "fireball_small (entity)": "Fire",
    "oilfireballsmall (entity)": "Oil Fire", "napalm (entity)": "Napalm",
    "cargoshipdynamic1 (entity)": "Cargo Ship", "cargoshipdynamic2 (entity)": "Cargo Ship",
    "rocket_crane_lift_trigger (entity)": "Crane Lift", "rowboat (entity)": "Rowboat",
    "hotairballoon (entity)": "Hot Air Balloon", "minicopter.entity (entity)": "Minicopter",
    "attackhelicopter.entity (entity)": "Attack Helicopter",
    "scraptransporthelicopter (entity)": "Scrap Transport Helicopter", "ch47scientists.entity (entity)": "Chinook",
    "submarinesolo.entity (entity)": "Submarine", "submarineduo.entity (entity)": "Submarine",
}
_ENTITY.update({f"cactus-{i} (entity)": "Cactus" for i in range(1, 8)})


def classify(raw: str) -> tuple[KillerType, str]:
    """Return (type, display name) for a name from a kill line."""
    key = raw.strip().lower()
    if key in _NATURAL:
        return KillerType.NATURAL, _NATURAL[key]
    if key in _NPC:
        return KillerType.NPC, _NPC[key]
    if key in _ENTITY:
        return KillerType.ENTITY, _ENTITY[key]
    if key.isdigit():
        # Scientists only show up as a number in the logs
        return KillerType.NPC, "Scientist"
    if key.endswith("(entity)"):
        # Unknown object: show the prefab name without the suffix
        return KillerType.ENTITY, raw.strip()[: -len("(entity)")].strip()
    return KillerType.PLAYER, raw.strip()
