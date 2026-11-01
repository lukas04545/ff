"""Bank & pay: send points to other members, a bank account with daily interest, and begging.

/pay <member> <amount>    send points (optional fee)
/bank                     wallet + bank balance
/deposit <amount|all>     wallet -> bank (earns interest every day)
/withdraw <amount|all>    bank -> wallet
/beg                      a few points now and then (cooldown, sometimes nothing)
"""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0x2ECC71)


class Bank(Plugin):
    NAME = "Bank & pay"
    ICON = "🏦"
    DESCRIPTION = "Members pay each other points, keep points in a bank that earns daily interest, and can /beg."
    SETTINGS = (
        Setting("pay_fee", "Fee for /pay (%)", "int", 0, min=0, max=50, help="Taken from the amount sent (0 = none)."),
        Setting("pay_min", "Smallest /pay", "int", 1, min=1, max=1_000_000),
        Setting("interest", "Daily interest (‰, per mille)", "int", 10, min=0, max=200,
                help="10 = 1 % per day on the bank balance."),
        Setting("interest_cap", "Max interest per day", "int", 500, min=0, max=10_000_000,
                help="So big balances don't explode (0 = no limit)."),
        Setting("beg_max", "/beg: up to … points", "int", 25, min=0, max=100_000, help="0 = /beg off."),
        Setting("beg_cooldown", "/beg cooldown (minutes)", "int", 60, min=1, max=10080),
        Setting("beg_chance", "/beg: chance to get something (%)", "int", 70, min=1, max=100),
    )

    # ------------------------------------------------------------ Data

    def bank(self, user_id: int) -> int:
        return int(self.data.get(f"bank:{user_id}", 0))

    def _set_bank(self, user_id: int, amount: int) -> None:
        self.data.set(f"bank:{user_id}", max(0, int(amount)))

    def wallet(self, user_id: int) -> int:
        return int(self.bot.db.wallet(user_id)["points"])

    @staticmethod
    def _amount(text: str, available: int) -> int | None:
        text = str(text).strip().lower().replace(" ", "").replace(",", "")
        if text in ("all", "max"):
            return available
        return int(text) if text.isdigit() else None

    # ------------------------------------------------------------ Actions (Discord + tests)

    def pay(self, sender: int, receiver: int, amount: int) -> tuple[bool, str]:
        if sender == receiver:
            return False, "You can't pay yourself."
        if amount < self.setting("pay_min"):
            return False, f"The smallest payment is {self.fmt_points(self.setting('pay_min'))}."
        fee = amount * self.setting("pay_fee") // 100
        if self.take_points(sender, amount, f"/pay to {receiver}") is None:
            return False, f"Not enough {self.bot.config.points_name}: you have {self.fmt_points(self.wallet(sender))}."
        self.bot.db.add_points(receiver, amount - fee, f"{self.NAME}: /pay from {sender}")
        self.data.set("paid_total", int(self.data.get("paid_total", 0)) + amount)
        return True, (f"Sent {self.fmt_points(amount - fee)}" + (f" (fee {self.fmt_points(fee)})" if fee else "") + ".")

    def deposit(self, user_id: int, amount: int) -> tuple[bool, str]:
        if amount <= 0:
            return False, "Nothing to deposit."
        if self.take_points(user_id, amount, "bank deposit") is None:
            return False, f"Your wallet only has {self.fmt_points(self.wallet(user_id))}."
        self._set_bank(user_id, self.bank(user_id) + amount)
        return True, f"Deposited {self.fmt_points(amount)}. Bank: {self.fmt_points(self.bank(user_id))}."

    def withdraw(self, user_id: int, amount: int) -> tuple[bool, str]:
        have = self.bank(user_id)
        if amount <= 0 or amount > have:
            return False, f"Your bank has {self.fmt_points(have)}."
        self._set_bank(user_id, have - amount)
        self.bot.db.add_points(user_id, amount, f"{self.NAME}: bank withdrawal")
        return True, f"Withdrew {self.fmt_points(amount)}. Bank: {self.fmt_points(self.bank(user_id))}."

    def beg(self, user_id: int, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        if not self.setting("beg_max"):
            return False, "Begging is switched off."
        last = float(self.data.get(f"beg:{user_id}", 0))
        wait = self.setting("beg_cooldown") * 60 - (now - last)
        if wait > 0:
            return False, f"Try again <t:{int(now + wait)}:R>."
        self.data.set(f"beg:{user_id}", now)
        if random.randint(1, 100) > self.setting("beg_chance"):
            return True, random.choice(("Nobody looked up. 🥶", "A scientist walked past without a word.",
                                        "You got a rock. It's not worth anything."))
        got = random.randint(1, self.setting("beg_max"))
        self.give_points(user_id, got, "/beg")
        return True, f"Someone tossed you {self.fmt_points(got)}. 🪙"

    def pay_interest(self) -> int:
        """Daily interest on every bank balance. Returns the total paid."""
        rate, cap, total = self.setting("interest"), self.setting("interest_cap"), 0
        if not rate:
            return 0
        for key, value in self.data.items("bank:").items():
            gain = int(value) * rate // 1000
            if cap:
                gain = min(gain, cap)
            if gain > 0:
                self.data.set(key, int(value) + gain)
                total += gain
        return total

    @tasks.loop(hours=1)
    async def interest_loop(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.data.get("interest_day") == today:
            return
        self.data.set("interest_day", today)
        total = self.pay_interest()
        if total:
            self.log.info("Bank interest paid: %s", total)

    # ------------------------------------------------------------ Discord

    async def _reply(self, interaction: discord.Interaction, ok: bool, text: str, *, public: bool = False) -> None:
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + text, ephemeral=not (ok and public),
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="pay", description="Send points to another member.")
    @app_commands.guild_only()
    async def pay_cmd(self, interaction: discord.Interaction, member: discord.Member,
                      amount: app_commands.Range[int, 1, 1_000_000_000]) -> None:
        if member.bot:
            await self._reply(interaction, False, "Bots don't need points.")
            return
        ok, text = self.pay(interaction.user.id, member.id, amount)
        await self._reply(interaction, ok, f"{text} → {member.mention}" if ok else text, public=True)

    @app_commands.command(name="bank", description="Your wallet and bank balance.")
    @app_commands.guild_only()
    async def bank_cmd(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        embed = discord.Embed(title="🏦 Bank", colour=COLOUR)
        embed.add_field(name="👛 Wallet", value=self.fmt_points(self.wallet(uid)))
        embed.add_field(name="🏦 Bank", value=self.fmt_points(self.bank(uid)))
        if self.setting("interest"):
            embed.set_footer(text=f"The bank pays {self.setting('interest') / 10:g} % interest every day · "
                                  "/deposit · /withdraw · /pay")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="deposit", description="Move points from your wallet into the bank.")
    @app_commands.describe(amount="A number or 'all'")
    @app_commands.guild_only()
    async def deposit_cmd(self, interaction: discord.Interaction, amount: str) -> None:
        n = self._amount(amount, self.wallet(interaction.user.id))
        await self._reply(interaction, *(self.deposit(interaction.user.id, n) if n is not None
                                         else (False, "Enter a number or 'all'.")))

    @app_commands.command(name="withdraw", description="Move points from the bank into your wallet.")
    @app_commands.describe(amount="A number or 'all'")
    @app_commands.guild_only()
    async def withdraw_cmd(self, interaction: discord.Interaction, amount: str) -> None:
        n = self._amount(amount, self.bank(interaction.user.id))
        await self._reply(interaction, *(self.withdraw(interaction.user.id, n) if n is not None
                                         else (False, "Enter a number or 'all'.")))

    @app_commands.command(name="beg", description="Beg for a few points.")
    @app_commands.guild_only()
    async def beg_cmd(self, interaction: discord.Interaction) -> None:
        ok, text = self.beg(interaction.user.id)
        await interaction.response.send_message(text, ephemeral=not ok)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        return [] if self.points_enabled else ["Points & shop are switched off – the bank needs points."]

    def status(self) -> dict:
        banks = self.data.items("bank:")
        return {"Bank accounts": len(banks), "In the bank": sum(int(v) for v in banks.values()),
                "Sent with /pay": int(self.data.get("paid_total", 0))}

    def profile_fields(self, member) -> list[tuple[str, str]]:
        return [("🏦 Bank", self.fmt_points(self.bank(member.id)))] if self.bank(member.id) else []
