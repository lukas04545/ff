"""Rust Console Edition item catalogue (shortname -> name), grouped like in-game.

Used for the kit builder in the web interface and for autocomplete. Other shortnames can still be
entered freely – whether an item exists can't be queried via RCON (the server ignores wrong names).
Note: a few RCE shortnames contain spaces or underscores (e.g. "lumberjack hoodie", "t1_smg").
"""
from __future__ import annotations

ICONS = {
    "Ammo": "🧨", "Attachments": "🔭", "Weapons": "🔫", "Attire": "🦺", "Horse Gear": "🐎", "Tools": "⛏️",
    "Medical": "💉", "Teas": "🍵", "Food and Plants": "🍖", "Construction Items": "🧱", "Items": "📦",
    "Traps and Miscellaneous": "🪤", "Resources": "🪵", "Components": "⚙️", "Electrical": "⚡",
    "Easter": "🐣", "Halloween": "🎃", "Xmas": "🎄", "Other": "🚗",
}

# "# Category / Group" starts a block, then "shortname = Name" per line
_DATA = """
# Ammo / Shells
ammo.shotgun = 12 Gauge Buckshot
ammo.shotgun.fire = 12 Gauge Incendiary Shell
ammo.shotgun.slug = 12 Gauge Slug
ammo.grenadelauncher.he = 40mm HE Grenade
ammo.grenadelauncher.buckshot = 40mm Shotgun Round
ammo.grenadelauncher.smoke = 40mm Smoke Grenade
ammo.handmade.shell = Handmade Shell
# Ammo / Bullets
ammo.rifle = 5.56 Rifle Ammo
ammo.rifle.explosive = Explosive 5.56 Rifle Ammo
ammo.rifle.hv = HV 5.56 Rifle Ammo
ammo.pistol.hv = HV Pistol Ammo
ammo.rifle.incendiary = Incendiary 5.56 Rifle Ammo
ammo.pistol.fire = Incendiary Pistol Bullet
ammo.pistol = Pistol Bullet
# Ammo / Arrows
arrow.bone = Bone Arrow
arrow.fire = Fire Arrow
arrow.hv = High Velocity Arrow
ammo.nailgun.nails = Nailgun Nails
arrow.wooden = Wooden Arrow
speargun.spear = Speargun Spear
# Ammo / Rocket
ammo.rocket.hv = High Velocity Rocket
ammo.rocket.seeker = Homing Missile
ammo.rocket.fire = Incendiary Rocket
ammo.rocket.mlrs = MLRS Rocket
ammo.rocket.basic = Rocket
ammo.rocket.sam = Sam Ammo
submarine.torpedo.straight = Torpedo
# Attachments / Weapon Attachments
weapon.mod.small.scope = 4x Zoom Scope
weapon.mod.burstmodule = Burst Module
weapon.mod.extendedmags = Extended Magazine
weapon.mod.gascompressionovedrive = Gas Compression Overdrive
weapon.mod.holosight = Holosight
weapon.mod.muzzleboost = Muzzle Boost
weapon.mod.muzzlebrake = Muzzle Brake
weapon.mod.oilfiltersilencer = Oil Filter Silencer
weapon.mod.simplesight = Simple Handmade Sight
weapon.mod.silencer = Silencer
weapon.mod.sodacansilencer = Soda Can Silencer
weapon.mod.flashlight = Weapon Flashlight
weapon.mod.lasersight = Weapon Lasersight
# Weapons / Guns
rifle.ak = Assault Rifle
rifle.bolt = Bolt Action Rifle
smg.2 = Custom SMG
shotgun.double = Double Barrel Shotgun
pistol.eoka = Eoka Pistol
t1_smg = Handmade SMG
revolver.hc = High Caliber Revolver
hmlmg = HMLMG
homingmissile.launcher = Homing Missile Launcher
rifle.l96 = L96 Rifle
rifle.lr300 = LR-300 Assault Rifle
lmg.m249 = M249
rifle.m39 = M39 Rifle
shotgun.m4 = M4 Shotgun
pistol.m92 = M92 Pistol
minigun = Minigun
smg.mp5 = MP5A4
multiplegrenadelauncher = Multiple Grenade Launcher
pistol.nailgun = Nailgun
pistol.prototype17 = Prototype 17
shotgun.pump = Pump Shotgun
pistol.python = Python Revolver
pistol.revolver = Revolver
rocket.launcher = Rocket Launcher
pistol.semiauto = Semi-Automatic Pistol
rifle.semiauto = Semi-Automatic Rifle
rifle.sks = SKS
shotgun.spas12 = Spas-12 Shotgun
smg.thompson = Thompson
shotgun.waterpipe = Waterpipe Shotgun
# Weapons / Bows
bow.compound = Compound Bow
crossbow = Crossbow
bow.hunting = Hunting Bow
# Weapons / Melee & Throwables
grenade.beancan = Beancan Grenade
bone.club = Bone Club
knife.bone = Bone Knife
knife.combat = Combat Knife
grenade.f1 = F1 Grenade
flamethrower = Flame Thrower
grenade.flashbang = Flashbang
longsword = Longsword
mace = Mace
machete = Machete
military flamethrower = Military Flamethrower
grenade.molotov = Molotov Cocktail
paddle = Paddle
salvaged.cleaver = Salvaged Cleaver
salvaged.sword = Salvaged Sword
speargun = Speargun
spear.stone = Stone Spear
spear.wooden = Wooden Spear
# Attire / Head
mask.bandana = Bandana Mask
hat.cap = Baseball Cap
hat.beenie = Beenie Hat
deer.skull.mask = Bone Helmet
hat.boonie = Boonie Hat
bucket.helmet = Bucket Helmet
hat.candle = Candle Hat
coffeecan.helmet = Coffee Can Helmet
diving.mask = Diving Mask
heavy.plate.helmet = Heavy Plate Helmet
mask.balaclava = Improvised Balaclava
metal.facemask = Metal Facemask
hat.miner = Miners Hat
partyhat = Party Hat
riot.helmet = Riot Helmet
hat.wolf = Wolf Headdress
wood.armor.helmet = Wood Armor Helmet
# Attire / Chest
burlap.shirt = Burlap Shirt
heavy.plate.jacket = Heavy Plate Jacket
attire.hide.poncho = Hide Poncho
hoodie = Hoodie
jacket = Jacket
tshirt.long = Longsleeve T-Shirt
lumberjack hoodie = Lumberjack Hoodie
metal.plate.torso = Metal Chest Plate
roadsign.jacket = Road Sign Jacket
shirt.collared = Shirt
jacket.snow = Snow Jacket
shirt.tanktop = Tank Top
tshirt = T-Shirt
wood.armor.jacket = Wood Chestplate
# Attire / Hands
burlap.gloves.new = Burlap Gloves
burlap.gloves = Leather Gloves
roadsign.gloves = Roadsign Gloves
tactical.gloves = Tactical Gloves
# Attire / Legs
heavy.plate.pants = Heavy Plate Pants
attire.hide.pants = Hide Pants
pants = Pants
roadsign.kilt = Road Sign Kilt
pants.shorts = Shorts
wood.armor.pants = Wood Armor Pants
# Attire / Feet
shoes.boots = Boots
burlap.shoes = Burlap Shoes
diving.fins = Diving Fins
boots.frog = Frog Boots
attire.hide.boots = Hide Boots
# Attire / Full Body
bone.armor.suit = Bone Armor
hazmatsuit = Hazmat Suit
largebackpack = Large Backpack
smallbackpack = Small Backpack
jumpsuit.waterwellnpc = Waterwell NPC Suit
diving.wetsuit = Wetsuit
scientistsuit_heavy = Heavy Scientist Suit
hazmatsuit_scientist_peacekeeper = Peacekeeper Hazmat Suit
hazmatsuit_scientist = Scientist Hazmat Suit
# Attire / Misc
attire.banditguard = Bandit Guard
barrelcostume = Barrel Costume
burlap.headwrap = Burlap Headwrap
burlap.trousers = Burlap Trousers
cratecostume = Crate Costume
diving.tank = Diving Tank
attire.hide.helterneck = Hide Halterneck
attire.hide.skirt = Hide Skirt
attire.hide.vest = Hide Vest
nightvisiongoggles = Night Vision Goggles
parachute = Parachute
prisonerhood = Prisoner Hood
# Horse Gear / Saddles
horse.saddle.double = Double Horse Saddle
horse.saddle = Horse Saddle
# Horse Gear / Armor
hab.armor = Hot Air Balloon Armor
horse.armor.roadsign = Roadsign Horse Armor
horse.armor.wood = Wooden Horse Armor
# Horse Gear / Shoes
horse.shoes.basic = Basic Horse Shoes
horse.shoes.advanced = High Quality Horse Shoes
# Horse Gear / Storage
horse.saddlebag = Saddle Bag
# Tools / Tools
tool.binoculars = Binoculars
cakefiveyear = Birthday Cake
chainsaw = Chainsaw
flare = Flare
flashlight.held = Flashlight
hammer = Hammer
handcuffs = Handcuffs
fishingrod.handmade = Handmade Fishing Rod
hatchet = Hatchet
jackhammer = Jackhammer
metal.detector = Metal Detector
pickaxe = Pickaxe
pipetool = Pipe Tool
rf.detonator = RF Transmitter
rock = Rock
axe.salvaged = Salvaged Axe
hammer.salvaged = Salvaged Hammer
icepick.salvaged = Salvaged Icepick
explosive.satchel = Satchel Charge
grenade.smoke = Smoke Grenade
spraycan = Spray Can
stonehatchet = Stone Hatchet
stone.pickaxe = Stone Pickaxe
supply.signal = Supply Signal
explosive.timed = Timed Explosive Charge
torch = Torch
wallpaper.tool = Wallpaper Tool
bucket.water = Water Bucket
# Medical / Medical
antiradpills = Anti-Radiation Pills
bandage = Bandage
largemedkit = Large Medkit
syringe.medical = Medical Syringe
# Teas / Teas
radiationresisttea.advanced = Advanced Anti-Rad Tea
healingtea.advanced = Advanced Healing Tea
maxhealthtea.advanced = Advanced Max Health Tea
oretea.advanced = Advanced Ore Tea
radiationremovetea.advanced = Advanced Rad. Removal Tea
scraptea.advanced = Advanced Scrap Tea
woodtea.advanced = Advanced Wood Tea
radiationresisttea = Anti-Rad Tea
healingtea = Basic Healing Tea
maxhealthtea = Basic Max Health Tea
oretea = Basic Ore Tea
scraptea = Basic Scrap Tea
woodtea = Basic Wood Tea
radiationresisttea.pure = Pure Anti-Rad Tea
healingtea.pure = Pure Healing Tea
maxhealthtea.pure = Pure Max Health Tea
oretea.pure = Pure Ore Tea
radiationremovetea.pure = Pure Rad. Removal Tea
scraptea.pure = Pure Scrap Tea
woodtea.pure = Pure Wood Tea
radiationremovetea = Rad. Removal Tea
# Food and Plants / Food
fish.anchovy = Anchovy
apple = Apple
black.raspberries = Black Raspberries
blueberries = Blueberries
blue.berry = Blue Berry
bearmeat.burned = Burnt Bear Meat
chicken.burned = Burnt Chicken
deermeat.burned = Burnt Deer Meat
horsemeat.burned = Burnt Horse Meat
humanmeat.burned = Burnt Human Meat
meat.pork.burned = Burnt Pork
wolfmeat.burned = Burnt Wolf Meat
cactusflesh = Cactus Flesh
can.beans = Can of Beans
can.tuna = Can of Tuna
fish.catfish = Catfish
chocholate = Chocolate Bar
bearmeat.cooked = Cooked Bear Meat
chicken.cooked = Cooked Chicken
deermeat.cooked = Cooked Deer Meat
fish.cooked = Cooked Fish
horsemeat.cooked = Cooked Horse Meat
humanmeat.cooked = Cooked Human Meat
meat.pork.cooked = Cooked Pork
wolfmeat.cooked = Cooked Wolf Meat
corn = Corn
granolabar = Granola Bar
green.berry = Green Berry
grub = Grub
fish.herring = Herring
fish.minnows = Minnows
mushroom = Mushroom
fish.orangeroughy = Orange Roughy
jar.pickle = Pickles
potato = Potato
pumpkin = Pumpkin
bearmeat = Raw Bear Meat
chicken.raw = Raw Chicken Breast
deermeat.raw = Raw Deer Meat
fish.raw = Raw Fish
horsemeat.raw = Raw Horse Meat
humanmeat.raw = Raw Human Meat
meat.boar = Raw Pork
wolfmeat.raw = Raw Wolf Meat
red.berry = Red Berry
fish.salmon = Salmon
fish.sardine = Sardine
fish.smallshark = Small Shark
fish.troutsmall = Small Trout
white.berry = White Berry
worm = Worm
yellow.berry = Yellow Berry
fish.yellowperch = Yellow Perch
# Food and Plants / Drinks
smallwaterbottle = Small Water Bottle
waterjug = Water Jug
# Food and Plants / Plants
clone.blue.berry = Blue Berry Clone
seed.blue.berry = Blue Berry Seed
clone.corn = Corn Clone
seed.corn = Corn Seed
clone.green.berry = Green Berry Clone
seed.green.berry = Green Berry Seed
clone.hemp = Hemp Clone
seed.hemp = Hemp Seed
clone.potato = Potato Clone
seed.potato = Potato Seed
clone.pumpkin = Pumpkin Plant Clone
seed.pumpkin = Pumpkin Seed
clone.red.berry = Red Berry Clone
seed.red.berry = Red Berry Seed
clone.white.berry = White Berry Clone
seed.white.berry = White Berry Seed
clone.yellow.berry = Yellow Berry Clone
seed.yellow.berry = Yellow Berry Seed
# Construction Items / Construction
wall.frame.fence = Chainlink Fence
floor.grill = Floor Grill
floor.triangle.grill = Floor Triangle Grill
wall.external.high.stone = High External Stone Wall
wall.external.high = High External Wooden Wall
floor.ladder.hatch = Ladder Hatch
wall.frame.shopfront.metal = Metal Shop Front
wall.window.bars.metal = Metal Window Bars
wall.frame.netting = Netting
wall.frame.cell = Prison Cell Wall
wall.window.bars.toptier = Reinforced Glass Window
wall.frame.shopfront = Shop Front
wall.window.glass.reinforced = Strengthened Glass Window
floor.triangle.ladder.hatch = Triangle Ladder Hatch
ladder.wooden.wall = Wooden Ladder
wall.window.bars.wood = Wooden Window Bars
# Construction Items / Doors
door.hinged.toptier = Armored Door
door.double.hinged.toptier = Armored Double Door
wall.frame.fence.gate = Chainlink Fence Gate
lock.code = Code Lock
wall.frame.garagedoor = Garage Door
gates.external.high.stone = High External Stone Gate
gates.external.high.wood = High External Wooden Gate
lock.key = Key Lock
wall.frame.cell.gate = Prison Cell Gate
door.hinged.metal = Sheet Metal Door
door.double.hinged.metal = Sheet Metal Double Door
door.double.hinged.wood = Wood Double Door
door.hinged.wood = Wooden Door
# Construction Items / Building
barricade.woodwire = Barbed Wooden Barricade
building.planner = Building Plan
barricade.concrete = Concrete Barricade
water.catcher.large = Large Water Catcher
barricade.metal = Metal Barricade
shutter.metal.embrasure.a = Metal Horizontal Embrasure
shutter.metal.embrasure.b = Metal Vertical Embrasure
barricade.sandbags = Sandbag Barricade
water.catcher.small = Small Water Catcher
barricade.stone = Stone Barricade
cupboard.tool = Tool Cupboard
watchtower.wood = Watch Tower
shutter.wood.a = Wood Shutters
barricade.wood = Wooden Barricade
barricade.wood.cover = Wooden Barricade Cover
# Items / Storage
dropbox = Drop Box
box.wooden.large = Large Wood Box
locker = Locker
mailbox = Mail Box
box.repair.bench = Repair Bench
vending.machine = Vending Machine
box.wooden = Wood Storage Box
# Items / Furniture
chair = Chair
mixingtable = Mixing Table
research.table = Research Table
rug.bear = Rug Bear Skin
rug = Rug
table = Table
# Items / Utility
furnace = Furnace
furnace.large = Large Furnace
small.oil.refinery = Small Oil Refinery
workbench1 = Workbench Level 1
workbench2 = Workbench Level 2
workbench3 = Workbench Level 3
# Items / Misc
bbq = Barbeque
bed = Bed
botabag = Bota Bag
campfire = Camp Fire
fridge = Fridge
hitchtroughcombo = Hitch & Trough
gunrack.horizontal = Horizontal Weapon Rack
kayak = Kayak
lantern = Lantern
planter.large = Large Planter Box
shelves = Salvaged Shelves
skull_fire_pit = Skull Fire Pit
sleepingbag = Sleeping Bag
planter.small = Small Planter Box
stash.small = Small Stash
fireplace.stone = Stone Fireplace
fishtrap.small = Survival Fish Trap
gunrack_tall.horizontal = Tall Weapon Rack
planter.triangle = Triangle Planter Box
tunalight = Tuna Can Lamp
composter = Composter
water.barrel = Water Barrel
water.purifier = Water Purifier
gunrack_stand = Weapon Rack Stand
gunrack_wide.horizontal = Wide Weapon Rack
vehicle.1mod.flatbed = Flatbed Vehicle Module
vehicle.2mod.flatbed = Large Flatbed Vehicle Module
# Traps and Miscellaneous / Traps
flameturret = Flame Turret
trap.landmine = Landmine
guntrap = Shotgun Trap
trap.bear = Snap Trap
# Traps and Miscellaneous / Misc
samsite = Sam Site
spikes.floor = Wooden Floor Spikes
keycard_blue = Blue Keycard
door.key = Door Key
keycard_green = Green Keycard
fun.guitar = Guitar
note = Note
keycard_red = Red Keycard
# Resources / Materials
fat.animal = Animal Fat
bone.fragments = Bone Fragments
cctv.camera = CCTV Camera
charcoal = Charcoal
cloth = Cloth
crude.oil = Crude Oil
diesel_barrel = Diesel Fuel
can.beans.empty = Empty Can Of Beans
can.tuna.empty = Empty Tuna Can
explosives = Explosives
fertilizer = Fertilizer
gunpowder = Gun Powder
metal.refined = High Quality Metal
horsedung = Horse Dung
skull.human = Human Skull
leather = Leather
metal.fragments = Metal Fragments
plantfiber = Plant Fiber
scrap = Scrap
stones = Stones
sulfur = Sulfur
targeting.computer = Targeting Computer
skull.wolf = Wolf Skull
wood = Wood
# Resources / Ore
hq.metal.ore = High Quality Metal Ore
metal.ore = Metal Ore
sulfur.ore = Sulfur Ore
# Resources / Fuel
lowgradefuel = Low Grade Fuel
# Components / Components
fuse = Electric Fuse
propanetank = Empty Propane Tank
gears = Gears
metalblade = Metal Blade
metalpipe = Metal Pipe
metalspring = Metal Spring
aiming.module.mlrs = MLRS Aiming Module
riflebody = Rifle Body
roadsigns = Road Signs
rope = Rope
semibody = Semi Automatic Body
sewingkit = Sewing Kit
sheetmetal = Sheet Metal
smgbody = SMG Body
tarp = Tarp
techparts = Tech Trash
# Electrical / Electrical
electric.andswitch = AND Switch
electric.audioalarm = Audio Alarm
autoturret = Auto Turret
electric.blocker = Blocker
electric.button = Button
ceilinglight = Ceiling Light
computerstation = Computer Station
electric.counter = Counter
electric.doorcontroller = Door Controller
drone = Drone
electrical.branch = Electrical Branch
electric.furnace = Electric Furnace
electric.heater = Electric Heater
elevator = Elevator
electric.flasherlight = Flasher Light
electric.hbhfsensor = HBHF Sensor
hosetool = Hose Tool
electric.igniter = Igniter
electric.battery.rechargable.large = Large Rechargeable Battery
electric.solarpanel.large = Large Solar Panel
electric.laserdetector = Laser Detector
electric.battery.rechargable.medium = Medium Rechargeable Battery
electrical.memorycell = Memory Cell
modularcarlift = Modular Car Lift
electric.orswitch = OR Switch
powered.water.purifier = Powered Water Purifier
electric.pressurepad = Pressure Pad
electric.random.switch = RAND Switch
target.reactive = Reactive Target
electric.rf.broadcaster = RF Broadcaster
rf_pager = RF Pager
electric.rf.receiver = RF Receiver
electrical.combiner = Root Combiner
searchlight = Search Light
electric.seismicsensor = Seismic Sensor
electric.sirenlight = Siren Light
electric.battery.rechargable.small = Small Rechargeable Battery
electric.fuelgenerator.small = Small Generator
electric.splitter = Splitter
electric.sprinkler = Sprinkler
storageadaptor = Storage Adapter
storage.monitor = Storage Monitor
electric.switch = Switch
electric.teslacoil = Tesla Coil
electric.generator.small = Test Generator
electric.timer = Timer
waterpump = Water Pump
generator.wind.scrap = Wind Turbine
wiretool = Wire Tool
electric.xorswitch = XOR Switch
# Electrical / Industrial
fluid.combiner = Fluid Combiner
fluid.splitter = Fluid Splitter
fluid.switch = Fluid Switch & Pump
industrial.combiner = Industrial Combiner
industrial.conveyor = Industrial Conveyor
industrial.crafter = Industrial Crafter
industrial.splitter = Industrial Splitter
# Easter / Easter
easter.bronzeegg = Bronze Egg
attire.bunnyears = Bunny Ears
attire.bunny.onesie = Bunny Onesie
easterbasket = Egg Basket
easter.goldegg = Gold Egg
easter.paintedeggs = Painted Egg
easter.silveregg = Silver Egg
# Halloween / Halloween
knife.butcher = Butcher Knife
coffin.storage = Coffin
gravestone = Gravestone
wall.graveyard.fence = Graveyard Fence
halloween.candy = Halloween Candy
halloween.lootbag.large = Large Loot Bag
halloween.lootbag.medium = Medium Loot Bag
halloween.mummysuit = Mummy Suit
pitchfork = Pitchfork
pumpkinbasket = Pumpkin Bucket
scarecrow = Scarecrow
scarecrow.suit = Scarecrow Suit
scarecrowhead = Scarecrow Wrap
sickle = Sickle
halloween.lootbag.small = Small Loot Bag
spiderweb = Spider Webs
woodcross = Wooden Cross
# Xmas / Christmas
candycane = Candy Cane
candycaneclub = Candy Cane Club
coal = Coal
gingerbreadsuit = Gingerbread Suit
wall.external.high.ice = High Ice Wall
snowball = Snowball
snowballgun = Snowball Gun
# Xmas / Decorations
xmas.lightstring = Christmas Lights
xmas.tree = Christmas Tree
xmas.decoration.baubels = Decorative Baubles
xmas.decoration.gingerbreadmen = Decorative Gingerbread Men
xmas.decoration.pinecone = Decorative Pinecones
xmas.decoration.candycanes = Decorative Plastic Candy Canes
xmas.decoration.tinsel = Decorative Tinsel
xmas.decoration.star = Star Tree Topper
xmas.decoration.lights = Tree Lights
# Xmas / Presents
xmas.present.large = Large Present
stocking.large = Large Stocking
xmas.present.medium = Medium Present
xmas.present.small = Small Present
stocking.small = Small Stocking
# Other / Vehicle Modules
vehicle.1mod.cockpit.armored = Armored Cockpit Vehicle Module
vehicle.1mod.passengers.armored = Armored Passenger Vehicle Module
vehicle.2mod.fuel.tank = Fuel Tank Vehicle Module
vehicle.2mod.camper = Camper Vehicle Module
vehicle.1mod.cockpit = Cockpit Vehicle Module
vehicle.1mod.cockpit.with.engine = Cockpit With Engine Vehicle Module
vehicle.1mod.engine = Engine Vehicle Module
vehicle.2mod.passengers = Passenger Vehicle Module
vehicle.1mod.rear.seats = Rear Seats Vehicle Module
vehicle.1mod.storage = Storage Vehicle Module
vehicle.1mod.taxi = Taxi Vehicle Module
# Other / Vehicle Parts
carburetor1 = Low Quality Carburetor
carburetor2 = Medium Quality Carburetor
carburetor3 = High Quality Carburetor
crankshaft1 = Low Quality Crankshaft
crankshaft2 = Medium Quality Crankshaft
crankshaft3 = High Quality Crankshaft
piston1 = Low Quality Pistons
piston2 = Medium Quality Pistons
piston3 = High Quality Pistons
sparkplug1 = Low Quality Spark Plugs
sparkplug2 = Medium Quality Spark Plugs
sparkplug3 = High Quality Spark Plugs
valve1 = Low Quality Valves
valve2 = Medium Quality Valves
valve3 = High Quality Valves
"""


