"""Casino: players gamble their points on the player website (/casino) – slots, coin flip, dice, roulette,
blackjack, Plinko, and Texas Hold'em poker tables where players play against each other. Play money only: points are
earned in-game and can't be bought or cashed out.

Every game but poker can also be played on Discord, in an embed with an animated picture (support/casino_images.py,
support/plinko_image.py): /slots, /coinflip, /dice, /roulette, /plinko – the result shows once the animation has
ended – and /blackjack with Hit/Stand/Double/Split buttons. Same games, odds and points as the website.

Slots, coin flip and dice return 95 % of the bets on average, roulette is a real European wheel (97.3 %) and
blackjack is played with real casino rules (≈ 99.5 %). Plinko returns 87.5 % on every risk and row count. The games themselves are in support/casino_games.py, the
pages in web/casino_pages.py. Players log in with the website's player login (/account).
"""
from __future__ import annotations

import asyncio
import io
import re
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from support import casino_games as games
from support import poker
from support.casino_games import GameError
from support.poker import PokerError

GAMES = {           # id -> (name, icon, line shown under it)
    "slots": ("Slots", "🎰", "Return to player 95 %"),
    "coinflip": ("Coin flip", "🪙", "Return to player 95 %"),
    "dice": ("Dice", "🎲", "Return to player 95 %"),
    "roulette": ("Roulette", "🎡", "Return to player 97.3 %"),
    "blackjack": ("Blackjack", "🃏", "Return to player ≈ 99.5 %"),
    "plinko": ("Plinko", "🔻", "Return to player 87.5 %"),
    "poker": ("Poker", "♠️", "Texas Hold'em against other players"),
}
DEFAULT_TABLES = "Low stakes | 5 | 10 | 200 | 2000\nHigh stakes | 50 | 100 | 2000 | 20000"
TABLE_LINE = re.compile(r"^\s*([^|]{1,40}?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$")
POLL_SECONDS = 20                   # a table page waits this long for a change before asking again


RISK_CHOICES = [app_commands.Choice(name=r.title(), value=r) for r in games.PLINKO_RISKS]
ROULETTE_CHOICES = [app_commands.Choice(name=f"{label} (pays {pay}×)", value=kind)
                    for kind, (label, pay) in games.ROULETTE_BETS.items()]
