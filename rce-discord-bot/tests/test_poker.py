"""Texas Hold'em engine: hand ranking, blinds and turn order, raises, side pots, time-outs, chips never lost."""
import random

import pytest

from support import poker as pk
from support.casino_games import RANKS


def c(text: str) -> int:
    """'As' -> ace of spades, 'Th' -> ten of hearts."""
    rank = {"T": "10"}.get(text[0], text[0])
    return "shdc".index(text[1]) * 13 + RANKS.index(rank)


def hand(*cards):
    return pk.rank5([c(x) for x in cards])


# ------------------------------------------------------------ Ranking

def test_hand_ranking_order():
    royal = hand("As", "Ks", "Qs", "Js", "Ts")
    quads = hand("9s", "9h", "9d", "9c", "2s")
    full = hand("8s", "8h", "8d", "3c", "3s")
    flush = hand("2h", "7h", "9h", "Jh", "Kh")
    straight = hand("5s", "6h", "7d", "8c", "9s")
    wheel = hand("As", "2h", "3d", "4c", "5s")
    trips = hand("7s", "7h", "7d", "Kc", "2s")
    two = hand("Ks", "Kh", "5d", "5c", "As")
    pair = hand("Js", "Jh", "5d", "4c", "2s")
    high = hand("As", "Jh", "8d", "4c", "2s")
    assert royal > quads > full > flush > straight > wheel > trips > two > pair > high
    assert wheel == (4, 5) and straight == (4, 9)
    assert hand("Ks", "Kh", "5d", "5c", "Qs") < two                     # kicker decides
    assert hand("Ah", "Ad", "Kh", "Kd", "2c") > two                     # higher top pair


def test_best_of_seven_and_names():
    strength, five = pk.best_hand([c(x) for x in ("As", "Ks", "2h", "Qs", "Js", "Ts", "3d")])
    assert strength == (8, 14) and pk.hand_name(strength) == "Straight flush, Ace high"
    assert pk.hand_name(hand("8s", "8h", "8d", "3c", "3s")) == "Full house, Eights full of Threes"
    assert pk.hand_name(hand("Ks", "Kh", "5d", "5c", "As")) == "Two pair, Kings and Fives"
    assert pk.hand_name(hand("As", "2h", "3d", "4c", "5s")) == "Straight, 5 high"


# ------------------------------------------------------------ Table helpers

class Deck(random.Random):
    """shuffle() puts the given cards on top (the deck is dealt from the end)."""
    def __init__(self, top):
        super().__init__(1)
        self.top = [c(x) for x in top]

    def shuffle(self, deck):
        rest = [x for x in deck if x not in self.top]
        deck[:] = rest + list(reversed(self.top))


def table(*stacks, sb=5, bb=10, **kw):
    t = pk.Table(id="t", name="Test", sb=sb, bb=bb, min_buy=1, max_buy=10_000, **kw)
    for n, stack in enumerate(stacks):
        t.sit(100 + n, f"P{n}", stack, now=0)
    return t


def total(t):
    return sum(s.stack + (s.committed if t.in_hand_now else 0) for s in t.seats if s) + \
        sum(a for _, a in t.cashouts) + t.rake_total


# ------------------------------------------------------------ Blinds, order, betting

def test_heads_up_button_posts_small_blind_and_acts_first():
    t = table(1000, 1000)
    assert t.start_hand(0, Deck([]))
    sb, bb = t.seats[t.button], t.seats[1 - t.button]
    assert sb.bet == 5 and bb.bet == 10 and t.to_act == t.button
    t.act(sb.user_id, "call", now=1)
    t.act(bb.user_id, "check", now=2)
    assert t.street == "flop" and len(t.board) == 3 and t.to_act == 1 - t.button     # big blind first after flop