def _parse() -> tuple[dict[tuple[str, str], dict[str, str]], dict[str, str]]:
    catalogue: dict[tuple[str, str], dict[str, str]] = {}
    groups: dict[str, str] = {}
    category = group = ""
    for line in _DATA.strip().splitlines():
        if line.startswith("# "):
            category, group = (part.strip() for part in line[2:].split("/", 1))
            continue
        short, name = (part.strip() for part in line.split(" = ", 1))
        catalogue.setdefault((category, ICONS.get(category, "📦")), {})[short] = name
        groups[short] = group
    return catalogue, groups


# (category, icon) -> {shortname: name};  shortname -> group within the category
CATALOGUE, GROUPS = _parse()

# All catalogue items flat: shortname -> name
COMMON_ITEMS: dict[str, str] = {short: name for items in CATALOGUE.values() for short, name in items.items()}


def normalize_shortname(value: str) -> str:
    """User input -> shortname: lowercase; spaces removed unless the item really has one (e.g. "lumberjack hoodie")."""
    short = " ".join(value.replace('"', "").split()).lower()
    return short if short in COMMON_ITEMS else short.replace(" ", "")


def suggestions(current: str, limit: int = 25) -> list[tuple[str, str]]:
    current_l = current.lower()
    hits = [(short, label) for short, label in COMMON_ITEMS.items()
            if current_l in short or current_l in label.lower()]
    return hits[:limit]
