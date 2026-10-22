"""Texas Hold'em (No-Limit) for the casino: players against players at tables, with points as chips.

Pure game logic – no database, no network. The Casino plugin (plugins/casino.py) keeps the tables, moves points
between wallets and tables and serves each player their own view (web/casino_pages.py, website_assets/poker.js).

Rules: 2–6 players, the button moves every hand, small/big blind (heads-up: the button posts the small blind and acts
first before the flop), no-limit betting with real minimum raises (a short all-in doesn't re-open the betting for
players who already acted), side pots for all-ins, split pots, odd chips to the first winner left of the button.
A player who runs out of time checks if he can, else folds, and sits out until he's back. Optional rake (house cut,
off by default): a percentage of each contested pot that saw a flop, up to a cap.

Times are passed in (`now`), the deck comes from `rng` – so everything is testable. Cards are 0–51 like blackjack.
"""
from __future__ import annotations

import random
import secrets
from dataclasses import asdict, dataclass, field
from itertools import combinations

from support.casino_games import card_label

RNG = secrets.SystemRandom()
MAX_SEATS = 6
SIT_OUT_LIMIT = 300                 # seconds sitting out before the bot stands the player up (chips back)
NEXT_HAND_DELAY = 5.0               # seconds the result stays on the table before the next hand
CATEGORIES = ("High card", "Pair", "Two pair", "Three of a kind", "Straight", "Flush", "Full house",
              "Four of a kind", "Straight flush")
RANK_NAMES = {14: "Aces", 13: "Kings", 12: "Queens", 11: "Jacks", 10: "Tens", 9: "Nines", 8: "Eights", 7: "Sevens",
              6: "Sixes", 5: "Fives", 4: "Fours", 3: "Threes", 2: "Twos"}


class PokerError(Exception):
    """Shown to the player."""


# ------------------------------------------------------------ Hand strength

def card_rank(card: int) -> int:
    r = card % 13
    return 14 if r == 0 else r + 1


def card_suit(card: int) -> int:
    return (card // 13) % 4


def rank5(cards) -> tuple:
    """Strength of 5 cards as a comparable tuple: (category, tie-breakers …)."""
    ranks = sorted((card_rank(c) for c in cards), reverse=True)
    counts: dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)     # by count, then rank
    flush = len({card_suit(c) for c in cards}) == 1
    unique = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:          # the wheel: A-2-3-4-5
            straight_high = 5
    if straight_high and flush:
        return (8, straight_high)
    if groups[0][1] == 4:
        return (7, groups[0][0], groups[1][0])
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, groups[0][0], groups[1][0])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if groups[0][1] == 3:
        return (3, groups[0][0], *[r for r, _ in groups[1:]])
    if groups[0][1] == 2 and groups[1][1] == 2:
        return (2, groups[0][0], groups[1][0], groups[2][0])
    if groups[0][1] == 2:
        return (1, groups[0][0], *[r for r, _ in groups[1:]])
    return (0, *ranks)


def best_hand(cards) -> tuple[tuple, tuple]:
    """Best 5 of up to 7 cards -> (strength, the 5 cards)."""
    best = None
    for five in combinations(cards, 5):
        strength = rank5(five)
        if best is None or strength > best[0]:
            best = (strength, five)
    return best


def hand_name(strength: tuple) -> str:
    cat = strength[0]
    name = CATEGORIES[cat]
    if cat in (1, 3, 7):
        return f"{name}, {RANK_NAMES[strength[1]]}"
    if cat == 2:
        return f"Two pair, {RANK_NAMES[strength[1]]} and {RANK_NAMES[strength[2]]}"
    if cat == 6:
        return f"Full house, {RANK_NAMES[strength[1]]} full of {RANK_NAMES[strength[2]]}"
    if cat in (0, 4, 5, 8):
        return f"{name}, {_high(strength[1])} high"
    return name


def _high(rank: int) -> str:
    return {14: "Ace", 13: "King", 12: "Queen", 11: "Jack"}.get(rank, str(rank))


# ------------------------------------------------------------ Table