def test_three_players_fold_to_big_blind():
    t = table(1000, 1000, 1000)
    t.start_hand(0, Deck([]))
    btn = t.button
    sb, bb, utg = (btn + 1) % 3, (btn + 2) % 3, btn
    assert t.seats[sb].bet == 5 and t.seats[bb].bet == 10 and t.to_act == utg
    t.act(t.seats[utg].user_id, "fold", now=1)
    t.act(t.seats[sb].user_id, "fold", now=2)
    assert t.street == "showdown" and t.result[0]["seat"] == bb and t.result[0]["amount"] == 15
    assert t.seats[bb].stack == 1005 and total(t) == 3000
    assert all(s.cards and not s.shown for s in t.seats if s)            # nobody has to show


def test_minimum_raise_and_reraise():
    t = table(1000, 1000, 1000)
    t.start_hand(0, Deck([]))
    utg = t.seats[t.to_act]
    with pytest.raises(pk.PokerError, match="between 20"):
        t.act(utg.user_id, "raise", 15, now=1)                         # min raise to 20 (bb + bb)
    t.act(utg.user_id, "raise", 40, now=1)                             # raises by 30
    nxt = t.seats[t.to_act]
    assert t.legal(nxt.user_id)["min_to"] == 70                       # next raise at least 30 more
    t.act(nxt.user_id, "raise", 70, now=2)
    assert t.min_raise == 30 and t.current_bet == 70


def test_short_all_in_does_not_reopen_betting():
    t = table(1000, 1000, 55)                                           # P2 has 55
    t.start_hand(0, Deck([]))
    order = []
    while t.street == "preflop" and t.to_act is not None:
        s = t.seats[t.to_act]
        order.append(s.name)
        legal = t.legal(s.user_id)
        if s.name == "P2":
            t.act(s.user_id, "allin", now=1)
        elif s.name not in order[:-1]:
            t.act(s.user_id, "raise", 40, now=1) if legal["raise"] and t.current_bet < 40 else \
                t.act(s.user_id, "call", now=1)
        else:
            assert not legal["raise"] or t.current_bet - 40 >= t.min_raise   # short all-in: call or fold only
            t.act(s.user_id, "call", now=1)
    assert total(t) == 2055


def test_side_pots_all_in():
    # P0 100 (best hand), P1 300 (second), P2 300 (worst)
    t = table(100, 300, 300)
    t.button = 2                                                        # button P0 next -> deal starts at P1
    deck = ["Kd", "2c", "As", "Kh", "3c", "Ah",                         # P1, P2, P0, P1, P2, P0
            "7d", "8d", "Js", "4h", "5c"]                               # flop, turn, river (no straight/flush)
    t.start_hand(0, Deck(deck))
    assert t.button == 0
    for _ in range(10):
        if t.to_act is None:
            break
        s = t.seats[t.to_act]
        t.act(s.user_id, "allin" if t.legal(s.user_id)["raise"] else "call", now=1)
    assert t.street == "showdown" and len(t.board) == 5
    won = {r["name"]: r["amount"] for r in t.result}
    assert won == {"P0": 300, "P1": 400}                                # main pot 3×100, side pot 2×200
    assert [s.stack for s in t.seats[:2]] == [300, 400] and t.seats[2] is None     # P2 busted, seat freed
    assert all(s is None or s.shown for s in t.seats)


def test_split_pot_odd_chip_goes_left_of_button():
    t = table(1000, 1000, 1000, sb=5, bb=10)
    t.button = 2
    deck = ["2c", "2d", "3c", "3d", "4c", "4d",
            "As", "Ks", "Qh", "Jd", "Tc"]                               # broadway on the board: everybody splits
    t.start_hand(0, Deck(deck))
    # P0 is the button; P1 small blind, P2 big blind; P0 folds after putting nothing in -> pot 15 split 2 ways
    t.act(t.seats[0].user_id, "fold", now=1)
    t.act(t.seats[1].user_id, "call", now=1)
    t.act(t.seats[2].user_id, "check", now=1)
    while t.to_act is not None:
        t.act(t.seats[t.to_act].user_id, "check", now=1)
    assert sorted(r["amount"] for r in t.result) == [10, 10] and total(t) == 3000


