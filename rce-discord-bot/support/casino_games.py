"""Casino games played with points on the player website (plugins/casino.py, web/casino_pages.py).

Pure game logic, no database: every function gets the bet and a random source and returns what is paid back
(0 = lost; the bet itself is taken before). Return to player (RTP) – checked exactly in tests/test_casino.py:

    Slots      95 %      (paytable below: 9 500 of every 10 000 points come back on average)
    Coin flip  95 %      (wins pay 1.9×)
    Dice       95 %      (win chance X % pays 95/X ×)
    Roulette   97.3 %    (European wheel, one zero – the real thing: 36/37)
    Blackjack  ≈ 99.5 %  (real rules: 6 decks, dealer stands on soft 17 and peeks, blackjack pays 3:2, double on
                          any two cards and after a split, one split, split aces get one card)
    Plinko     97.5 %    (every risk and row count – paytables below)

Points are whole numbers, so a payout like 28.5 is rounded fairly: 28, plus 1 more with a 50 % chance – on average
exactly the paytable, the RTP isn't shaved by rounding.
"""
from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from fractions import Fraction

RNG = secrets.SystemRandom()          # unpredictable – players can't work out the next result


def fair(numerator: int, denominator: int, rng: random.Random = RNG) -> int:
    """numerator / denominator in whole points: rounded down, plus 1 with the chance of the remainder."""
    whole, rest = divmod(numerator, denominator)
    return whole + (1 if rest and rng.randrange(denominator) < rest else 0)

# ------------------------------------------------------------ Slots

# (symbols shown, pays × bet, weight out of SLOT_TOTAL). Sum of pays × weight = 9 500 -> RTP exactly 95 %.
SLOT_WINS = (
    ("💎💎💎", 50, 10),
    ("7️⃣7️⃣7️⃣", 20, 40),
    ("🔔🔔🔔", 10, 100),
    ("🍋🍋🍋", 5, 300),
    ("🍒🍒🍒", 3, 600),
    ("🍒🍒", 2, 1950),               # any two cherries
)
SLOT_TOTAL = 10_000
SLOT_SYMBOLS = ("🍒", "🍋", "🔔", "7️⃣", "💎", "⭐")
SLOT_RTP = sum(pay * weight for _, pay, weight in SLOT_WINS) / SLOT_TOTAL


@dataclass
class SlotSpin:
    reels: list[str]
    multiplier: int
    payout: int


def spin_slots(bet: int, rng: random.Random = RNG) -> SlotSpin:
    roll = rng.randrange(SLOT_TOTAL)
    for symbols, pay, weight in SLOT_WINS:
        if roll < weight:
            return SlotSpin(_reels_for(symbols, rng), pay, bet * pay)
        roll -= weight
    return SlotSpin(_losing_reels(rng), 0, 0)


def _reels_for(symbols: str, rng: random.Random) -> list[str]:
    if symbols == "🍒🍒":             # two cherries and something else, in any order
        reels = ["🍒", "🍒", rng.choice([s for s in SLOT_SYMBOLS if s != "🍒"])]
        rng.shuffle(reels)
        return reels
    symbol = next(s for s in SLOT_SYMBOLS if symbols.startswith(s))
    return [symbol] * 3


def _losing_reels(rng: random.Random) -> list[str]:
    while True:                       # anything that isn't a win: not three alike, at most one cherry
        reels = [rng.choice(SLOT_SYMBOLS) for _ in range(3)]
        if len(set(reels)) > 1 and reels.count("🍒") < 2:
            return reels


# ------------------------------------------------------------ Coin flip

COINFLIP_PAYS = 1.9                    # 50 % × 1.9 = 95 %


def flip_coin(bet: int, pick: str, rng: random.Random = RNG) -> tuple[str, int]:
    """pick 'heads' or 'tails' -> (side shown, payout)."""
    side = rng.choice(("heads", "tails"))
    return side, fair(bet * 19, 10, rng) if side == pick else 0


# ------------------------------------------------------------ Dice

DICE_MIN, DICE_MAX = 2, 90             # win chance in %


def dice_multiplier(target: int) -> float:
    return 95 / target


