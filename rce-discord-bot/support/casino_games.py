"""Casino games played with points on the player website (plugins/casino.py, web/casino_pages.py).

Pure game logic, no database: every function gets the bet and a random source and returns what is paid back
(0 = lost; the bet itself is taken before). Return to player (RTP) – checked exactly in tests/test_casino.py:

    Slots      95 %      (paytable below: 9 500 of every 10 000 points come back on average)
    Coin flip  95 %      (wins pay 1.9×)
    Dice       95 %      (win chance X % pays 95/X ×)
    Roulette   97.3 %    (European wheel, one zero – the real thing: 36/37)
    Blackjack  ≈ 99.5 %  (real rules: 6 decks, dealer stands on soft 17 and peeks, blackjack pays 3:2, double on
                          any two cards and after a split, one split, split aces get one card)
    Plinko     87.5 %    (every risk and row count – paytables below)

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
PLINKO_RTP = Fraction(7, 8)              # 0.875
PLINKO_ROWS = range(8, 17)
PLINKO_RISKS = ("low", "medium", "high")
PLINKO_TABLES = {    # (risk, rows) -> multiplier × 100 for slots 0..rows
    ("low", 8): (523, 240, 120, 67, 47, 67, 120, 240, 523),
    ("low", 9): (602, 286, 149, 84, 54, 54, 84, 149, 286, 602),
    ("low", 10): (702, 352, 184, 105, 65, 48, 65, 105, 184, 352, 702),
    ("low", 11): (797, 410, 221, 128, 79, 54, 54, 79, 128, 221, 410, 797),
    ("low", 12): (918, 489, 268, 154, 96, 64, 49, 64, 96, 154, 268, 489, 918),
    ("low", 13): (1030, 578, 320, 187, 115, 75, 54, 54, 75, 115, 187, 320, 578, 1030),
    ("low", 14): (1190, 662, 380, 223, 137, 90, 63, 49, 63, 90, 137, 223, 380, 662, 1190),
    ("low", 15): (1390, 777, 448, 264, 165, 105, 73, 54, 54, 73, 105, 165, 264, 448, 777, 1390),
    ("low", 16): (1590, 900, 523, 314, 195, 125, 85, 62, 50, 62, 85, 125, 195, 314, 523, 900, 1590),
    ("medium", 8): (1230, 317, 112, 53, 38, 53, 112, 317, 1230),
    ("medium", 9): (1670, 446, 154, 70, 42, 42, 70, 154, 446, 1670),
    ("medium", 10): (2200, 618, 214, 92, 51, 40, 51, 92, 214, 618, 2200),
    ("medium", 11): (2920, 844, 293, 123, 64, 43, 43, 64, 123, 293, 844, 2920),
    ("medium", 12): (3820, 1140, 398, 165, 82, 50, 41, 50, 82, 165, 398, 1140, 3820),
    ("medium", 13): (5060, 1540, 537, 219, 107, 61, 43, 43, 61, 107, 219, 537, 1540, 5060),
    ("medium", 14): (6630, 2060, 727, 295, 137, 75, 50, 41, 50, 75, 137, 295, 727, 2060, 6630),
    ("medium", 15): (8680, 2740, 971, 391, 176, 95, 59, 44, 44, 59, 95, 176, 391, 971, 2740, 8680),
    ("medium", 16): (11400, 3660, 1310, 520, 231, 120, 70, 49, 44, 49, 70, 120, 231, 520, 1310, 3660, 11400),
    ("high", 8): (3500, 364, 77, 32, 24, 32, 77, 364, 3500),
    ("high", 9): (5630, 614, 121, 43, 26, 26, 43, 121, 614, 5630),
    ("high", 10): (9080, 1010, 192, 61, 31, 25, 31, 61, 192, 1010, 9080),
    ("high", 11): (14800, 1700, 306, 88, 40, 25, 25, 40, 88, 306, 1700, 14800),
    ("high", 12): (23500, 2690, 477, 129, 52, 32, 27, 32, 52, 129, 477, 2690, 23500),
    ("high", 13): (36000, 4350, 761, 196, 73, 39, 28, 28, 39, 73, 196, 761, 4350, 36000),
    ("high", 14): (58100, 7010, 1210, 297, 100, 49, 32, 28, 32, 49, 100, 297, 1210, 7010, 58100),
    ("high", 15): (91600, 11200, 1920, 445, 145, 65, 38, 29, 29, 38, 65, 145, 445, 1920, 11200, 91600),
    ("high", 16): (144600, 18000, 3030, 685, 211, 89, 46, 32, 28, 32, 46, 89, 211, 685, 3030, 18000, 144600),
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