@dataclass
class Seat:
    user_id: int
    name: str
    stack: int
    sitting_out: bool = False
    out_since: float = 0.0
    leaving: bool = False
    # this hand
    cards: list = field(default_factory=list)
    in_hand: bool = False
    folded: bool = False
    allin: bool = False
    bet: int = 0                # this betting round
    committed: int = 0          # this hand
    acted: bool = False
    can_raise: bool = True
    shown: bool = False
    last_action: str = ""


@dataclass
class Table:
    id: str
    name: str
    sb: int
    bb: int
    min_buy: int
    max_buy: int
    rake_pct: float = 0.0
    rake_cap: int = 0
    turn_time: int = 30
    seats: list = field(default_factory=lambda: [None] * MAX_SEATS)
    button: int = -1
    street: str = "idle"        # idle, preflop, flop, turn, river, showdown
    deck: list = field(default_factory=list)
    board: list = field(default_factory=list)
    current_bet: int = 0
    min_raise: int = 0
    to_act: int | None = None
    deadline: float = 0.0
    next_hand_at: float = 0.0
    hand_no: int = 0
    version: int = 0
    result: list = field(default_factory=list)       # winners of the last hand (shown on the table)
    log: list = field(default_factory=list)
    rake_total: int = 0
    cashouts: list = field(default_factory=list)     # (user_id, points) the plugin must pay back

    # -------------------------------------------------------- Persistence

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Table":
        t = cls(**{k: v for k, v in d.items() if k != "seats"})
        t.seats = [Seat(**s) if s else None for s in d.get("seats", [None] * MAX_SEATS)]
        return t

    # -------------------------------------------------------- Seats

    def seat_of(self, user_id: int) -> int | None:
        return next((i for i, s in enumerate(self.seats) if s is not None and s.user_id == user_id), None)

    @property
    def in_hand_now(self) -> bool:
        return self.street in ("preflop", "flop", "turn", "river")

    def sit(self, user_id: int, name: str, buy_in: int, now: float, seat: int | None = None) -> int:
        if self.seat_of(user_id) is not None:
            raise PokerError("You're already sitting at this table.")
        if not self.min_buy <= buy_in <= self.max_buy:
            raise PokerError(f"Buy in with {self.min_buy}–{self.max_buy} points.")
        free = [i for i, s in enumerate(self.seats) if s is None]
        if not free:
            raise PokerError("The table is full.")
        if seat is None or seat not in free:
            seat = free[0]
        self.seats[seat] = Seat(user_id=user_id, name=name, stack=buy_in)
        self._note(f"{name} sits down with {buy_in}.")
        if not self.in_hand_now and self.next_hand_at < now:
            self.next_hand_at = now + 2
        self._changed()
        return seat

    def leave(self, user_id: int, now: float) -> int:
        """Stands up; the chips not in the pot go back (returned, also added to cashouts by the caller)."""
        i = self.seat_of(user_id)
        if i is None:
            raise PokerError("You're not sitting at this table.")
        s = self.seats[i]
        if self.in_hand_now and s.in_hand and not s.folded:
            self._fold(i, now, reason="leaves")
        chips, s.stack = s.stack, 0
        if self.in_hand_now and s.in_hand:
            s.leaving = True                       # the seat is freed when the hand is over
        else:
            self.seats[i] = None
        self._note(f"{s.name} leaves the table.")
        self._changed()
        return chips

    def set_back(self, user_id: int, back: bool = True) -> None:
        i = self.seat_of(user_id)
        if i is None:
            raise PokerError("You're not sitting at this table.")
        s = self.seats[i]
        s.sitting_out = not back
        s.out_since = 0.0 if back else s.out_since
        self._changed()

    # -------------------------------------------------------- Hands

    def _players(self) -> list[int]:
        return [i for i, s in enumerate(self.seats)
                if s is not None and not s.sitting_out and not s.leaving and s.stack > 0]

    def _after(self, i: int, pred) -> int | None:
        for k in range(1, MAX_SEATS + 1):
            j = (i + k) % MAX_SEATS
            if pred(j):
                return j
        return None

    def _actor(self, j: int) -> bool:
        s = self.seats[j]
        return s is not None and s.in_hand and not s.folded and not s.allin

    def start_hand(self, now: float, rng: random.Random = RNG) -> bool:
        players = self._players()
        if len(players) < 2:
            return False
        self.hand_no += 1
        self.result, self.board = [], []
        self.deck = list(range(52))
        rng.shuffle(self.deck)
        for i, s in enumerate(self.seats):
            if s is None:
                continue
            s.cards, s.in_hand, s.folded, s.allin = [], i in players, False, False
            s.bet = s.committed = 0
            s.acted, s.can_raise, s.shown, s.last_action = False, True, False, ""
        self.button = self._after(self.button if self.button >= 0 else -1, lambda j: j in players)
        if len(players) == 2:
            sb_seat = self.button
            bb_seat = self._after(sb_seat, lambda j: j in players)
        else:
            sb_seat = self._after(self.button, lambda j: j in players)
            bb_seat = self._after(sb_seat, lambda j: j in players)
        for _ in range(2):
            for k in range(1, MAX_SEATS + 1):
                j = (self.button + k) % MAX_SEATS
                if j in players:
                    self.seats[j].cards.append(self.deck.pop())
        self._post(sb_seat, self.sb, "small blind")
        self._post(bb_seat, self.bb, "big blind")
        self.street = "preflop"
        self.current_bet, self.min_raise = self.bb, self.bb
        self._note(f"Hand #{self.hand_no}: {self.seats[sb_seat].name} and {self.seats[bb_seat].name} post the blinds.")
        first = self._after(bb_seat, self._actor)
        self._continue(first, now)
        self._changed()
        return True

    def _post(self, i: int, amount: int, what: str) -> None:
        s = self.seats[i]
        paid = min(amount, s.stack)
        s.stack -= paid
        s.bet += paid
        s.committed += paid
        s.allin = s.stack == 0
        s.last_action = what

    def legal(self, user_id: int) -> dict | None:
        """What the player may do now (None = not his turn)."""
        i = self.seat_of(user_id)
        if i is None or i != self.to_act or not self.in_hand_now:
            return None
        s = self.seats[i]
        to_call = max(0, self.current_bet - s.bet)
        call = min(to_call, s.stack)
        all_in_to = s.bet + s.stack
        min_to = self.current_bet + self.min_raise if self.current_bet else self.bb
        raise_ok = s.can_raise and all_in_to > self.current_bet and any(
            self._actor(j) for j in range(MAX_SEATS) if j != i)
        return {"fold": True, "check": to_call == 0, "call": call if to_call else 0,
                "raise": raise_ok, "min_to": min(min_to, all_in_to), "max_to": all_in_to,
                "bet_word": "Raise" if self.current_bet else "Bet"}

    def act(self, user_id: int, action: str, amount: int = 0, now: float = 0.0) -> None:
        legal = self.legal(user_id)
        if legal is None:
            raise PokerError("It's not your turn.")
        i = self.seat_of(user_id)
        s = self.seats[i]
        if action == "fold":
            self._fold(i, now)
            return
        if action == "check":
            if not legal["check"]:
                raise PokerError("You can't check – call or fold.")
            s.acted, s.can_raise, s.last_action = True, False, "check"
        elif action == "call":
            if legal["check"]:
                raise PokerError("Nothing to call – check instead.")
            self._put(s, legal["call"])
            s.acted, s.can_raise = True, False         # may raise again only after someone's full raise
            s.last_action = "all-in" if s.allin else f"call {legal['call']}"
        elif action in ("bet", "raise", "allin"):
            if not legal["raise"]:
                if action == "allin" and legal["call"]:
                    return self.act(user_id, "call", 0, now)
                raise PokerError("You can't raise now.")
            to = legal["max_to"] if action == "allin" else int(amount)
            if not legal["min_to"] <= to <= legal["max_to"]:
                raise PokerError(f"{legal['bet_word']} to between {legal['min_to']} and {legal['max_to']}.")
            raise_by = to - self.current_bet
            self._put(s, to - s.bet)
            full = raise_by >= self.min_raise
            if full:
                self.min_raise = raise_by
            self.current_bet = to
            for j, o in enumerate(self.seats):
                if j != i and o is not None and self._actor(j):
                    o.acted = False                 # everyone has to answer the raise …
                    if full:
                        o.can_raise = True          # … and may re-raise only after a full raise
            s.acted, s.can_raise = True, False
            s.last_action = "all-in" if s.allin else f"{legal['bet_word'].lower()} to {to}"
        else:
            raise PokerError("Unknown action.")
        self._note(f"{s.name}: {s.last_action}")
        self._continue(self._after(i, self._actor), now)
        self._changed()

    def _put(self, s: Seat, amount: int) -> None:
        amount = min(amount, s.stack)
        s.stack -= amount
        s.bet += amount
        s.committed += amount
        if s.stack == 0:
            s.allin = True

    def _fold(self, i: int, now: float, reason: str = "fold") -> None:
        s = self.seats[i]
        s.folded, s.acted, s.last_action = True, True, "fold"
        self._note(f"{s.name} {'folds' if reason == 'fold' else reason}.")
        nxt = self._after(i, self._actor) if self.to_act == i else self.to_act
        self._continue(nxt, now)
        self._changed()

    def _continue(self, nxt: int | None, now: float) -> None:
        """After an action: next player, next street, or the end of the hand."""
        live = [j for j, s in enumerate(self.seats) if s is not None and s.in_hand and not s.folded]
        if len(live) == 1:
            self._finish(now)
            return
        actors = [j for j in live if not self.seats[j].allin]
        pending = [j for j in actors if not self.seats[j].acted or self.seats[j].bet < self.current_bet]
        if pending and not (len(actors) == 1 and self.seats[actors[0]].bet >= self.current_bet):
            if nxt is None or nxt not in pending:
                nxt = self._after(nxt if nxt is not None else self.button, lambda j: j in pending)
            away = self.seats[nxt].sitting_out          # timed out before: don't make the table wait again
            self.to_act, self.deadline = nxt, now + (2 if away else self.turn_time)
            return
        self._next_street(now)

    def _next_street(self, now: float) -> None:
        for s in self.seats:
            if s is not None:
                s.bet, s.acted = 0, False
                if s.in_hand and not s.folded:
                    s.can_raise = True
        self.current_bet, self.min_raise = 0, self.bb
        if self.street == "river":
            self._finish(now)
            return
        self.street = {"preflop": "flop", "flop": "turn", "turn": "river"}[self.street]
        self.board += [self.deck.pop() for _ in range(3 if self.street == "flop" else 1)]
        live = [j for j, s in enumerate(self.seats) if s is not None and s.in_hand and not s.folded]
        actors = [j for j in live if not self.seats[j].allin]
        if len(actors) <= 1:                       # everyone (but one) is all-in: deal the rest, no betting
            self._next_street(now)
            return
        self._continue(self._after(self.button, self._actor), now)

    def _finish(self, now: float) -> None:
        live = [j for j, s in enumerate(self.seats) if s is not None and s.in_hand and not s.folded]
        contrib = {j: s.committed for j, s in enumerate(self.seats) if s is not None and s.committed}
        showdown = len(live) > 1
        strength = {j: best_hand(self.seats[j].cards + self.board)[0] for j in live} if showdown else {}
        won: dict[int, int] = {}
        names: dict[int, str] = {}
        levels = sorted(set(contrib.values()))
        prev, raked = 0, 0
        for level in levels:
            pot = sum(min(c, level) - min(c, prev) for c in contrib.values())
            eligible = [j for j in live if contrib.get(j, 0) >= level] or live
            prev = level
            if pot <= 0:
                continue
            if len(eligible) > 1 and self.board and self.rake_pct > 0:
                rake = int(pot * self.rake_pct / 100)
                if self.rake_cap:
                    rake = min(rake, max(0, self.rake_cap - raked))
                pot -= rake
                raked += rake
            if showdown and len(eligible) > 1:
                best = max(strength[j] for j in eligible)
                winners = [j for j in eligible if strength[j] == best]
            else:
                winners = eligible[:1] if not showdown else eligible
            share, odd = divmod(pot, len(winners))
            order = [j for j in ((self.button + k) % MAX_SEATS for k in range(1, MAX_SEATS + 1)) if j in winners]
            for n, j in enumerate(order):
                won[j] = won.get(j, 0) + share + (1 if n < odd else 0)
        for j, amount in won.items():
            s = self.seats[j]
            if s.leaving:
                self.cashouts.append((s.user_id, amount))          # left mid-hand but won: paid to the wallet
            else:
                s.stack += amount
            if showdown:
                names[j] = hand_name(strength[j])
        if showdown:
            for j in live:
                self.seats[j].shown = True
        self.rake_total += raked
        self.result = [{"seat": j, "name": self.seats[j].name, "amount": a, "hand": names.get(j, "")}
                       for j, a in won.items()]
        for r in self.result:
            self._note(f"{r['name']} wins {r['amount']}" + (f" with {r['hand']}" if r["hand"] else "") + ".")
        self.street, self.to_act = "showdown", None
        self.next_hand_at = now + NEXT_HAND_DELAY
        for i, s in enumerate(self.seats):                          # free seats of leavers and busted players
            if s is None:
                continue
            if s.leaving:
                self.seats[i] = None
            elif s.stack == 0:
                self._note(f"{s.name} is out of chips.")
                self.seats[i] = None

    # -------------------------------------------------------- Clock

    def tick(self, now: float, rng: random.Random = RNG) -> bool:
        """Time-outs, sit-out limit and starting the next hand. True if something changed."""
        changed = False
        if self.in_hand_now and self.to_act is not None and now >= self.deadline:
            s = self.seats[self.to_act]
            legal = self.legal(s.user_id)
            s.sitting_out, s.out_since = True, now
            self._note(f"{s.name} ran out of time.")
            self.act(s.user_id, "check" if legal and legal["check"] else "fold", 0, now)
            changed = True
        for i, s in enumerate(self.seats):
            if s is not None and s.sitting_out and not s.out_since:
                s.out_since = now
            if (s is not None and s.sitting_out and now - s.out_since > SIT_OUT_LIMIT
                    and not (self.in_hand_now and s.in_hand and not s.folded)):
                self.cashouts.append((s.user_id, s.stack))
                self._note(f"{s.name} was away too long and stands up.")
                if self.in_hand_now and s.in_hand:
                    s.stack, s.leaving = 0, True
                else:
                    self.seats[i] = None
                changed = True
        if not self.in_hand_now and now >= self.next_hand_at:
            if self.street == "showdown":
                self.street, changed = "idle", True
            if self.start_hand(now, rng):
                changed = True
        if changed:
            self._changed()
        return changed

    def refund_all(self) -> list[tuple[int, int]]:
        """Closes the table (bot restart, table removed): every player gets his stack + what he put in this hand."""
        out = list(self.cashouts)
        for s in self.seats:
            if s is not None:
                back = s.stack + (s.committed if self.in_hand_now else 0)
                if back:
                    out.append((s.user_id, back))
        self.seats = [None] * MAX_SEATS
        self.cashouts, self.street, self.to_act, self.board = [], "idle", None, []
        self._changed()
        return out

    # -------------------------------------------------------- What a player sees

    def view(self, user_id: int | None, now: float) -> dict:
        me = self.seat_of(user_id) if user_id is not None else None
        seats = []
        for i, s in enumerate(self.seats):
            if s is None:
                seats.append(None)
                continue
            visible = i == me or (s.shown and self.street == "showdown")
            seats.append({
                "name": s.name, "stack": s.stack, "bet": s.bet, "in_hand": s.in_hand, "folded": s.folded,
                "allin": s.allin, "sitting_out": s.sitting_out, "action": s.last_action,
                "cards": [card_label(c) for c in s.cards] if visible and s.in_hand else None,
                "card_count": len(s.cards) if s.in_hand and not s.folded else 0,
                "button": i == self.button and self.street != "idle", "turn": i == self.to_act,
            })
        pot = sum(s.committed for s in self.seats if s is not None) if self.street != "idle" else 0
        if self.street == "showdown":
            pot = 0
        legal = self.legal(user_id) if user_id is not None else None
        return {"id": self.id, "name": self.name, "sb": self.sb, "bb": self.bb, "min_buy": self.min_buy,
                "max_buy": self.max_buy, "street": self.street, "board": [card_label(c) for c in self.board],
                "pot": pot, "seats": seats, "you": me, "legal": legal, "hand": self.hand_no,
                "turn_left": max(0, round(self.deadline - now, 1)) if self.to_act is not None else 0,
                "turn_time": self.turn_time, "result": self.result, "log": self.log[-8:], "version": self.version}

    def _note(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-30]

    def _changed(self) -> None:
        self.version += 1