def roll_dice(bet: int, target: int, rng: random.Random = RNG) -> tuple[int, int]:
    """Roll 1–100, win when the roll is at most `target` (= the win chance in %). -> (roll, payout)."""
    if not DICE_MIN <= target <= DICE_MAX:
        raise ValueError(f"Pick a win chance between {DICE_MIN} and {DICE_MAX} %.")
    roll = rng.randint(1, 100)
    return roll, (fair(bet * 95, target, rng) if roll <= target else 0)


# ------------------------------------------------------------ Roulette (European, single zero)

RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
ROULETTE_BETS = {                      # bet -> (label, pays back × bet incl. the bet)
    "red": ("Red", 2), "black": ("Black", 2), "odd": ("Odd", 2), "even": ("Even", 2),
    "low": ("1–18", 2), "high": ("19–36", 2),
    "dozen1": ("1st 12", 3), "dozen2": ("2nd 12", 3), "dozen3": ("3rd 12", 3),
    "col1": ("Column 1", 3), "col2": ("Column 2", 3), "col3": ("Column 3", 3),
    "number": ("Single number", 36),
}


def roulette_colour(n: int) -> str:
    return "green" if n == 0 else "red" if n in RED else "black"


def roulette_wins(kind: str, number: int | None, n: int) -> bool:
    if n == 0:
        return kind == "number" and number == 0
    return {
        "red": n in RED, "black": n not in RED, "odd": n % 2 == 1, "even": n % 2 == 0,
        "low": n <= 18, "high": n >= 19,
        "dozen1": n <= 12, "dozen2": 13 <= n <= 24, "dozen3": n >= 25,
        "col1": n % 3 == 1, "col2": n % 3 == 2, "col3": n % 3 == 0,
        "number": n == number,
    }[kind]


def spin_roulette(bet: int, kind: str, number: int | None = None, rng: random.Random = RNG) -> tuple[int, int]:
    """-> (pocket 0–36, payout)."""
    if kind not in ROULETTE_BETS:
        raise ValueError("Pick a bet.")
    if kind == "number" and (number is None or not 0 <= number <= 36):
        raise ValueError("Pick a number from 0 to 36.")
    n = rng.randint(0, 36)
    return n, (bet * ROULETTE_BETS[kind][1] if roulette_wins(kind, number, n) else 0)


# ------------------------------------------------------------ Blackjack

DECKS = 6
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("♠", "♥", "♦", "♣")


class GameError(Exception):
    """Shown to the player."""


