"""Casino: players gamble their points on the player website (/casino) – slots, coin flip, dice, roulette and
blackjack. Play money only: points are earned in-game and can't be bought or cashed out.

Slots, coin flip and dice return 95 % of the bets on average, roulette is a real European wheel (97.3 %) and
blackjack is played with real casino rules (≈ 99.5 %). The games themselves are in support/casino_games.py, the
pages in web/casino_pages.py. Players log in with the website's player login (/account).
"""
from __future__ import annotations

import time

from plugin_api import Plugin, Setting
from support import casino_games as games
from support.casino_games import GameError

GAMES = {           # id -> (name, icon, RTP shown to players)
    "slots": ("Slots", "🎰", "95 %"),
    "coinflip": ("Coin flip", "🪙", "95 %"),
    "dice": ("Dice", "🎲", "95 %"),
    "roulette": ("Roulette", "🎡", "97.3 %"),
    "blackjack": ("Blackjack", "🃏", "≈ 99.5 %"),
}


class Casino(Plugin):
    NAME = "Casino"
    ICON = "🎰"
    DESCRIPTION = ("Players gamble their points on the player website: slots, coin flip and dice (95 % RTP), "
                   "European roulette (97.3 %) and blackjack with real rules (≈ 99.5 %). Play money only – points "
                   "can't be bought or cashed out. Needs the Player website and player login.")
    SETTINGS = (
        Setting("min_bet", "Smallest bet", "int", 10, min=1, max=1_000_000),
        Setting("max_bet", "Biggest bet", "int", 5000, min=1, max=100_000_000,
                help="Per game (blackjack: per hand before doubling or splitting)."),
        Setting("daily_loss_limit", "Daily loss limit per player", "int", 0, min=0, max=1_000_000_000,
                help="Points a player may lose in total per day (UTC). 0 = no limit."),
        *(Setting(gid, f"{icon} {name}", "bool", True) for gid, (name, icon, _) in GAMES.items()),
    )

    # ------------------------------------------------------------ Setup

    def enabled_games(self) -> list[str]:
        return [gid for gid in GAMES if self.setting(gid)]

    def problems(self) -> list[str]:
        out = []
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
        return {"Rounds played": rounds, "Points bet": wagered, "Points paid out": paid,
                "House profit": wagered - paid}

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
