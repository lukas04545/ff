"""Points, shop, daily reward, playtime rewards and /profile.

Points belong to a Discord account and are earned with the player linked to it (/customkit link):
  * playtime        POINTS_PER_HOUR while online
  * PvP kills       POINTS_PER_KILL (the same killer/victim pair at most once every 10 minutes)
  * /daily          DAILY_POINTS + DAILY_STREAK_BONUS per day in a row (max. 7)
  * playtime reward every PLAYTIME_REWARD_HOURS online: PLAYTIME_REWARD_KIT and/or PLAYTIME_REWARD_POINTS
They are spent in /shop (items: web interface → Shop): custom kit, in-game kit, items or a Discord role
(optionally for N days). If delivery fails, the points are refunded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rce.kill_sources import KillerType
from rce.rcon_client import RconError
from utils import admin_only, format_duration, safe_md, sanitize_ingame_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.economy")

SHOP_KINDS = {"custom_kit": "Custom kit", "kit": "In-game kit", "item": "Item", "role": "Discord role"}
DAILY_COOLDOWN = 20 * 3600       # a bit less than a day, so the time of day can drift
STREAK_WINDOW = 48 * 3600        # /daily within this time keeps the streak
STREAK_MAX = 7
KILL_PAIR_COOLDOWN = 600
COLOUR = discord.Colour(0xE0A23B)


@dataclass
class BuyOutcome:
    ok: bool
    message: str


class Economy(commands.Cog):
    points = app_commands.Group(name="points", description="Points: balance, top list, history", guild_only=True)
    shop = app_commands.Group(name="shop", description="Spend your points", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._remainder: defaultdict[int, float] = defaultdict(float)   # online seconds not yet paid out
        self._last_tick: float | None = None
        self._kill_pairs: dict[tuple[str, str], float] = {}
        self._buy_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def enabled(self) -> bool:
        return self.bot.config.economy_enabled

    def fmt(self, amount: int) -> str:
        return f"**{amount:,}** {self.bot.config.points_name}".replace(",", " ")

    async def cog_load(self) -> None:
        if self.enabled:
            self.earn_loop.start()

    async def cog_unload(self) -> None:
        self.earn_loop.cancel()

    async def on_config_changed(self, old, new) -> None:
        if new.economy_enabled and not self.earn_loop.is_running():
            self._last_tick = None
            self.earn_loop.start()
        elif not new.economy_enabled and self.earn_loop.is_running():
            self.earn_loop.cancel()

    async def _disabled(self, interaction: discord.Interaction) -> bool:
        if self.enabled:
            return False
        await interaction.response.send_message("Points & shop are switched off (ECONOMY_ENABLED).", ephemeral=True)
        return True

    def _online_name(self, player: str | None) -> str | None:
        if not player:
            return None
        return next((n for n in self.bot.online_players if n.lower() == player.lower()), None)

    # ------------------------------------------------------------ Earning

    @tasks.loop(seconds=60)
    async def earn_loop(self) -> None:
        now = time.monotonic()
        elapsed, self._last_tick = (min(now - self._last_tick, 120) if self._last_tick else 0), now
        if not elapsed or not self.bot.rcon.connected:
            return
        await self.credit_playtime(elapsed)

    @earn_loop.before_loop
    async def _before_earn(self) -> None:
        await self.bot.wait_until_ready()

    async def credit_playtime(self, elapsed: float) -> None:
        """Pays points for `elapsed` seconds online and hands out playtime rewards (linked players only)."""
        cfg = self.bot.config
        for player in list(self.bot.online_players):
            user_id = self.bot.db.player_owner(player)
            if user_id is None:
                continue
            if cfg.points_per_hour:
                self._remainder[user_id] += elapsed
                earned = int(self._remainder[user_id] * cfg.points_per_hour / 3600)
                if earned:
                    self._remainder[user_id] -= earned * 3600 / cfg.points_per_hour
                    self.bot.db.add_points(user_id, earned, "playtime", log=False)
            if cfg.playtime_reward_hours:
                need = cfg.playtime_reward_hours * 3600
                if self.bot.db.add_playtime_progress(user_id, int(elapsed)) >= need:
                    self.bot.db.add_playtime_progress(user_id, -need)
                    await self._playtime_reward(user_id, player)

    async def _playtime_reward(self, user_id: int, player: str) -> None:
        cfg = self.bot.config
        got = []
        kit = self.bot.db.custom_kit(cfg.playtime_reward_kit) if cfg.playtime_reward_kit else None
        custom = self.bot.get_cog("CustomKits")
        if kit is not None and custom is not None:
            try:
                sent, total = await custom.deliver(kit["name"], player)
                if sent:
                    got.append(f"kit {kit['name']}")
            except RconError as exc:
                log.warning("Playtime reward kit for %s failed: %s", player, exc)
        elif cfg.playtime_reward_kit:
            log.warning("PLAYTIME_REWARD_KIT %r is not a custom kit.", cfg.playtime_reward_kit)
        if cfg.playtime_reward_points:
            self.bot.db.add_points(user_id, cfg.playtime_reward_points, f"playtime reward ({cfg.playtime_reward_hours}h)")
            got.append(f"{cfg.playtime_reward_points} {cfg.points_name}")
        if not got:
            return
        self.bot.audit(f"⏱️ Playtime reward ({cfg.playtime_reward_hours}h online) for <@{user_id}> "
                       f"({safe_md(player)}): {', '.join(got)}")
        text = sanitize_ingame_text(f"{player} got the playtime reward for {cfg.playtime_reward_hours}h online: "
                                    f"{', '.join(got)}!")
        try:
            await self.bot.rcon.command(f"say {text}", expect_response=False)
        except RconError:
            pass

    @commands.Cog.listener()
    async def on_rce_kill(self, ev) -> None:
        per_kill = self.bot.config.points_per_kill
        if not (self.enabled and per_kill and ev.killer_type is KillerType.PLAYER
                and ev.victim_type is KillerType.PLAYER and ev.killer.lower() != ev.victim.lower()):
            return
        user_id = self.bot.db.player_owner(ev.killer)
        if user_id is None:
            return
        pair, now = (ev.killer.lower(), ev.victim.lower()), time.monotonic()
        if now - self._kill_pairs.get(pair, -KILL_PAIR_COOLDOWN) < KILL_PAIR_COOLDOWN:
            return   # same victim again right away – no farming
        self._kill_pairs[pair] = now
        if len(self._kill_pairs) > 5000:
            self._kill_pairs = {k: v for k, v in self._kill_pairs.items() if now - v < KILL_PAIR_COOLDOWN}
        self.bot.db.add_points(user_id, per_kill, f"kill: {ev.victim}"[:200])

    # ------------------------------------------------------------ Daily

    def daily(self, user_id: int) -> tuple[bool, str]:
        """Claims the daily reward. (ok, message)"""
        cfg = self.bot.config
        if self.bot.db.linked_player(user_id) is None:
            return False, "🔗 Link your in-game player first with `/customkit link` – then you can collect points."
        wallet, now = self.bot.db.wallet(user_id), int(time.time())
        last = wallet["daily_last"]
        if last and now - last < DAILY_COOLDOWN:
            return False, f"⏳ Already collected – your next `/daily` is ready <t:{last + DAILY_COOLDOWN}:R>."
        streak = wallet["daily_streak"] + 1 if last and now - last <= STREAK_WINDOW else 1
        amount = cfg.daily_points + (min(streak, STREAK_MAX) - 1) * cfg.daily_streak_bonus
        self.bot.db.set_daily(user_id, now, streak)
        balance = self.bot.db.add_points(user_id, amount, f"daily (day {streak})")
        streak_text = f" 🔥 {streak} days in a row!" if streak > 1 else ""
        return True, (f"🪙 +{self.fmt(amount)}{streak_text}\nBalance: {self.fmt(balance)} · next `/daily` "
                      f"<t:{now + DAILY_COOLDOWN}:R> (come back within 2 days to keep the streak).")

    @app_commands.command(name="daily", description="Collect your daily points (more for days in a row).")
    @app_commands.guild_only()
    async def daily_cmd(self, interaction: discord.Interaction) -> None:
        if await self._disabled(interaction):
            return
        ok, text = self.daily(interaction.user.id)
        await interaction.response.send_message(text, ephemeral=not ok)

    # ------------------------------------------------------------ Shop

    def guild(self) -> discord.Guild | None:
        cfg = self.bot.config
        return self.bot.get_guild(cfg.guild_id) if cfg.guild_id else (self.bot.guilds[0] if self.bot.guilds else None)

    def validate_item(self, kind: str, target: str, amount: int, price: int) -> str:
        """Checks a shop item (web interface). Returns the cleaned target or raises ValueError."""
        from rce import kits as kitparse
        target = kitparse.clean_arg(str(target or "")).strip()
        if kind not in SHOP_KINDS:
            raise ValueError("Unknown item type.")
        if price < 0:
            raise ValueError("The price can't be negative.")
        if not target:
            raise ValueError("Choose what the item gives.")
        if kind == "custom_kit":
            row = self.bot.db.custom_kit(target)
            if row is None:
                raise ValueError(f"Custom kit {target} does not exist.")
            return row["name"]
        if kind == "item":
            if not kitparse.valid_shortname(target):
                raise ValueError(f"{target} is not a valid item shortname.")
            if amount < 1:
                raise ValueError("The amount must be at least 1.")
        if kind == "role":
            guild = self.guild()
            role = guild.get_role(int(target)) if guild and target.isdigit() else None
            if role is None:
                raise ValueError("Role not found on the Discord server.")
            if role.managed or role.is_default() or role >= guild.me.top_role:
                raise ValueError(f"The bot can't give {role.name} (managed role or above the bot's role).")
            return str(role.id)
        return target

    def describe_item(self, row, guild: discord.Guild | None = None) -> str:
        kind, target, amount = row["kind"], row["target"], row["amount"]
        if kind == "custom_kit":
            items = self.bot.db.custom_kit_items(target)
            return "Kit: " + (", ".join(f"{i['shortname']} ×{i['amount']}" for i in items[:6])
                              + (" …" if len(items) > 6 else "") if items else target)
        if kind == "kit":
            return f"In-game kit {target}"
        if kind == "item":
            return f"{amount} × {target}"
        role = guild.get_role(int(target)) if guild and target.isdigit() else None
        name = role.name if role else "role"
        return f"Discord role {name}" + (f" for {amount} days" if amount else " (permanent)")

    async def buy(self, member: discord.Member, item_name: str) -> BuyOutcome:
        if not self.enabled:
            return BuyOutcome(False, "Points & shop are switched off.")
        row = self.bot.db.shop_item_by_name(item_name)
        if row is None or not row["enabled"]:
            return BuyOutcome(False, f"There is no shop item **{safe_md(item_name)}** – see `/shop list`.")
        player = self.bot.db.linked_player(member.id)
        if player is None:
            return BuyOutcome(False, "🔗 Link your in-game player first with `/customkit link`.")
        async with self._buy_locks[member.id]:
            online, role = None, None
            if row["kind"] == "role":
                role = member.guild.get_role(int(row["target"])) if row["target"].isdigit() else None
                if role is None:
                    return BuyOutcome(False, "This role no longer exists – please tell an admin.")
                timed = self.bot.get_cog("TimedRoles")
                if role in member.roles and (not row["amount"] or (timed and timed.permanent(member, role))):
                    return BuyOutcome(False, f"You already have **{safe_md(role.name)}**.")
            else:
                online = self._online_name(player)
                if online is None:
                    return BuyOutcome(False, f"🎮 **{safe_md(player)}** is not online – join the server first, "
                                             "the item is sent in-game.")
            price = int(row["price"])
            balance = self.bot.db.spend_points(member.id, price, f"shop: {row['name']}")
            if balance is None:
                have = self.bot.db.wallet(member.id)["points"]
                return BuyOutcome(False, f"Not enough {self.bot.config.points_name}: **{row['name']}** costs "
                                         f"{self.fmt(price)}, you have {self.fmt(have)}.")
            try:
                result = await self._deliver(row, member, online, role)
            except Exception as exc:   # anything went wrong -> money back
                refund = self.bot.db.add_points(member.id, price, f"refund: {row['name']}")
                log.warning("Shop delivery of %s for %s failed: %s", row["name"], member, exc)
                return BuyOutcome(False, f"⚠️ Delivery failed ({exc}) – your {self.fmt(price)} were refunded "
                                         f"(balance {self.fmt(refund)}).")
        self.bot.audit(f"🛒 {member.mention} bought **{safe_md(row['name'])}** for {price} "
                       f"{self.bot.config.points_name} ({result})")
        return BuyOutcome(True, f"✅ **{safe_md(row['name'])}** – {result}. Balance: {self.fmt(balance)}.")

    async def _deliver(self, row, member: discord.Member, online: str | None, role: discord.Role | None) -> str:
        kind, target, amount = row["kind"], row["target"], int(row["amount"])
        cfg = self.bot.config
        if kind == "custom_kit":
            custom = self.bot.get_cog("CustomKits")
            if custom is None or self.bot.db.custom_kit(target) is None:
                raise RuntimeError("custom kit not found")
            sent, total = await custom.deliver(target, online)
            if not total:
                raise RuntimeError("the kit is empty")
            return f"sent to **{safe_md(online)}** in-game ({sent}/{total} items)"
        if kind == "kit":
            await self.bot.rcon.command(cfg.kit_give_template.format(kit=target, name=online), expect_response=False)
            return f"kit sent to **{safe_md(online)}** in-game"
        if kind == "item":
            await self.bot.rcon.command(cfg.item_give_template.format(name=online, item=target, amount=amount),
                                        expect_response=False)
            return f"{amount} × {target} sent to **{safe_md(online)}** in-game"
        if kind == "role":
            if amount:
                timed = self.bot.get_cog("TimedRoles")
                if timed is None:
                    raise RuntimeError("timed roles are not available")
                expires_at = await timed.grant(member, role, amount * 1440, reason=f"shop: {row['name']}",
                                               by="shop")
                return f"you have **{safe_md(role.name)}** until <t:{expires_at}:f>"
            await member.add_roles(role, reason=f"Bought in the shop: {row['name']}")
            return f"you now have **{safe_md(role.name)}**"
        raise RuntimeError(f"unknown item type {kind}")

    async def _shop_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=f"{r['name']} – {r['price']} {self.bot.config.points_name}"[:100],
                                    value=r["name"][:100])
                for r in self.bot.db.shop_items(enabled_only=True) if current.lower() in r["name"].lower()][:25]

    @shop.command(name="list", description="Shows what you can buy with your points.")
    async def shop_list(self, interaction: discord.Interaction) -> None:
        if await self._disabled(interaction):
            return
        rows = self.bot.db.shop_items(enabled_only=True)
        balance = self.bot.db.wallet(interaction.user.id)["points"]
        embed = discord.Embed(title="🛒 Shop", colour=COLOUR,
                              description=f"You have {self.fmt(balance)}. Buy with `/shop buy`." if rows else
                              "The shop is empty – admins add items in the web interface (🪙 Shop).")
        for r in rows[:25]:
            text = r["description"] or self.describe_item(r, interaction.guild)
            embed.add_field(name=f"{r['name']} – {r['price']} {self.bot.config.points_name}"[:256],
                            value=safe_md(text)[:1024], inline=False)
        embed.set_footer(text="Earn points: playtime, PvP kills, /daily · needs a linked player (/customkit link)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shop.command(name="buy", description="Buy something from the shop.")
    @app_commands.describe(item="Shop item")
    @app_commands.autocomplete(item=_shop_ac)
    async def shop_buy(self, interaction: discord.Interaction, item: str) -> None:
        if await self._disabled(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        outcome = await self.buy(interaction.user, item)
        await interaction.followup.send(outcome.message, ephemeral=True)

    # ------------------------------------------------------------ Points commands

    @points.command(name="balance", description="Shows your points (or someone else's).")
    @app_commands.describe(member="Member (default: you)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if await self._disabled(interaction):
            return
        member = member or interaction.user
        w = self.bot.db.wallet(member.id)
        await interaction.response.send_message(
            f"🪙 {member.mention} has {self.fmt(w['points'])} (earned in total: {w['earned_total']:,}).",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @points.command(name="top", description="The members with the most points.")
    async def top(self, interaction: discord.Interaction) -> None:
        if await self._disabled(interaction):
            return
        rows = self.bot.db.top_wallets(10)
        lines = [f"{['🥇', '🥈', '🥉'][i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> – {self.fmt(r['points'])}"
                 for i, r in enumerate(rows)]
        embed = discord.Embed(title=f"🪙 Top {self.bot.config.points_name}", colour=COLOUR,
                              description="\n".join(lines) or "Nobody has collected any yet.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @points.command(name="history", description="Your last point changes.")
    @app_commands.describe(member="Member (admins only)")
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if await self._disabled(interaction):
            return
        from utils import is_admin
        if member is not None and member.id != interaction.user.id and not is_admin(self.bot, interaction.user):
            await interaction.response.send_message("Only admins can see other members' history.", ephemeral=True)
            return
        member = member or interaction.user
        rows = self.bot.db.point_history(member.id, 15)
        lines = [f"`{'+' if r['delta'] > 0 else ''}{r['delta']}` {safe_md(r['reason'])} · {r['created_at']} UTC"
                 for r in rows]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"🪙 History – {member.display_name}", colour=COLOUR,
                                description="\n".join(lines) or "No changes yet."), ephemeral=True)

    @points.command(name="give", description="Gives a member points.")
    @app_commands.describe(member="Member", amount="How many", reason="Why (shown in the history)")
    @admin_only()
    async def give(self, interaction: discord.Interaction, member: discord.Member,
                   amount: app_commands.Range[int, 1, 1_000_000], reason: str | None = None) -> None:
        balance = self.bot.db.add_points(member.id, amount, f"by {interaction.user}: {reason or 'given by staff'}")
        self.bot.audit(f"🪙 {safe_md(str(interaction.user))} gave {member.mention} {amount} "
                       f"{self.bot.config.points_name} ({safe_md(reason or '-')})")
        await interaction.response.send_message(f"✅ {member.mention} now has {self.fmt(balance)}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @points.command(name="take", description="Takes points from a member.")
    @app_commands.describe(member="Member", amount="How many", reason="Why (shown in the history)")
    @admin_only()
    async def take(self, interaction: discord.Interaction, member: discord.Member,
                   amount: app_commands.Range[int, 1, 1_000_000], reason: str | None = None) -> None:
        balance = self.bot.db.add_points(member.id, -amount, f"by {interaction.user}: {reason or 'taken by staff'}")
        self.bot.audit(f"🪙 {safe_md(str(interaction.user))} took {amount} {self.bot.config.points_name} from "
                       f"{member.mention} ({safe_md(reason or '-')})")
        await interaction.response.send_message(f"✅ {member.mention} now has {self.fmt(balance)}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ Profile

    def profile_embed(self, member: discord.Member) -> discord.Embed:
        db, cfg = self.bot.db, self.bot.config
        player = db.linked_player(member.id)
        embed = discord.Embed(title=f"👤 {member.display_name}", colour=COLOUR)
        embed.set_thumbnail(url=member.display_avatar.url)
        if player:
            online = self._online_name(player) is not None
            embed.add_field(name="In-game", value=f"{'🟢' if online else '⚪'} {safe_md(player)}")
            stats = db.player(player)
            if stats:
                kd = stats["kills"] / max(stats["deaths"], 1)
                embed.add_field(name="PvP", value=f"{stats['kills']} kills · {stats['deaths']} deaths · K/D {kd:.2f}")
                embed.add_field(name="Playtime", value=format_duration(stats["playtime_seconds"]))
        else:
            embed.add_field(name="In-game", value="not linked – `/customkit link`", inline=False)
        if self.enabled:
            w = db.wallet(member.id)
            daily = ("ready now" if not w["daily_last"] or time.time() - w["daily_last"] >= DAILY_COOLDOWN
                     else f"<t:{w['daily_last'] + DAILY_COOLDOWN}:R>")
            embed.add_field(name=cfg.points_name.capitalize(), value=f"{w['points']:,}".replace(",", " "))
            embed.add_field(name="/daily", value=f"{daily}" + (f" · 🔥 {w['daily_streak']}" if w["daily_streak"] else ""))
            if cfg.playtime_reward_hours:
                left = max(cfg.playtime_reward_hours * 3600 - w["playtime_progress"], 0)
                embed.add_field(name="Next playtime reward", value=f"in {format_duration(left)} online")
        custom = self.bot.get_cog("CustomKits")
        if custom is not None:
            kits = custom.kit_overview(member)["claimable_kits"]
            if kits:
                embed.add_field(name="Custom kits", inline=False, value="\n".join(
                    f"🎁 **{safe_md(k['kit'])}** – " + ("ready" if k["can_claim_now"] else
                                                        f"again {k['can_claim_again_discord']}") for k in kits)[:1024])
        timed = db.timed_roles(user_id=member.id)
        if timed:
            embed.add_field(name="Timed roles", inline=False, value="\n".join(
                f"⏳ <@&{r['role_id']}> until <t:{r['expires_at']}:R>" for r in timed)[:1024])
        return embed

    @app_commands.command(name="profile", description="Your stats, points, kits and roles (or someone else's).")
    @app_commands.describe(member="Member (default: you)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await interaction.response.send_message(embed=self.profile_embed(member or interaction.user),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Economy(bot))
