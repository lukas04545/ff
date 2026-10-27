"""Battle pass: a season of tiers on the player website (/battlepass) with a free and a premium reward track.

Players level up with XP from time online, PvP kills and a daily bonus (linked players – /customkit link – and the
website's player login). The premium track is bought with the premium currency (default "Coins", 1100) and pays out
1200 of it over the season – enough to buy the next pass. Coins come from coin packs in the web store (PayPal / gift
cards), from the battle pass itself and from admins (/coins give).

Rewards (one line per tier: "tier | free reward | premium reward"):
    points 500            points (points & shop must be on)
    coins 100             premium currency
    item rifle.ak 1       an in-game item (given right away – the player has to be online to claim it)
    -                     nothing
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only, sanitize_player_name

COIN_TIERS = (8, 16, 25, 33, 41, 50, 58, 66, 75, 83, 91, 100)   # 12 × 100 coins = 1200 on the premium row
_FREE = ("points", "item wood 1500", "item stones 1500", "-", "item metal.fragments 500", "points",
         "item cloth 100", "-", "item lowgradefuel 100", "item scrap 50")
_PREMIUM = ("item metal.refined 25", "item ammo.rifle 64", "item syringe.medical 3", "item scrap 150",
            "item ammo.pistol 64", "item largemedkit 2", "item metal.fragments 1500", "item wood 3000",
            "item stones 3000", "item lowgradefuel 300", "item ammo.shotgun 32", "item sulfur 1000")
_MILESTONES = {5: "item crossbow 1", 10: "item pistol.revolver 1", 15: "item coffeecan.helmet 1",
               20: "item pistol.semiauto 1", 30: "item shotgun.pump 1", 35: "item roadsign.jacket 1",
               40: "item rifle.semiauto 1", 45: "item smg.thompson 1", 55: "item pistol.python 1",
               60: "item smg.mp5 1", 65: "item metal.facemask 1", 70: "item rifle.m39 1",
               80: "item rifle.lr300 1", 85: "item metal.plate.torso 1", 90: "item rifle.bolt 1",
               95: "item hazmatsuit 1", 99: "item rifle.ak 1"}


def _default_tiers() -> str:
    """100 tiers: free row with points (growing) and resources, a 1000 points bonus every 25 tiers; premium row with
    better loot, a weapon or armour piece every few tiers and 100 coins on 12 tiers (1200 in total)."""
    lines = []
    for tier in range(1, 101):
        free = _FREE[(tier - 1) % len(_FREE)]
        if free == "points":
            free = f"points {100 + tier * 5}"
        if tier % 25 == 0:
            free = "points 1000"
        premium = "coins 100" if tier in COIN_TIERS else \
            _MILESTONES.get(tier) or _PREMIUM[(tier - 1) % len(_PREMIUM)]
        lines.append(f"{tier} | {free} | {premium}")
    return "\n".join(lines)


DEFAULT_TIERS = _default_tiers()
DEFAULT_PACKS = "500 | 4.99\n1100 | 9.99\n2400 | 19.99\n6000 | 44.99"
LINE = re.compile(r"^\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*$")
PACK = re.compile(r"^\s*(\d+)\s*\|\s*(\d+(?:[.,]\d{1,2})?)\s*$")
SHORTNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PassError(Exception):
    """Shown to the player."""


@dataclass(frozen=True)
class Reward:
    kind: str          # points, coins, item
    amount: int
    item: str = ""

    def label(self, coin_name: str, points_name: str) -> str:
        if self.kind == "item":
            from rce import items as itemlist
            return f"{itemlist.COMMON_ITEMS.get(self.item, self.item)} ×{self.amount}"
        return f"{self.amount:,} {coin_name if self.kind == 'coins' else points_name}".replace(",", " ")


def parse_reward(text: str) -> Reward | None:
    """'points 500' / 'coins 100' / 'item rifle.ak 1' / '-' -> Reward (None = nothing). ValueError if wrong."""
    words = text.split()
    if not words or words == ["-"]:
        return None
    if words[0] in ("points", "coins") and len(words) == 2 and words[1].isdigit() and int(words[1]) > 0:
        return Reward(words[0], int(words[1]))
    if words[0] == "item" and len(words) == 3 and SHORTNAME.match(words[1]) and words[2].isdigit() \
            and int(words[2]) > 0:
        return Reward("item", int(words[2]), words[1])
    raise ValueError(f'"{text}" – use points N, coins N, item <shortname> N or -')


def parse_tiers(text: str) -> tuple[dict[int, tuple[Reward | None, Reward | None]], list[str]]:
    """{tier: (free, premium)} and a list of problems."""
    tiers, problems = {}, []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip():
            continue
        m = LINE.match(line)
        if m is None:
            problems.append(f"Tiers, line {n}: use tier | free reward | premium reward")
            continue
        try:
            tiers[int(m[1])] = (parse_reward(m[2]), parse_reward(m[3]))
        except ValueError as exc:
            problems.append(f"Tiers, line {n}: {exc}")
    return dict(sorted(tiers.items())), problems


def parse_packs(text: str) -> list[tuple[int, float]]:
    packs = []
    for line in str(text or "").splitlines():
        m = PACK.match(line)
        if m and int(m[1]) > 0:
            packs.append((int(m[1]), round(float(m[2].replace(",", ".")), 2)))
    return sorted(packs)


class BattlePass(Plugin):
    NAME = "Battle pass"
    ICON = "🎖️"
    DESCRIPTION = ("A season battle pass on the player website: free and premium rewards per tier, XP from time "
                   "online, PvP kills and a daily bonus. The premium pass costs 1100 of a premium currency (Coins) "
                   "and pays out 1200 over the season. Coins: packs in the web store, the pass itself, admins.")
    SETTINGS = (
        Setting("season", "Season number", "int", 1, min=1, max=10000,
                help="Raise it to start a new season – everyone starts again at tier 0 and buys the new pass."),
        Setting("season_name", "Season name", "text", "Season 1"),
        Setting("season_end", "Season ends (YYYY-MM-DD)", "text", "", help="Shown as a countdown. Optional."),
        Setting("currency_name", "Premium currency name", "text", "Coins"),
        Setting("currency_icon", "Premium currency icon", "text", "💎"),
        Setting("pass_price", "Premium pass price (premium currency)", "int", 1100, min=0, max=10_000_000),
        Setting("pass_money_price", "Premium pass price in the web store (money)", "text", "",
                help="Also sell the premium pass directly for real money (PayPal / gift card), e.g. 9.99 in the store "
                     "currency – next to buying it with the premium currency. Empty = only with the premium "
                     "currency."),
        Setting("xp_per_tier", "XP per tier", "int", 500, min=10, max=10_000_000,
                help="100 tiers × 500 XP ≈ 80 hours online over the season (kills and the daily bonus make it less)."),
        Setting("xp_per_minute", "XP per minute online", "int", 10, min=0, max=100_000),
        Setting("xp_per_kill", "XP per PvP kill", "int", 50, min=0, max=100_000),
        Setting("daily_xp", "Daily bonus XP", "int", 300, min=0, max=100_000,
                help="Claimed once a day (UTC) on the battle pass page."),
        Setting("tiers", "Tiers and rewards", "textarea", DEFAULT_TIERS,
                help="One tier per line (default: 100 tiers): tier | free reward | premium reward. Rewards: "
                     "points N, coins N, item <shortname> N, or -."),
        Setting("coin_packs", "Coin packs in the web store", "textarea", DEFAULT_PACKS,
                help="One per line: amount | price (in the store currency, paid with PayPal or a gift card). "
                     "Empty = coins can't be bought."),
    )

    coins_group = app_commands.Group(name="coins", description="Premium currency (battle pass)", guild_only=True)

    # ------------------------------------------------------------ Setup

    @property
    def coin_name(self) -> str:
        return str(self.setting("currency_name") or "Coins").strip() or "Coins"

    @property
    def coin_icon(self) -> str:
        return str(self.setting("currency_icon") or "💎").strip()[:4] or "💎"

    def fmt_coins(self, amount: int) -> str:
        return f"{self.coin_icon} {amount:,} {self.coin_name}".replace(",", " ")

    def tiers(self) -> dict[int, tuple[Reward | None, Reward | None]]:
        return parse_tiers(self.setting("tiers"))[0]

    def packs(self) -> list[tuple[int, float]]:
        return parse_packs(self.setting("coin_packs"))

    def premium_coins(self) -> int:
        return sum(p.amount for _, p in self.tiers().values() if p is not None and p.kind == "coins")

    def problems(self) -> list[str]:
        out = parse_tiers(self.setting("tiers"))[1]
        if not self.tiers():
            out.append("No tiers set up.")
        accounts = self.bot.get_cog("PlayerAccounts")
        if accounts is None or not accounts.enabled:
            out.append("Player login (PLAYER_ACCOUNTS) is off – players log in on the website to use the pass.")
        if self.plugin("website") is None:
            out.append("Switch on the 🌍 Player website – the battle pass is a page of it.")
        return out

    def status(self) -> dict:
        rows = self.data.items(f"p:{self.season}:")
        return {"Season": self.setting("season_name"), "Players": len(rows),
                "Premium passes": sum(1 for r in rows.values() if r.get("premium")),
                f"{self.coin_name} on the premium track": self.premium_coins()}

    @property
    def season(self) -> int:
        return int(self.setting("season"))

    def season_end(self) -> int | None:
        text = str(self.setting("season_end") or "").strip()
        try:
            return int(time.mktime(time.strptime(text, "%Y-%m-%d"))) + 86400 if text else None
        except ValueError:
            return None

    # ------------------------------------------------------------ Progress (self.data, per season)

    def progress(self, user_id: int) -> dict:
        p = self.data.get(f"p:{self.season}:{user_id}") or {}
        return {"xp": int(p.get("xp", 0)), "premium": bool(p.get("premium")), "free": list(p.get("free", [])),
                "paid": list(p.get("paid", [])), "daily": p.get("daily", "")}

    def _save(self, user_id: int, p: dict) -> None:
        self.data.set(f"p:{self.season}:{user_id}", p)

    def level(self, xp: int) -> int:
        top = max(self.tiers() or {0: None})
        return min(top, xp // max(1, int(self.setting("xp_per_tier"))))

    def add_xp(self, user_id: int, amount: int) -> None:
        if amount <= 0:
            return
        p = self.progress(user_id)
        p["xp"] += amount
        self._save(user_id, p)

    # ------------------------------------------------------------ XP sources

    @tasks.loop(minutes=1)
    async def online_xp(self) -> None:
        per = int(self.setting("xp_per_minute"))
        if per <= 0:
            return
        for name in list(self.bot.online_players):
            user_id = self.linked_user(name)
            if user_id is not None:
                self.add_xp(user_id, per)

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        if not getattr(ev, "is_pvp", False) or ev.killer == ev.victim:
            return
        user_id = self.linked_user(ev.killer)
        if user_id is not None:
            self.add_xp(user_id, int(self.setting("xp_per_kill")))

    def claim_daily(self, user_id: int) -> int:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        p = self.progress(user_id)
        if p["daily"] == today:
            raise PassError("You already got today's bonus – come back tomorrow.")
        amount = int(self.setting("daily_xp"))
        p["daily"], p["xp"] = today, p["xp"] + amount
        self._save(user_id, p)
        return amount

    # ------------------------------------------------------------ Buying and claiming

    def money_price(self) -> float:
        """The premium pass's price in the web store (0 = not for sale for money)."""
        try:
            return max(0.0, round(float(str(self.setting("pass_money_price")).replace(",", ".").strip() or 0), 2))
        except ValueError:
            return 0.0

    def grant_pass(self, user_id: int, why: str) -> bool:
        """Premium pass bought in the web store. False if they had it already."""
        p = self.progress(user_id)
        if p["premium"]:
            return False
        p["premium"] = True
        self._save(user_id, p)
        self.audit(f"<@{user_id}> got the premium battle pass ({self.setting('season_name')}, {why})")
        return True

    def buy_pass(self, user_id: int) -> None:
        p = self.progress(user_id)
        if p["premium"]:
            raise PassError("You already have the premium pass for this season.")
        price = int(self.setting("pass_price"))
        if price and self.bot.db.spend_coins(user_id, price, f"Battle pass {self.setting('season_name')}") is None:
            raise PassError(f"The premium pass costs {self.fmt_coins(price)} – you have "
                            f"{self.fmt_coins(self.bot.db.coins(user_id))}.")
        p["premium"] = True
        self._save(user_id, p)
        self.audit(f"<@{user_id}> bought the premium battle pass ({self.setting('season_name')}, {price})")

    async def claim(self, user_id: int, tier: int, track: str) -> str:
        """Claims one reward. track: "free" or "paid". Returns what was given."""
        p = self.progress(user_id)
        rewards = self.tiers().get(tier)
        if rewards is None or track not in ("free", "paid"):
            raise PassError("That reward doesn't exist.")
        reward = rewards[0] if track == "free" else rewards[1]
        if reward is None:
            raise PassError("Nothing to claim there.")
        if self.level(p["xp"]) < tier:
            raise PassError(f"Reach tier {tier} first.")
        if track == "paid" and not p["premium"]:
            raise PassError("That's a premium reward – get the premium pass first.")
        if tier in p[track]:
            raise PassError("You already claimed that.")
        p[track].append(tier)                            # reserved first: a second click can't give it twice
        self._save(user_id, p)
        try:
            await self._give(user_id, reward, f"Battle pass tier {tier}")
        except PassError:
            p = self.progress(user_id)
            p[track] = [t for t in p[track] if t != tier]
            self._save(user_id, p)
            raise
        return reward.label(self.coin_name, self.bot.config.points_name)

    async def claim_all(self, user_id: int) -> tuple[list[str], list[str]]:
        """Claims everything that's ready. Returns (given, could not)."""
        p = self.progress(user_id)
        level, given, failed = self.level(p["xp"]), [], []
        for tier, (free, paid) in self.tiers().items():
            if tier > level:
                break
            for track, reward in (("free", free), ("paid", paid)):
                if reward is None or tier in p[track] or (track == "paid" and not p["premium"]):
                    continue
                try:
                    given.append(await self.claim(user_id, tier, track))
                except PassError as exc:
                    failed.append(f"Tier {tier}: {exc}")
        return given, failed

    async def _give(self, user_id: int, reward: Reward, why: str) -> None:
        if reward.kind == "coins":
            self.bot.db.add_coins(user_id, reward.amount, why)
        elif reward.kind == "points":
            if not self.points_enabled:
                raise PassError("Points are switched off right now.")
            self.bot.db.add_points(user_id, reward.amount, f"{self.NAME}: {why}")
        else:
            player = self.linked_player(user_id)
            online = self.online_name(player) if player else None
            if online is None:
                # Several servers (servers.py): given on whichever of our servers the player is on
                import servers
                for s in servers.load_registry():
                    if player and s.get("enabled", True) and \
                            await servers.give_on(s["id"], player, item=reward.item, amount=reward.amount) == "given":
                        return
                raise PassError("Join the server first – items are given to you in-game.")
            command = self.bot.config.item_give_template.format(name=sanitize_player_name(online),
                                                                item=reward.item, amount=reward.amount)
            try:
                await self.bot.rcon.command(command, expect_response=False)
            except RconError:
                raise PassError("The server can't be reached right now – try again in a moment.") from None

    def add_coins_from_store(self, user_id: int, coins: int, order_id: int) -> None:
        self.bot.db.add_coins(user_id, coins, f"Web store order #{order_id}")

    # ------------------------------------------------------------ Admin commands

    @coins_group.command(name="give", description="Give (or take, with a minus) premium currency to a member.")
    @app_commands.describe(member="Discord member", amount="How many (negative takes some away)")
    @admin_only()
    async def coins_give(self, interaction: discord.Interaction, member: discord.Member,
                         amount: app_commands.Range[int, -10_000_000, 10_000_000]) -> None:
        balance = self.bot.db.add_coins(member.id, amount, f"by {interaction.user}")
        self.audit(f"{interaction.user.mention} gave {amount} {self.coin_name} to {member.mention}")
        await interaction.response.send_message(
            f"{self.coin_icon} {member.mention} now has **{self.fmt_coins(balance)}**.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @coins_group.command(name="balance", description="Your premium currency and battle pass.")
    async def coins_balance(self, interaction: discord.Interaction) -> None:
        p = self.progress(interaction.user.id)
        coins = self.fmt_coins(self.bot.db.coins(interaction.user.id))
        await interaction.response.send_message(
            f"{coins} · Battle pass **{self.setting('season_name')}**: tier {self.level(p['xp'])}"
            + (" (premium)" if p["premium"] else "") +
            " – see the Battle pass page on our website.", ephemeral=True)