def test_time_out_checks_or_folds_and_sits_out():
    t = table(1000, 1000, turn_time=30)
    t.start_hand(0, Deck([]))
    first = t.seats[t.to_act]
    assert not t.tick(10)
    assert t.tick(31) and first.folded and first.sitting_out and t.street == "showdown"
    t.tick(40, Deck([]))
    assert t.street in ("idle", "showdown")                             # one player left: no new hand
    t.tick(40 + pk.SIT_OUT_LIMIT + 1)
    assert t.seat_of(first.user_id) is None and t.cashouts == [(first.user_id, first.stack)]


def test_leave_mid_hand_and_refund_on_restart():
    t = table(500, 500, 500)
    t.start_hand(0, Deck([]))
    s = t.seats[t.to_act]
    t.act(s.user_id, "raise", 100, now=1)
    back = t.leave(s.user_id, now=2)
    assert back == 400 and s.folded and s.leaving                       # the 100 in the pot stays there
    refunds = t.refund_all()
    assert sum(a for _, a in refunds) == 1500 - 400                      # everyone gets stack + chips in the pot
    assert all(x is None for x in t.seats)


def test_random_play_never_creates_or_loses_chips():
    rng = random.Random(42)
    for game in range(40):
        stacks = [rng.choice([20, 100, 500, 1000]) for _ in range(rng.randint(2, 6))]
        t = table(*stacks, rake_pct=rng.choice([0, 5]), rake_cap=rng.choice([0, 30]))
        start = sum(stacks)
        now = 0.0
        for _ in range(400):
            now += 1
            if not t.in_hand_now:
                if not t.start_hand(now, rng):
                    break
                if not t.in_hand_now:                                   # all-in on the blinds: already over
                    continue
            assert t.to_act is not None
            s = t.seats[t.to_act]
            legal = t.legal(s.user_id)
            roll = rng.random()
            if roll < 0.15:
                t.act(s.user_id, "fold", now=now)
            elif roll < 0.3 and legal["raise"]:
                t.act(s.user_id, "raise", rng.randint(legal["min_to"], legal["max_to"]), now=now)
            elif roll < 0.35 and legal["raise"]:
                t.act(s.user_id, "allin", now=now)
            else:
                t.act(s.user_id, "check" if legal["check"] else "call", now=now)
            assert total(t) == start, (game, t.log[-5:])
            assert all(x is None or x.stack >= 0 for x in t.seats)


def test_view_hides_other_cards_and_round_trips():
    t = table(1000, 1000)
    t.start_hand(0, Deck([]))
    v = t.view(100, 0)
    mine, other = v["seats"][0], v["seats"][1]
    assert len(mine["cards"]) == 2 and other["cards"] is None and other["card_count"] == 2
    assert v["legal"] is not None or t.to_act != 0
    assert "deck" not in v and "deck" not in str(t.view(None, 0))
    copy = pk.Table.from_dict(t.to_dict())
    assert copy.view(101, 0) == t.view(101, 0)


def test_dealer_deals_by_timer():
    t = pk.Table(id="t", name="T", sb=5, bb=10, min_buy=1, max_buy=1000, hand_delay=8)
    t.sit(1, "A", 500, now=100)
    assert t.view(1, 100)["next_in"] is None                            # alone: no countdown
    t.tick(110)
    t.sit(2, "B", 500, now=120)
    assert t.view(1, 120)["next_in"] == 8                               # 2 players: deals in 8 s
    assert not t.tick(127, Deck([])) and not t.in_hand_now
    assert t.tick(128, Deck([])) and t.in_hand_now
    while t.to_act is not None:
        t.act(t.seats[t.to_act].user_id, "fold", now=130)
    assert t.street == "showdown" and t.view(1, 130)["next_in"] == 8   # next hand by the timer again
    assert not t.tick(137, Deck([])) and t.tick(138, Deck([])) and t.hand_no == 2
