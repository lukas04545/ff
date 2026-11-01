"""Market & trader with a stash (virtual inventory).

Console Edition has no command that takes items out of a player's in-game inventory, so trading runs on a stash:
  /trader list · /trader buy <item> <amount>     buy from the server into your stash (points)
  /trader sell <item> <amount>                   sell stash items back to the server (lower price)
  /stash · /stash claim <item> <amount>          your stash; claim = items delivered in game (online, linked)
  /market list · /market sell <item> <amount> <price> · /market buy <id> · /market cancel <id>
                                                 player-to-player listings (the seller gets the price minus a fee)
Admins: /stash give <member> <item> <amount> (prizes, refunds).
"""
from __future__ import annotations

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from rce import items as itemlist
from rce.rcon_client import RconError
from utils import admin_only

COLOUR = discord.Colour(0xD4A017)
DEFAULT_CATALOGUE = """# item shortname = buy price / sell-back price (points)
wood = 2 / 1
stones = 2 / 1
metal.fragments = 5 / 2
lowgradefuel = 8 / 4
scrap = 10 / 5
bandage = 30 / 10
syringe.medical = 80 / 30
ammo.rifle = 15 / 5
rifle.ak = 3000 / 1200"""


def parse_catalogue(text: str) -> dict[str, tuple[int, int]]:
    out = {}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        item, _, prices = line.partition("=")
        parts = [p.strip() for p in prices.replace("|", "/").split("/")]
        try:
            buy = int(parts[0])
            sell = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        except ValueError:
            continue
        if item.strip():
            out[item.strip().lower()] = (max(0, buy), max(0, sell))
    return out