COIN_CHOICES = [app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")]
AGAIN = {"plinko": ("Drop again", "🔻"), "slots": ("Spin again", "🎰"), "coinflip": ("Flip again", "🪙"),
         "dice": ("Roll again", "🎲"), "roulette": ("Spin again", "🎡")}


class PlayAgain(discord.ui.View):
    """"Play again" under a Discord game result – same bet and choices; only for the player who played."""

    def __init__(self, casino: "Casino", user_id: int, game: str, *args) -> None:
        super().__init__(timeout=300)
        self.casino, self.user_id, self.game, self.args = casino, user_id, game, args
        self.again.label, self.again.emoji = AGAIN[game]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(f"Use /{self.game} to play your own round.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Play again", style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.game == "plinko":
            await self.casino.discord_plinko(interaction, *self.args)
        else:
            await self.casino.discord_quick(interaction, self.game, *self.args)


PlinkoAgain = PlayAgain


class BlackjackView(discord.ui.View):
    """Hit / Stand / Double / Split under a /blackjack table (Deal again when the hand is over)."""

    def __init__(self, casino: "Casino", user_id: int, state: dict, bet: int) -> None:
        super().__init__(timeout=900)
        self.casino, self.user_id, self.step, self.bet = casino, user_id, state["step"], bet
        allowed = games.bj_actions(state)
        for action, label, style in (("hit", "Hit", discord.ButtonStyle.primary),
                                     ("stand", "Stand", discord.ButtonStyle.secondary),
                                     ("double", "Double", discord.ButtonStyle.success),
                                     ("split", "Split", discord.ButtonStyle.success)):
            if action in allowed:
                self.add_item(self._button(label, style, action))
        if state["finished"]:
            self.add_item(self._button("Deal again", discord.ButtonStyle.success, "deal", emoji="🃏"))

    def _button(self, label: str, style, action: str, emoji: str | None = None) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, emoji=emoji)

        async def press(interaction: discord.Interaction) -> None:
            await self.casino.blackjack_press(interaction, action, self.step, self.bet)
        button.callback = press
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Use /blackjack to play your own hand.", ephemeral=True)
            return False
        return True


class Casino(Plugin):
    NAME = "Casino"
    ICON = "🎰"
    DESCRIPTION = ("Players gamble their points on the player website: slots, coin flip and dice (95 % RTP), "
                   "European roulette (97.3 %), blackjack with real rules (≈ 99.5 %), Plinko (87.5 %) and poker tables. Play money only – points "
                   "can't be bought or cashed out. Needs the Player website and player login.")
    SETTINGS = (
        Setting("min_bet", "Smallest bet", "int", 10, min=1, max=1_000_000),
        Setting("max_bet", "Biggest bet", "int", 5000, min=1, max=100_000_000,
                help="Per game (blackjack: per hand before doubling or splitting)."),
        Setting("daily_loss_limit", "Daily loss limit per player", "int", 0, min=0, max=1_000_000_000,
                help="Points a player may lose in total per day (UTC). 0 = no limit. (Poker buy-ins don't count.)"),
        *(Setting(gid, f"{icon} {name}", "bool", True) for gid, (name, icon, _) in GAMES.items()),
        Setting("discord_games", "Casino games on Discord", "bool", True,
                help="/slots, /coinflip, /dice, /roulette and /blackjack on Discord – embeds with an animated picture "
                     "(blackjack with buttons). Same games, odds, bets and limits as the website."),
        Setting("plinko_discord", "🔻 Plinko on Discord (/plinko)", "bool", True,
                help="Players can also drop Plinko balls on Discord with /plinko – an animated board in an embed."),
        Setting("plinko_max_balls", "Plinko: most balls per drop", "int", 20, min=1, max=100,
                help="Players can drop several balls at once; each ball is one bet."),
        Setting("poker_tables", "Poker tables", "textarea", DEFAULT_TABLES,
                help="One table per line: name | small blind | big blind | smallest buy-in | biggest buy-in. "
                     "Up to 6 players per table. Changing a table stands its players up (chips go back)."),
        Setting("poker_turn_time", "Poker: seconds per turn", "int", 30, min=10, max=120,
                help="Who runs out of time checks or folds and sits out until he's back."),
        Setting("poker_hand_delay", "Poker: seconds between hands", "int", 8, min=3, max=60,
                help="The dealer deals the next hand by this timer (also the first one, once 2 players sit)."),
        Setting("poker_rake", "Poker: house cut (rake) in %", "int", 0, min=0, max=10,
                help="Taken from every pot that saw a flop and was played by 2+ players. 0 = none "
                     "(players only win from each other)."),
        Setting("poker_rake_cap", "Poker: biggest cut per hand", "int", 0, min=0, max=10_000_000,
                help="0 = no cap."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tables: dict[str, poker.Table] = {}
        self._changed: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------ Setup

    def enabled_games(self) -> list[str]:
        return [gid for gid in GAMES if self.setting(gid)]

    def problems(self) -> list[str]:
        out = [f"Poker tables, line {n}: {line.strip()[:40]} – use name | small blind | big blind | min | max "
               "buy-in (big blind ≥ small blind, min ≤ max)." for n, line in self._bad_table_lines()]
        if not self.points_enabled:
            out.append("Points & shop are switched off – the casino plays with points.")
        if self.plugin("website") is None:
            out.append("Switch on the 🌍 Player website – the casino is a page of it.")
        accounts = self.bot.get_cog("PlayerAccounts")
        if accounts is None or not accounts.enabled:
            out.append("Player login (PLAYER_ACCOUNTS) is off – players log in to play.")
        if self.setting("min_bet") > self.setting("max_bet"):
            out.append("The smallest bet is bigger than the biggest bet.")
        return out

    def status(self) -> dict:
        wagered = paid = rounds = 0
        for gid in GAMES:
            s = self.data.get(f"stats:{gid}", {})
            wagered, paid, rounds = wagered + s.get("wagered", 0), paid + s.get("paid", 0), rounds + s.get("rounds", 0)
        seated = sum(1 for t in self.tables.values() for s in t.seats if s is not None)
        return {"Rounds played": rounds, "Points bet": wagered, "Points paid out": paid,
                "House profit": wagered - paid, "Players at poker tables": seated,
                "Poker rake": int(self.data.get("poker_rake_total", 0))}

    # ------------------------------------------------------------ Poker tables

    async def on_load(self) -> None:
        self._refund_saved()
        self._build_tables()

    async def on_unload(self) -> None:
        for t in self.tables.values():
            self._cash_out(t, t.refund_all(), "table closed")
        self.tables = {}
        self.data.delete("poker")

    async def on_settings_changed(self) -> None:
        self._build_tables()

    def _table_specs(self) -> dict[str, tuple]:
        specs = {}
        for line in str(self.setting("poker_tables") or "").splitlines():
            m = TABLE_LINE.match(line)
            if not m:
                continue
            name, sb, bb, low, high = m[1], *(int(x) for x in m.groups()[1:])
            tid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "table"
            if 0 < sb <= bb and 0 < low <= high and tid not in specs:
                specs[tid] = (name, sb, bb, low, high)
        return specs

    def _bad_table_lines(self) -> list[tuple[int, str]]:
        bad = []
        for n, line in enumerate(str(self.setting("poker_tables") or "").splitlines(), 1):
            m = TABLE_LINE.match(line) if line.strip() else True
            if m is None or (m is not True and not (0 < int(m[2]) <= int(m[3]) and 0 < int(m[4]) <= int(m[5]))):
                bad.append((n, line))
        return bad

    def _build_tables(self) -> None:
        """Tables from the settings. A table whose settings changed (or that was removed) stands its players up."""
        specs = self._table_specs()
        turn, rake, cap = self.setting("poker_turn_time"), self.setting("poker_rake"), self.setting("poker_rake_cap")
        delay = self.setting("poker_hand_delay")
        for tid, t in list(self.tables.items()):
            spec = specs.get(tid)
            same = spec is not None and (t.name, t.sb, t.bb, t.min_buy, t.max_buy) == spec
            if same:
                t.turn_time, t.rake_pct, t.rake_cap, t.hand_delay = turn, rake, cap, delay
                continue
            self._cash_out(t, t.refund_all(), "table changed")
            self._notify(tid)
            del self.tables[tid]
        for tid, (name, sb, bb, low, high) in specs.items():
            if tid not in self.tables:
                self.tables[tid] = poker.Table(id=tid, name=name, sb=sb, bb=bb, min_buy=low, max_buy=high,
                                               turn_time=turn, rake_pct=rake, rake_cap=cap, hand_delay=delay)
        self._save()

    def _refund_saved(self) -> None:
        """After a crash/restart: every player gets back his chips, also those in an unfinished hand."""
        for raw in (self.data.get("poker") or {}).values():
            try:
                t = poker.Table.from_dict(raw)
            except (TypeError, ValueError, KeyError):
                self.log.warning("Could not read a saved poker table")
                continue
            self._cash_out(t, t.refund_all(), "table closed (restart)")
        self.data.delete("poker")

    def _cash_out(self, t: poker.Table, payouts, why: str) -> None:
        for user_id, amount in payouts:
            if amount > 0:
                self.bot.db.add_points(user_id, amount, f"{self.NAME}: poker {why} ({t.name})")

    def _after(self, t: poker.Table) -> None:
        """After every change: pay out who left, count the rake, save, wake the waiting pages."""
        if t.cashouts:
            self._cash_out(t, t.cashouts, "chips back")
            t.cashouts = []
        if t.rake_total:
            self.data.set("poker_rake_total", int(self.data.get("poker_rake_total", 0)) + t.rake_total)
            t.rake_total = 0
        self._save()
        self._notify(t.id)

    def _save(self) -> None:
        self.data.set("poker", {tid: t.to_dict() for tid, t in self.tables.items()})

    def _notify(self, tid: str) -> None:
        event = self._changed.pop(tid, None)
        if event is not None:
            event.set()

    @tasks.loop(seconds=1)
    async def poker_clock(self) -> None:
        now = time.time()
        for t in list(self.tables.values()):
            try:
                if t.tick(now):
                    self._after(t)
            except Exception:
                self.log.exception("Poker table %s", t.id)

    def table(self, tid: str) -> poker.Table:
        t = self.tables.get(tid)
        if t is None or "poker" not in self.enabled_games():
            raise GameError("This table doesn't exist.")
        return t

    def poker_sit(self, user_id: int, name: str, tid: str, buy_in: int) -> None:
        t = self.table(tid)
        if not self.points_enabled:
            raise GameError("The casino is closed right now.")
        elsewhere = next((o for o in self.tables.values() if o.seat_of(user_id) is not None), None)
        if elsewhere is not None and elsewhere is not t:
            raise GameError(f"You're already sitting at {elsewhere.name} – leave it first.")
        if not t.min_buy <= buy_in <= t.max_buy:
            raise GameError(f"Buy in with {self.fmt_points(t.min_buy)} – {self.fmt_points(t.max_buy)}.")
        if self.take_points(user_id, buy_in, f"poker buy-in ({t.name})") is None:
            raise GameError(f"Not enough points – you have {self.fmt_points(self.balance(user_id))}.")
        try:
            t.sit(user_id, name, buy_in, time.time())
        except PokerError as exc:
            self.bot.db.add_points(user_id, buy_in, f"{self.NAME}: poker buy-in back ({t.name})")
            raise GameError(str(exc)) from None
        self._after(t)

    def poker_leave(self, user_id: int, tid: str) -> int:
        t = self.table(tid)
        try:
            chips = t.leave(user_id, time.time())
        except PokerError as exc:
            raise GameError(str(exc)) from None
        self._cash_out(t, [(user_id, chips)], "cash-out")
        self._after(t)
        return chips

    def poker_act(self, user_id: int, tid: str, action: str, amount: int = 0) -> None:
        t = self.table(tid)
        try:
            if action == "back":
                t.set_back(user_id, True)
            elif action == "away":
                t.set_back(user_id, False)
            else:
                t.act(user_id, action, amount, time.time())
        except PokerError as exc:
            raise GameError(str(exc)) from None
        self._after(t)

    def poker_view(self, user_id: int, tid: str) -> dict:
        return self.table(tid).view(user_id, time.time())

    async def poker_wait(self, tid: str, version: int, timeout: float = POLL_SECONDS) -> None:
        """Waits until the table changes (long polling – the page asks again right after)."""
        t = self.table(tid)
        if t.version != version:
            return
        event = self._changed.setdefault(tid, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ Money

    def balance(self, user_id: int) -> int:
        return int(self.bot.db.wallet(user_id)["points"])

    def _check(self, game: str, user_id: int, bet: int, *, extra: bool = False) -> None:
        if not self.points_enabled:
            raise GameError("The casino is closed right now.")
        if game not in self.enabled_games():
            raise GameError("This game is closed.")
        if not extra:
            low, high = self.setting("min_bet"), self.setting("max_bet")
            if not low <= bet <= high:
                raise GameError(f"Bets are between {self.fmt_points(low)} and {self.fmt_points(high)}.")
        limit = self.setting("daily_loss_limit")
        if limit and self._lost_today(user_id) + bet > limit:
            raise GameError(f"You reached today's loss limit ({self.fmt_points(limit)}). Come back tomorrow!")

    def _take(self, game: str, user_id: int, bet: int, *, extra: bool = False) -> None:
        self._check(game, user_id, bet, extra=extra)
        if self.take_points(user_id, bet, f"{GAMES[game][0]} bet") is None:
            raise GameError(f"Not enough points – you have {self.fmt_points(self.balance(user_id))}.")
        self._track_loss(user_id, bet)
        self._stats(game, wagered=bet)

    def _pay(self, game: str, user_id: int, payout: int, *, round_over: bool = True) -> None:
        if payout > 0:
            self.bot.db.add_points(user_id, payout, f"{self.NAME}: {GAMES[game][0]} win")
            self._track_loss(user_id, -payout)
        self._stats(game, paid=payout, rounds=1 if round_over else 0)

    def _stats(self, game: str, *, wagered: int = 0, paid: int = 0, rounds: int = 0) -> None:
        key = f"stats:{game}"
        s = self.data.get(key, {})
        s = {"wagered": s.get("wagered", 0) + wagered, "paid": s.get("paid", 0) + paid,
             "rounds": s.get("rounds", 0) + rounds}
        self.data.set(key, s)

    @staticmethod
    def _today() -> int:
        return int(time.time() // 86400)

    def _lost_today(self, user_id: int) -> int:
        row = self.data.get(f"loss:{user_id}", {})
        return int(row.get("net", 0)) if row.get("day") == self._today() else 0

    def _track_loss(self, user_id: int, delta: int) -> None:
        if not self.setting("daily_loss_limit"):
            return
        self.data.set(f"loss:{user_id}", {"day": self._today(), "net": self._lost_today(user_id) + delta})

    # ------------------------------------------------------------ Quick games

    def play_slots(self, user_id: int, bet: int) -> games.SlotSpin:
        self._take("slots", user_id, bet)
        spin = games.spin_slots(bet)
        self._pay("slots", user_id, spin.payout)
        return spin

    def play_coinflip(self, user_id: int, bet: int, pick: str) -> tuple[str, int]:
        if pick not in ("heads", "tails"):
            raise GameError("Pick heads or tails.")
        self._take("coinflip", user_id, bet)
        side, payout = games.flip_coin(bet, pick)
        self._pay("coinflip", user_id, payout)
        return side, payout

    def play_dice(self, user_id: int, bet: int, target: int) -> tuple[int, int]:
        if not games.DICE_MIN <= target <= games.DICE_MAX:
            raise GameError(f"Pick a win chance between {games.DICE_MIN} and {games.DICE_MAX} %.")
        self._take("dice", user_id, bet)
        roll, payout = games.roll_dice(bet, target)
        self._pay("dice", user_id, payout)
        return roll, payout

    def play_roulette(self, user_id: int, bet: int, kind: str, number: int | None) -> tuple[int, int]:
        if kind not in games.ROULETTE_BETS:
            raise GameError("Pick a bet.")
        if kind == "number" and (number is None or not 0 <= number <= 36):
            raise GameError("Pick a number from 0 to 36.")
        self._take("roulette", user_id, bet)
        pocket, payout = games.spin_roulette(bet, kind, number)
        self._pay("roulette", user_id, payout)
        return pocket, payout

    def play_plinko(self, user_id: int, bet: int, risk: str, rows: int, balls: int = 1) -> list[dict]:
        """Drops `balls` balls of `bet` points each; the server decides every path."""
        if risk not in games.PLINKO_RISKS:
            raise GameError("Pick a risk: low, medium or high.")
        if rows not in games.PLINKO_ROWS:
            raise GameError("Pick 8 to 16 rows.")
        most = self.setting("plinko_max_balls")
        if not 1 <= balls <= most:
            raise GameError(f"Drop 1 to {most} balls at once.")
        self._check("plinko", user_id, bet)                     # the bet range is per ball
        self._take("plinko", user_id, bet * balls, extra=True)  # the loss limit counts all of them
        drops = [games.plinko_drop(bet, risk, rows) for _ in range(balls)]
        payout = sum(d["payout"] for d in drops)
        self._pay("plinko", user_id, payout, round_over=False)
        self._stats("plinko", rounds=balls)
        return drops

    # ------------------------------------------------------------ Plinko on Discord

    @app_commands.command(name="plinko", description="Drop Plinko balls with your points (87.5 % return to player).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points per ball", balls="How many balls at once (each one is a bet)",
                           risk="Higher risk: bigger edges, smaller middle", rows="Rows of pegs (8–16)")
    @app_commands.choices(risk=RISK_CHOICES)
    async def plinko_command(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000],
                             balls: app_commands.Range[int, 1, 100] = 1, risk: str = "medium",
                             rows: app_commands.Range[int, 8, 16] = 16) -> None:
        await self.discord_plinko(interaction, bet, balls, risk, rows)

    def _plinko_embed(self, user, bet: int, balls: int, risk: str, rows: int, drops: list[dict] | None):
        head = (f"**{discord.utils.escape_markdown(user.display_name)}** · {balls} × {self.fmt_points(bet)} · "
                f"{risk.title()} risk · {rows} rows")
        embed = discord.Embed(title="🔻 Plinko", colour=discord.Colour(0x2F4553), description=head + "\n\nDropping …")
        if drops is not None:
            total, staked = sum(d["payout"] for d in drops), bet * len(drops)
            hits = " · ".join(f"**{games.fmt_multiplier(d['cents'])}×**" for d in drops[:30])
            more = f" … (+{len(drops) - 30})" if len(drops) > 30 else ""
            net = total - staked
            line = (f"🎉 You won **{self.fmt_points(total)}** (+{self.fmt_points(net)})" if net > 0 else
                    "Bet back" if net == 0 else f"Back: **{self.fmt_points(total)}** ({self.fmt_points(-net)} lost)")
            embed.description = f"{head}\n\n{hits}{more}\n\n{line}"
            embed.colour = discord.Colour.green() if net > 0 else discord.Colour.red() if net < 0 else embed.colour
            embed.set_footer(text=f"Balance: {self.fmt_points(self.balance(user.id))} · return to player 87.5 %")
        embed.set_image(url="attachment://plinko.gif")
        return embed

    async def discord_plinko(self, interaction: discord.Interaction, bet: int, balls: int, risk: str,
                             rows: int) -> None:
        """/plinko and "Drop again": takes the bets, then shows the drop as an animated board in an embed and adds
        the result once the balls have landed (the result is already decided and paid before)."""
        from support.plinko_image import plinko_gif
        if not self.setting("plinko_discord"):
            await interaction.response.send_message("Plinko on Discord is switched off.", ephemeral=True)
            return
        try:
            drops = self.play_plinko(interaction.user.id, bet, risk, rows, balls)
        except GameError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        user = interaction.user
        try:
            data, seconds = await asyncio.to_thread(plinko_gif, risk, rows, [d["path"] for d in drops])
            msg = await interaction.followup.send(embed=self._plinko_embed(user, bet, balls, risk, rows, None),
                                                  file=discord.File(io.BytesIO(data), "plinko.gif"), wait=True)
        except Exception as exc:          # the bets are settled already – at least show the result as text
            self.log.warning("Plinko picture: %s", exc)
            embed = self._plinko_embed(user, bet, balls, risk, rows, drops)
            embed.set_image(url=None)
            await interaction.followup.send(embed=embed)
            return
        await asyncio.sleep(seconds + 0.3)
        try:
            await msg.edit(embed=self._plinko_embed(user, bet, balls, risk, rows, drops),
                           view=PlayAgain(self, user.id, "plinko", bet, balls, risk, rows))
        except discord.HTTPException as exc:
            self.log.debug("Plinko result: %s", exc)

    # ------------------------------------------------------------ The other games on Discord

    def _outcome_line(self, payout: int, staked: int) -> tuple[str, discord.Colour]:
        net = payout - staked
        if net > 0:
            return f"🎉 You won **{self.fmt_points(payout)}** (+{self.fmt_points(net)})", discord.Colour.green()
        if net == 0:
            return "Bet back", discord.Colour(0x2F4553)
        if payout:
            return f"**{self.fmt_points(payout)}** back ({self.fmt_points(-net)} lost)", discord.Colour.red()
        return f"No luck – {self.fmt_points(staked)} lost", discord.Colour.red()

    def _game_embed(self, game: str, user, head: str, body: str | None, payout: int = 0, staked: int = 0,
                    image: str = "game.gif") -> discord.Embed:
        name, icon, _ = GAMES[game]
        who = f"**{discord.utils.escape_markdown(user.display_name)}** · {head}"
        embed = discord.Embed(title=f"{icon} {name}", colour=discord.Colour(0x2F4553), description=who)
        if body is None:
            embed.description += "\n\n" + {"slots": "Spinning …", "coinflip": "Flipping …", "dice": "Rolling …",
                                            "roulette": "No more bets – the wheel is spinning …"}.get(game, "…")
        else:
            line, colour = self._outcome_line(payout, staked)
            embed.description += f"\n\n{body}\n\n{line}"
            embed.colour = colour
            embed.set_footer(text=f"Balance: {self.fmt_points(self.balance(user.id))}")
        if image:
            embed.set_image(url=f"attachment://{image}")
        return embed

    @app_commands.command(name="slots", description="Spin the slot machine with your points (95 % return to player).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points to bet")
    async def slots_command(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000]):
        await self.discord_quick(interaction, "slots", bet)

    @app_commands.command(name="coinflip", description="Flip a coin – the right side pays 1.9× (95 % RTP).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points to bet", side="Heads or tails")
    @app_commands.choices(side=COIN_CHOICES)
    async def coinflip_command(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000],
                               side: str = "heads"):
        await self.discord_quick(interaction, "coinflip", bet, side)

    @app_commands.command(name="dice", description="Roll 1–100: at or under your win chance wins (95 % RTP).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points to bet", chance=f"Win chance in % ({games.DICE_MIN}–{games.DICE_MAX}) – "
                                                       "pays 95 ÷ chance × your bet")
    async def dice_command(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000],
                           chance: app_commands.Range[int, games.DICE_MIN, games.DICE_MAX] = 50):
        await self.discord_quick(interaction, "dice", bet, chance)

    @app_commands.command(name="roulette", description="European roulette with your points (97.3 % RTP).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points to bet", on="What you bet on", number="Your number (only for a single number)")
    @app_commands.choices(on=ROULETTE_CHOICES)
    async def roulette_command(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000],
                               on: str, number: app_commands.Range[int, 0, 36] | None = None):
        await self.discord_quick(interaction, "roulette", bet, on, number)

    async def discord_quick(self, interaction: discord.Interaction, game: str, bet: int, *choice) -> None:
        """Slots, coin flip, dice, roulette on Discord: bet settled first, then the animation, then the result."""
        from support import casino_images as pics
        if not self.setting("discord_games"):
            await interaction.response.send_message("Casino games on Discord are switched off.", ephemeral=True)
            return
        uid, user = interaction.user.id, interaction.user
        try:
            if game == "slots":
                spin = self.play_slots(uid, bet)
                payout = spin.payout
                render = (pics.slots_gif, spin.reels, payout > 0)
                head = f"bet {self.fmt_points(bet)}"
                body = f"# {' '.join(spin.reels)}"
            elif game == "coinflip":
                pick = choice[0]
                side, payout = self.play_coinflip(uid, bet, pick)
                render = (pics.coin_gif, side, payout > 0)
                head = f"{self.fmt_points(bet)} on **{pick.title()}**"
                body = f"It's **{side.title()}**!"
            elif game == "dice":
                target = choice[0]
                roll, payout = self.play_dice(uid, bet, target)
                render = (pics.dice_gif, roll, target, payout > 0)
                pays = games.fmt_multiplier(round(9500 / target))
                head = f"{self.fmt_points(bet)} · {target} % chance (pays {pays}×)"
                body = f"Rolled **{roll}** – {target} or under wins."
            elif game == "roulette":
                kind, number = choice
                pocket, payout = self.play_roulette(uid, bet, kind, number)
                render = (pics.roulette_gif, pocket, payout > 0)
                label = games.ROULETTE_BETS.get(kind, (kind, 0))[0] + (f" {number}" if kind == "number" else "")
                head = f"{self.fmt_points(bet)} on **{label}**"
                colour = games.roulette_colour(pocket)
                body = f"**{pocket} {colour.title()}** {'🟢' if colour == 'green' else '🔴' if colour == 'red' else '⚫'}"
            else:
                raise GameError("Unknown game.")
        except GameError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            data, seconds = await asyncio.to_thread(*render)
            msg = await interaction.followup.send(embed=self._game_embed(game, user, head, None),
                                                  file=discord.File(io.BytesIO(data), "game.gif"), wait=True)
        except Exception as exc:          # the bet is settled already – at least show the result as text
            self.log.warning("%s picture: %s", game, exc)
            await interaction.followup.send(embed=self._game_embed(game, user, head, body, payout, bet, image=""))
            return
        await asyncio.sleep(seconds + 0.3)
        try:
            await msg.edit(embed=self._game_embed(game, user, head, body, payout, bet),
                           view=PlayAgain(self, uid, game, bet, *choice))
        except discord.HTTPException as exc:
            self.log.debug("%s result: %s", game, exc)

    # ------------------------------------------------------------ Blackjack on Discord (buttons)

    @app_commands.command(name="blackjack", description="Blackjack with real casino rules (≈ 99.5 % RTP).")
    @app_commands.guild_only()
    @app_commands.describe(bet="Points to bet (a hand you didn't finish is shown again instead)")
    async def blackjack_command(self, interaction: discord.Interaction,
                                bet: app_commands.Range[int, 1, 100_000_000]) -> None:
        if not self.setting("discord_games"):
            await interaction.response.send_message("Casino games on Discord are switched off.", ephemeral=True)
            return
        state = self.bj_state(interaction.user.id)
        note = ""
        if state is not None and not state["finished"]:
            note = "Your unfinished hand:"                  # one hand at a time (also on the website)
            bet = state["hands"][0]["bet"]
        else:
            try:
                state = self.bj_start(interaction.user.id, bet)
            except GameError as exc:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
                return
        embed, file = await self._blackjack_message(interaction.user, state, note)
        await interaction.response.send_message(embed=embed, file=file,
                                                view=BlackjackView(self, interaction.user.id, state, bet))

    async def _blackjack_message(self, user, state: dict, note: str = "") -> tuple[discord.Embed, discord.File]:
        from support.casino_images import blackjack_png
        data = await asyncio.to_thread(blackjack_png, state, user.display_name[:20])
        staked = games.bj_wagered(state)
        head = f"bet {self.fmt_points(staked)}" + (f" · {note}" if note else "")
        if state["finished"]:
            words = {"win": "Win", "blackjack": "Blackjack!", "push": "Push", "lose": "Lose", "bust": "Bust"}
            body = " · ".join(f"{'Hand ' + str(i + 1) + ': ' if len(state['hands']) > 1 else ''}"
                              f"**{words.get(h.get('result'), '?')}**" for i, h in enumerate(state["hands"]))
            embed = self._game_embed("blackjack", user, head, body, state.get("payout", 0), staked,
                                     image="table.png")
        else:
            moves = ", ".join(a.title() for a in games.bj_actions(state))
            embed = self._game_embed("blackjack", user, head, None, image="table.png")
            embed.description = embed.description.rsplit("\n\n", 1)[0] + f"\n\nYour move: {moves}"
        return embed, discord.File(io.BytesIO(data), "table.png")

    async def blackjack_press(self, interaction: discord.Interaction, action: str, step: int, bet: int) -> None:
        try:
            if action == "deal":
                state = self.bj_start(interaction.user.id, bet)
            else:
                state = self.bj_action(interaction.user.id, action, step)
        except GameError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        embed, file = await self._blackjack_message(interaction.user, state)
        await interaction.response.edit_message(embed=embed, attachments=[file],
                                                view=BlackjackView(self, interaction.user.id, state, bet))

    # ------------------------------------------------------------ Blackjack (the hand is kept between clicks)

    def bj_state(self, user_id: int) -> dict | None:
        return self.data.get(f"bj:{user_id}")

    def bj_start(self, user_id: int, bet: int) -> dict:
        state = self.bj_state(user_id)
        if state is not None and not state["finished"]:
            raise GameError("Finish your hand first.")
        self._take("blackjack", user_id, bet)
        state = games.bj_deal(bet)
        self._finish_if_done(user_id, state)
        self.data.set(f"bj:{user_id}", state)
        return state

    def bj_action(self, user_id: int, action: str, step: int) -> dict:
        state = self.bj_state(user_id)
        if state is None or state["finished"]:
            raise GameError("There is no hand running – deal a new one.")
        if step != state["step"]:
            raise GameError("That button was from an older page – here is your hand.")
        if action not in games.bj_actions(state):
            raise GameError("You can't do that now.")
        extra = games.bj_extra_bet(state, action)
        if extra:
            self._take("blackjack", user_id, extra, extra=True)
        games.bj_act(state, action)
        self._finish_if_done(user_id, state)
        self.data.set(f"bj:{user_id}", state)
        return state

    def _finish_if_done(self, user_id: int, state: dict) -> None:
        if state["finished"]:
            self._pay("blackjack", user_id, state["payout"])