def card_label(card: int) -> str:
    return RANKS[card % 13] + SUITS[(card // 13) % 4]


def card_value(card: int) -> int:
    rank = card % 13
    return 11 if rank == 0 else min(rank + 1, 10)


def hand_total(cards: list[int]) -> tuple[int, bool]:
    """(best total, soft) – soft = an ace still counts 11."""
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c % 13 == 0)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(cards: list[int]) -> bool:
    return len(cards) == 2 and hand_total(cards)[0] == 21


def new_shoe(rng: random.Random = RNG) -> list[int]:
    shoe = list(range(52 * DECKS))
    rng.shuffle(shoe)
    return shoe


def bj_deal(bet: int, rng: random.Random = RNG) -> dict:
    """A new round. The state is plain JSON (kept between clicks). Check state['finished']."""
    shoe = new_shoe(rng)
    player = [shoe.pop(), shoe.pop()]
    dealer = [shoe.pop(), shoe.pop()]
    state = {"shoe": shoe, "dealer": dealer, "active": 0, "finished": False, "step": 0, "moved": "deal",
             "hands": [{"cards": player, "bet": bet, "done": False, "doubled": False, "split_aces": False}]}
    dealer_bj, player_bj = is_blackjack(dealer), is_blackjack(player)
    if dealer_bj or player_bj:          # the dealer peeks: a blackjack on either side ends the round at once
        state["hands"][0]["done"] = True
        _settle(state)
    return state


def bj_actions(state: dict) -> list[str]:
    """What the player may do now."""
    if state["finished"]:
        return []
    hand = state["hands"][state["active"]]
    actions = ["hit", "stand"]
    if len(hand["cards"]) == 2 and not hand["split_aces"]:
        actions.append("double")
        if len(state["hands"]) == 1 and card_value(hand["cards"][0]) == card_value(hand["cards"][1]):
            actions.append("split")
    return actions


def bj_extra_bet(state: dict, action: str) -> int:
    """Points a double or split costs on top (taken before bj_act)."""
    return state["hands"][state["active"]]["bet"] if action in ("double", "split") else 0


def bj_act(state: dict, action: str) -> dict:
    if action not in bj_actions(state):
        raise GameError("You can't do that now.")
    hands, shoe = state["hands"], state["shoe"]
    hand = hands[state["active"]]
    state["step"] += 1
    state["moved"] = [0, 1] if action == "split" else [state["active"]]     # hands that got a card (animation)
    if action == "hit":
        hand["cards"].append(shoe.pop())
        if hand_total(hand["cards"])[0] >= 21:
            hand["done"] = True
    elif action == "stand":
        hand["done"] = True
    elif action == "double":
        hand["bet"] *= 2
        hand["doubled"] = True
        hand["cards"].append(shoe.pop())
        hand["done"] = True
    elif action == "split":
        first, second = hand["cards"]
        aces = first % 13 == 0
        hands[:] = [{"cards": [first, shoe.pop()], "bet": hand["bet"], "done": aces, "doubled": False,
                     "split_aces": aces},
                    {"cards": [second, shoe.pop()], "bet": hand["bet"], "done": aces, "doubled": False,
                     "split_aces": aces}]
        for h in hands:                 # 21 after a split stands by itself (it isn't a blackjack)
            if hand_total(h["cards"])[0] == 21:
                h["done"] = True
    _advance(state)
    return state


def _advance(state: dict) -> None:
    for i, hand in enumerate(state["hands"]):
        if not hand["done"]:
            state["active"] = i
            return
    _settle(state)


def _settle(state: dict) -> None:
    hands, dealer, shoe = state["hands"], state["dealer"], state["shoe"]
    split = len(hands) > 1
    alive = any(hand_total(h["cards"])[0] <= 21 for h in hands)
    player_bj = not split and is_blackjack(hands[0]["cards"])
    if alive and not player_bj and not is_blackjack(dealer):
        while hand_total(dealer)[0] < 17:     # dealer draws to 17 and stands on every 17 (also soft)
            dealer.append(shoe.pop())
    dealer_total = hand_total(dealer)[0]
    dealer_bj = is_blackjack(dealer)
    for h in hands:
        total = hand_total(h["cards"])[0]
        player_bj = not split and is_blackjack(h["cards"])
        if player_bj and dealer_bj:
            h["result"], h["payout"] = "push", h["bet"]
        elif player_bj:
            h["result"], h["payout"] = "blackjack", fair(h["bet"] * 5, 2)
        elif dealer_bj:
            h["result"], h["payout"] = "lose", 0
        elif total > 21:
            h["result"], h["payout"] = "bust", 0
        elif dealer_total > 21 or total > dealer_total:
            h["result"], h["payout"] = "win", h["bet"] * 2
        elif total == dealer_total:
            h["result"], h["payout"] = "push", h["bet"]
        else:
            h["result"], h["payout"] = "lose", 0
        h["done"] = True
    state["finished"] = True
    state["payout"] = sum(h["payout"] for h in hands)
    state["shoe"] = []                  # not needed any more – keeps the saved state small


def bj_wagered(state: dict) -> int:
    return sum(h["bet"] for h in state["hands"])


# ------------------------------------------------------------ Plinko

# Like the well-known crypto-casino Plinko: a triangle of pegs (8–16 rows), the ball goes left or right at every
# row (50/50), so it lands in slot k with probability C(rows, k) / 2^rows. Multipliers are symmetric: low in the
# middle, high at the edges; "risk" makes the edges bigger and the middle smaller. Every table returns exactly
# PLINKO_RTP: the multipliers are in cents and  Σ C(rows, k) · cents_k  =  RTP · 100 · 2^rows  (tests check it).
PLINKO_RTP = Fraction(39, 40)            # 0.975
PLINKO_ROWS = range(8, 17)
PLINKO_RISKS = ("low", "medium", "high")
PLINKO_TABLES = {    # (risk, rows) -> multiplier × 100 for slots 0..rows
    ("low", 8): (580, 266, 134, 75, 52, 75, 134, 266, 580),
    ("low", 9): (666, 318, 166, 94, 60, 60, 94, 166, 318, 666),
    ("low", 10): (782, 391, 206, 118, 72, 53, 72, 118, 206, 391, 782),
    ("low", 11): (884, 456, 247, 143, 88, 60, 60, 88, 143, 247, 456, 884),
    ("low", 12): (1020, 550, 298, 174, 106, 71, 55, 71, 106, 174, 298, 550, 1020),
    ("low", 13): (1170, 642, 356, 208, 128, 84, 60, 60, 84, 128, 208, 356, 642, 1170),
    ("low", 14): (1330, 737, 423, 249, 153, 100, 70, 55, 70, 100, 153, 249, 423, 737, 1330),
    ("low", 15): (1550, 867, 499, 296, 185, 115, 81, 61, 61, 81, 115, 185, 296, 499, 867, 1550),
    ("low", 16): (1760, 1000, 585, 349, 214, 140, 95, 69, 56, 69, 95, 140, 214, 349, 585, 1000, 1760),
    ("medium", 8): (1370, 351, 126, 59, 42, 59, 126, 351, 1370),
    ("medium", 9): (1830, 496, 172, 78, 47, 47, 78, 172, 496, 1830),
    ("medium", 10): (2450, 688, 238, 102, 57, 45, 57, 102, 238, 688, 2450),
    ("medium", 11): (3260, 939, 328, 137, 71, 48, 48, 71, 137, 328, 939, 3260),
    ("medium", 12): (4260, 1270, 441, 186, 90, 56, 46, 56, 90, 186, 441, 1270, 4260),
    ("medium", 13): (5590, 1720, 600, 246, 118, 68, 48, 48, 68, 118, 246, 600, 1720, 5590),
    ("medium", 14): (7450, 2300, 809, 327, 153, 83, 56, 46, 56, 83, 153, 327, 809, 2300, 7450),
    ("medium", 15): (9590, 3050, 1080, 437, 197, 105, 66, 49, 49, 66, 105, 197, 437, 1080, 3050, 9590),
    ("medium", 16): (12700, 4050, 1460, 579, 263, 132, 78, 55, 48, 55, 78, 132, 263, 579, 1460, 4050, 12700),
    ("high", 8): (3890, 407, 86, 36, 26, 36, 86, 407, 3890),
    ("high", 9): (6240, 686, 135, 48, 29, 29, 48, 135, 686, 6240),
    ("high", 10): (10100, 1090, 214, 68, 35, 30, 35, 68, 214, 1090, 10100),
    ("high", 11): (15800, 1860, 340, 98, 45, 30, 30, 45, 98, 340, 1860, 15800),
    ("high", 12): (26200, 2980, 533, 143, 58, 36, 30, 36, 58, 143, 533, 2980, 26200),
    ("high", 13): (40300, 4860, 860, 218, 81, 43, 31, 31, 43, 81, 218, 860, 4860, 40300),
    ("high", 14): (64900, 7770, 1360, 329, 110, 55, 36, 31, 36, 55, 110, 329, 1360, 7770, 64900),
    ("high", 15): (101900, 12400, 2130, 491, 162, 75, 42, 32, 32, 42, 75, 162, 491, 2130, 12400, 101900),
    ("high", 16): (161100, 20100, 3380, 757, 233, 99, 51, 36, 32, 36, 51, 99, 233, 757, 3380, 20100, 161100),
}


def plinko_drop(bet: int, risk: str, rows: int, rng: random.Random = RNG) -> dict:
    """One ball: {"path": [0/1 per row, 1 = right], "slot", "cents" (multiplier ×100), "payout"}."""
    table = PLINKO_TABLES.get((risk, rows))
    if table is None:
        raise ValueError("Pick a risk and 8–16 rows.")
    path = [rng.randrange(2) for _ in range(rows)]
    slot = sum(path)
    return {"path": path, "slot": slot, "cents": table[slot], "payout": fair(bet * table[slot], 100, rng)}


def plinko_colour(k: int, rows: int) -> str:
    """Slot colour: red at the edges, orange, yellow in the middle (like the well-known Plinko board)."""
    x = abs(k - rows / 2) / (rows / 2)                          # 0 middle … 1 edge
    stops = ((0, (255, 192, 0)), (0.55, (255, 120, 20)), (1, (255, 0, 63)))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if x <= b:
            f = (x - a) / (b - a)
            return "#" + "".join(f"{round(p + (q - p) * f):02x}" for p, q in zip(ca, cb))
    return "#ff003f"


def fmt_multiplier(cents: int) -> str:
    """1000.00 -> "1000", 0.3 -> "0.3", 1.25 -> "1.25"."""
    whole, part = divmod(cents, 100)
    return str(whole) if not part else f"{whole}.{part:02d}".rstrip("0")
