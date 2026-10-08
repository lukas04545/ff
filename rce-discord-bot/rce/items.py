"""Häufige Item-Shortnames – nur Vorschläge für die Autovervollständigung.

Andere Shortnames können frei eingegeben werden. Die Console Edition nutzt im
Wesentlichen die Shortnames von Rust PC; ob ein Item auf deinem Server existiert,
lässt sich über RCON nicht abfragen (falsche Namen ignoriert der Server).
"""
from __future__ import annotations

COMMON_ITEMS: dict[str, str] = {
    # Waffen
    "rifle.ak": "AK-47", "rifle.lr300": "LR-300", "rifle.bolt": "Bolt Action Rifle",
    "rifle.semiauto": "Semi-Automatic Rifle", "smg.mp5": "MP5A4", "smg.thompson": "Thompson",
    "smg.2": "Custom SMG", "pistol.semiauto": "Semi-Automatic Pistol", "pistol.revolver": "Revolver",
    "pistol.python": "Python Revolver", "shotgun.pump": "Pump Shotgun", "shotgun.double": "Double Barrel",
    "shotgun.waterpipe": "Waterpipe Shotgun", "bow.hunting": "Jagdbogen", "crossbow": "Armbrust",
    "rocket.launcher": "Raketenwerfer",
    # Munition & Explosives
    "ammo.rifle": "5.56 Munition", "ammo.pistol": "Pistolenmunition", "ammo.shotgun": "12-Gauge Schrot",
    "ammo.rifle.explosive": "Explosive 5.56", "arrow.wooden": "Holzpfeil", "ammo.rocket.basic": "Rakete",
    "explosive.timed": "C4", "explosive.satchel": "Satchel Charge", "grenade.f1": "F1-Granate",
    "grenade.beancan": "Beancan-Granate", "gunpowder": "Schwarzpulver",
    # Rüstung & Kleidung
    "metal.facemask": "Metal Facemask", "metal.plate.torso": "Metal Chestplate",
    "roadsign.kilt": "Road Sign Kilt", "roadsign.jacket": "Road Sign Jacket", "hoodie": "Hoodie",
    "pants": "Hose", "shoes.boots": "Stiefel", "burlap.shirt": "Jute-Hemd",
    # Medizin
    "syringe.medical": "Medizinische Spritze", "largemedkit": "Großes Medkit", "bandage": "Bandage",
    # Werkzeuge
    "stone.pickaxe": "Stein-Spitzhacke", "stonehatchet": "Steinbeil", "pickaxe": "Spitzhacke",
    "hatchet": "Beil", "hammer": "Hammer", "building.planner": "Bauplan", "torch": "Fackel",
    # Ressourcen
    "wood": "Holz", "stones": "Steine", "metal.fragments": "Metallfragmente",
    "metal.refined": "HQ-Metall", "sulfur": "Schwefel", "scrap": "Scrap", "lowgradefuel": "Low Grade Fuel",
    "cloth": "Stoff", "leather": "Leder",
    # Basis
    "cupboard.tool": "Werkzeugschrank", "lock.code": "Codeschloss", "door.hinged.metal": "Metalltür",
    "sleepingbag": "Schlafsack", "box.wooden.large": "Große Holzkiste", "furnace": "Ofen",
}


def suggestions(current: str, limit: int = 25) -> list[tuple[str, str]]:
    current_l = current.lower()
    hits = [(short, label) for short, label in COMMON_ITEMS.items()
            if current_l in short or current_l in label.lower()]
    return hits[:limit]