class Market(Plugin):
    NAME = "Market & trader"
    ICON = "🏪"
    DESCRIPTION = ("A stash for every player: buy items from the trader, sell them back, trade them with other players "
                   "on the market, and claim them in game. (Console Edition can't take items out of an in-game "
                   "inventory, so trading happens in the stash.)")
    SETTINGS = (
        Setting("catalogue", "Trader: items and prices", "textarea", DEFAULT_CATALOGUE,
                help="One item per line: shortname = buy price / sell-back price. Sell 0 = can't be sold back."),
        Setting("fee", "Market fee (%)", "int", 5, min=0, max=50, help="Taken from the seller's price."),
        Setting("max_listings", "Listings per player", "int", 10, min=1, max=100),
        Setting("channel", "Market channel (new listings)", "channel", help="Empty = not announced."),
    )

    trader = app_commands.Group(name="trader", description="Buy from / sell to the server", guild_only=True)
    stash = app_commands.Group(name="stash", description="Your stash (virtual inventory)", guild_only=True)
    market = app_commands.Group(name="market", description="Player market", guild_only=True)

    # ------------------------------------------------------------ Data

    def catalogue(self) -> dict[str, tuple[int, int]]:
        return parse_catalogue(self.setting("catalogue"))

    def stash_of(self, user_id: int) -> dict[str, int]:
        return {k: int(v) for k, v in (self.data.get(f"stash:{user_id}", {}) or {}).items() if int(v) > 0}

    def _add(self, user_id: int, item: str, amount: int) -> None:
        stash = self.stash_of(user_id)
        stash[item] = stash.get(item, 0) + amount
        self.data.set(f"stash:{user_id}", {k: v for k, v in stash.items() if v > 0})

    def _take(self, user_id: int, item: str, amount: int) -> bool:
        stash = self.stash_of(user_id)
        if stash.get(item, 0) < amount or amount <= 0:
            return False
        stash[item] -= amount
        self.data.set(f"stash:{user_id}", {k: v for k, v in stash.items() if v > 0})
        return True

    def listings(self) -> list[dict]:
        return list(self.data.get("listings", []))

    @staticmethod
    def label(item: str) -> str:
        return itemlist.COMMON_ITEMS.get(item, item)

    # ------------------------------------------------------------ Trader

    def buy(self, user_id: int, item: str, amount: int) -> tuple[bool, str]:
        item = item.strip().lower()
        price = self.catalogue().get(item)
        if price is None or not price[0]:
            return False, "The trader doesn't sell that."
        cost = price[0] * amount
        if self.take_points(user_id, cost, f"trader: {amount}× {item}") is None:
            return False, f"That costs {self.fmt_points(cost)} – you don't have enough."
        self._add(user_id, item, amount)
        return True, f"Bought {amount}× {self.label(item)} for {self.fmt_points(cost)} – it's in your /stash."

    def sell(self, user_id: int, item: str, amount: int) -> tuple[bool, str]:
        item = item.strip().lower()
        price = self.catalogue().get(item)
        if price is None or not price[1]:
            return False, "The trader doesn't buy that."
        if not self._take(user_id, item, amount):
            return False, f"Your stash has {self.stash_of(user_id).get(item, 0)}× {self.label(item)}."
        got = price[1] * amount
        self.give_points(user_id, got, f"trader: sold {amount}× {item}")
        return True, f"Sold {amount}× {self.label(item)} for {self.fmt_points(got)}."

    # ------------------------------------------------------------ Market

    def list_item(self, user_id: int, item: str, amount: int, price: int) -> tuple[bool, str]:
        item = item.strip().lower()
        if price < 1:
            return False, "Set a price."
        if sum(1 for x in self.listings() if x["seller"] == user_id) >= self.setting("max_listings"):
            return False, "You have too many listings – cancel one first."
        if not self._take(user_id, item, amount):
            return False, f"Your stash has {self.stash_of(user_id).get(item, 0)}× {self.label(item)}."
        lid = int(self.data.get("next_id", 1))
        self.data.set("next_id", lid + 1)
        self.data.set("listings", self.listings() + [{"id": lid, "seller": user_id, "item": item, "amount": amount,
                                                      "price": price, "at": self.now()}])
        return True, f"Listed #{lid}: {amount}× {self.label(item)} for {self.fmt_points(price)}."

    def buy_listing(self, user_id: int, lid: int) -> tuple[bool, str]:
        rows = self.listings()
        row = next((x for x in rows if x["id"] == lid), None)
        if row is None:
            return False, "That listing is gone."
        if row["seller"] == user_id:
            return False, "That's your own listing – /market cancel it instead."
        if self.take_points(user_id, row["price"], f"market #{lid}") is None:
            return False, f"It costs {self.fmt_points(row['price'])} – you don't have enough."
        self.data.set("listings", [x for x in rows if x["id"] != lid])
        fee = row["price"] * self.setting("fee") // 100
        self.bot.db.add_points(row["seller"], row["price"] - fee, f"{self.NAME}: sold market #{lid}")
        self._add(user_id, row["item"], row["amount"])
        self.data.set("sold", int(self.data.get("sold", 0)) + 1)
        return True, f"Bought {row['amount']}× {self.label(row['item'])} – it's in your /stash."

    def cancel(self, user_id: int, lid: int, *, admin: bool = False) -> tuple[bool, str]:
        rows = self.listings()
        row = next((x for x in rows if x["id"] == lid and (admin or x["seller"] == user_id)), None)
        if row is None:
            return False, "No such listing of yours."
        self.data.set("listings", [x for x in rows if x["id"] != lid])
        self._add(row["seller"], row["item"], row["amount"])
        return True, f"Listing #{lid} cancelled – the items are back in the stash."

    # ------------------------------------------------------------ Claiming in game

    async def claim(self, user_id: int, item: str, amount: int) -> tuple[bool, str]:
        item = item.strip().lower()
        player = self.online_name(self.linked_player(user_id) or "")
        if player is None:
            return False, "Join the server first – your linked player must be online (/customkit link)."
        if not self._take(user_id, item, amount):
            return False, f"Your stash has {self.stash_of(user_id).get(item, 0)}× {self.label(item)}."
        try:
            await self.bot.rcon.command(self.bot.config.item_give_template.format(name=player, item=item,
                                                                                amount=amount), expect_response=False)
        except RconError as exc:
            self._add(user_id, item, amount)
            return False, f"The server didn't take it ({exc}) – the items stay in your stash."
        return True, f"{amount}× {self.label(item)} delivered to **{self.md(player)}**."

    # ------------------------------------------------------------ Discord

    async def _send(self, interaction, ok: bool, text: str) -> None:
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + text, ephemeral=True)

    async def _catalogue_ac(self, interaction, current: str):
        q = current.lower()
        return [app_commands.Choice(name=f"{self.label(i)} – {b} / {s}"[:100], value=i)
                for i, (b, s) in self.catalogue().items() if q in i or q in self.label(i).lower()][:25]

    async def _stash_ac(self, interaction, current: str):
        q = current.lower()
        return [app_commands.Choice(name=f"{self.label(i)} × {n}"[:100], value=i)
                for i, n in self.stash_of(interaction.user.id).items() if q in i or q in self.label(i).lower()][:25]

    @trader.command(name="list", description="What the trader sells and buys.")
    async def trader_list(self, interaction: discord.Interaction) -> None:
        lines = [f"**{self.label(i)}** (`{i}`) – buy {self.fmt_points(b)}" + (f" · sells for {self.fmt_points(s)}"
                                                                               if s else "")
                 for i, (b, s) in self.catalogue().items()]
        embed = discord.Embed(title="🏪 Trader", colour=COLOUR, description="\n".join(lines)[:4000] or "Nothing.")
        embed.set_footer(text="/trader buy · /trader sell · bought items go to your /stash")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @trader.command(name="buy", description="Buy items from the trader into your stash.")
    @app_commands.autocomplete(item=_catalogue_ac)
    async def trader_buy(self, interaction: discord.Interaction, item: str,
                         amount: app_commands.Range[int, 1, 100_000] = 1) -> None:
        await self._send(interaction, *self.buy(interaction.user.id, item, amount))

    @trader.command(name="sell", description="Sell stash items back to the trader.")
    @app_commands.autocomplete(item=_stash_ac)
    async def trader_sell(self, interaction: discord.Interaction, item: str,
                          amount: app_commands.Range[int, 1, 100_000] = 1) -> None:
        await self._send(interaction, *self.sell(interaction.user.id, item, amount))

    @stash.command(name="show", description="What's in your stash.")
    async def stash_show(self, interaction: discord.Interaction) -> None:
        stash = self.stash_of(interaction.user.id)
        lines = [f"**{self.label(i)}** × {n}" for i, n in sorted(stash.items())]
        embed = discord.Embed(title="🎒 Your stash", colour=COLOUR,
                              description="\n".join(lines)[:4000] or "Empty – buy from /trader or the /market.")
        embed.set_footer(text="/stash claim delivers items in game · /market sell lists them for other players")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @stash.command(name="claim", description="Get stash items delivered in game (be online).")
    @app_commands.autocomplete(item=_stash_ac)
    async def stash_claim(self, interaction: discord.Interaction, item: str,
                          amount: app_commands.Range[int, 1, 100_000] = 1) -> None:
        await self._send(interaction, *await self.claim(interaction.user.id, item, amount))

    @stash.command(name="give", description="Put items into a member's stash (admins).")
    @admin_only()
    async def stash_give(self, interaction: discord.Interaction, member: discord.Member, item: str,
                         amount: app_commands.Range[int, 1, 100_000] = 1) -> None:
        self._add(member.id, item.strip().lower(), amount)
        self.audit(f"{interaction.user} gave {amount}× {item} to {member}'s stash")
        await self._send(interaction, True, f"Added {amount}× {self.label(item.strip().lower())} to "
                                            f"{member.mention}'s stash.")

    @market.command(name="list", description="Items other players are selling.")
    async def market_list(self, interaction: discord.Interaction) -> None:
        rows = sorted(self.listings(), key=lambda x: x["id"], reverse=True)[:25]
        lines = [f"`#{x['id']}` **{self.label(x['item'])}** × {x['amount']} – {self.fmt_points(x['price'])} "
                 f"· <@{x['seller']}>" for x in rows]
        embed = discord.Embed(title="🏪 Market", colour=COLOUR, description="\n".join(lines) or "Nothing for sale.")
        embed.set_footer(text="/market buy <id> · /market sell from your stash")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @market.command(name="sell", description="List stash items for other players.")
    @app_commands.autocomplete(item=_stash_ac)
    async def market_sell(self, interaction: discord.Interaction, item: str,
                          amount: app_commands.Range[int, 1, 100_000], price: app_commands.Range[int, 1, 100_000_000]
                          ) -> None:
        ok, text = self.list_item(interaction.user.id, item, amount, price)
        await self._send(interaction, ok, text)
        if ok and self.setting("channel"):
            await self.post(self.channel(self.setting("channel")), f"🏪 New on the market: {text.split(': ', 1)[-1]} "
                                                                   f"(by {interaction.user.mention}) – /market buy")

    @market.command(name="buy", description="Buy a market listing.")
    async def market_buy(self, interaction: discord.Interaction, listing: int) -> None:
        await self._send(interaction, *self.buy_listing(interaction.user.id, listing))

    @market.command(name="cancel", description="Take your listing back (items return to your stash).")
    async def market_cancel(self, interaction: discord.Interaction, listing: int) -> None:
        from utils import is_admin
        await self._send(interaction, *self.cancel(interaction.user.id, listing,
                                                   admin=is_admin(self.bot, interaction.user)))

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        out = [] if self.points_enabled else ["Points & shop are switched off – the market needs points."]
        unknown = [i for i in self.catalogue() if i not in itemlist.COMMON_ITEMS]
        if unknown:
            out.append("Unknown shortnames in the trader list: " + ", ".join(unknown[:8]))
        return out

    def status(self) -> dict:
        return {"Trader items": len(self.catalogue()), "Listings": len(self.listings()),
                "Market sales": int(self.data.get("sold", 0))}
