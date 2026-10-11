"""Common item shortnames – only suggestions for autocomplete.

Other shortnames can be entered freely. Console Edition mostly uses the
Rust PC shortnames; whether an item exists on your server can't be queried
via RCON (the server ignores wrong names).
"""
from __future__ import annotations

COMMON_ITEMS: dict[str, str] = {
    # Weapons
    "rifle.ak": "AK-47", "rifle.lr300": "LR-300", "rifle.bolt": "Bolt Action Rifle",
    "rifle.semiauto": "Semi-Automatic Rifle", "smg.mp5": "MP5A4", "smg.thompson": "Thompson",
    "smg.2": "Custom SMG", "pistol.semiauto": "Semi-Automatic Pistol", "pistol.revolver": "Revolver",
    "pistol.python": "Python Revolver", "shotgun.pump": "Pump Shotgun", "shotgun.double": "Double Barrel",
    "shotgun.waterpipe": "Waterpipe Shotgun", "bow.hunting": "Hunting Bow", "crossbow": "Crossbow",
    "rocket.launcher": "Rocket Launcher",
    # Ammo & explosives
    "ammo.rifle": "5.56 Rifle Ammo", "ammo.pistol": "Pistol Bullet", "ammo.shotgun": "12 Gauge Buckshot",
    "ammo.rifle.explosive": "Explosive 5.56", "arrow.wooden": "Wooden Arrow", "ammo.rocket.basic": "Rocket",
    "explosive.timed": "C4", "explosive.satchel": "Satchel Charge", "grenade.f1": "F1 Grenade",
    "grenade.beancan": "Beancan Grenade", "gunpowder": "Gun Powder",
    # Armor & clothing
    "metal.facemask": "Metal Facemask", "metal.plate.torso": "Metal Chestplate",
    "roadsign.kilt": "Road Sign Kilt", "roadsign.jacket": "Road Sign Jacket", "hoodie": "Hoodie",
    "pants": "Pants", "shoes.boots": "Boots", "burlap.shirt": "Burlap Shirt",
    # Medical
    "syringe.medical": "Medical Syringe", "largemedkit": "Large Medkit", "bandage": "Bandage",
    # Tools
    "stone.pickaxe": "Stone Pickaxe", "stonehatchet": "Stone Hatchet", "pickaxe": "Pickaxe",
    "hatchet": "Hatchet", "hammer": "Hammer", "building.planner": "Building Plan", "torch": "Torch",
    # Resources
    "wood": "Wood", "stones": "Stones", "metal.fragments": "Metal Fragments",
    "metal.refined": "High Quality Metal", "sulfur": "Sulfur", "scrap": "Scrap", "lowgradefuel": "Low Grade Fuel",
    "cloth": "Cloth", "leather": "Leather",
    # Base
    "cupboard.tool": "Tool Cupboard", "lock.code": "Code Lock", "door.hinged.metal": "Sheet Metal Door",
    "sleepingbag": "Sleeping Bag", "box.wooden.large": "Large Wood Box", "furnace": "Furnace",
}


def suggestions(current: str, limit: int = 25) -> list[tuple[str, str]]:
    current_l = current.lower()
    hits = [(short, label) for short, label in COMMON_ITEMS.items()
            if current_l in short or current_l in label.lower()]
    return hits[:limit]
