"""Casino: exact RTP of every game, real blackjack rules, the plugin's money handling and the website pages."""
import asyncio
import random
import re
import time
from dataclasses import replace
from fractions import Fraction

import pytest
from aiohttp.test_utils import TestClient, TestServer

from support import casino_games as g
from tests.test_plugins import pb  # noqa: F401


# ------------------------------------------------------------ Exact return to player

def test_slots_rtp_is_exactly_95_percent():
    class Roll(random.Random):     # walks through every possible roll once
        def randrange(self, n):
            return self.value
    rng, paid = Roll(1), 0
    for value in range(g.SLOT_TOTAL):
        rng.value = value
        spin = g.spin_slots(100, rng)
        paid += spin.payout
        three = len(set(spin.reels)) == 1
        cherries = spin.reels.count("🍒")
        if spin.multiplier == 0:
            assert not three and cherries < 2                       # a loss never looks like a win
        elif spin.multiplier == 2:
            assert cherries == 2
        else:
            assert three
    assert Fraction(paid, 100 * g.SLOT_TOTAL) == Fraction(95, 100) and g.SLOT_RTP == 0.95


class Fixed(random.Random):
    def __init__(self, value):
        super().__init__(0)
        self.value = value

    def randrange(self, n):
        return self.value


def test_coinflip_and_dice_rtp_is_95_percent():
    assert Fraction(1, 2) * Fraction(19, 10) == Fraction(95, 100)
    assert g.flip_coin(100, "heads", random.Random(1))[1] in (0, 190)
    assert g.flip_coin(10, "heads", random.Random(1))[1] in (0, 19)
    for target in range(g.DICE_MIN, g.DICE_MAX + 1):
        assert Fraction(target, 100) * Fraction(95, target) == Fraction(95, 100)
    for bet, target in ((1000, 90), (7, 3), (15, 50)):              # fair rounding: exact on average
        whole, rest = divmod(bet * 95, target)
        expected_payout = whole + Fraction(rest, target)
        assert Fraction(target, 100) * expected_payout / bet == Fraction(95, 100)
        assert sum(g.fair(bet * 95, target, Fixed(k)) for k in range(target)) == bet * 95   # every remainder case
    assert g.fair(15 * 19, 10, Fixed(4)) == 29 and g.fair(15 * 19, 10, Fixed(5)) == 28
    with pytest.raises(ValueError):
        g.roll_dice(10, 95)


def test_roulette_is_a_real_european_wheel():
    assert len(g.RED) == 18
    for kind, (_, pays) in g.ROULETTE_BETS.items():
        numbers = [7] if kind == "number" else [None]
        for number in numbers:
            wins = sum(1 for n in range(37) if g.roulette_wins(kind, number, n))
            assert Fraction(wins * pays, 37) == Fraction(36, 37), kind     # 97.3 %
    assert all(not g.roulette_wins(k, None, 0) for k in g.ROULETTE_BETS if k != "number")
    assert g.roulette_wins("number", 0, 0)
    assert g.roulette_colour(0) == "green" and g.roulette_colour(1) == "red" and g.roulette_colour(2) == "black"


# ------------------------------------------------------------ Blackjack rules

def card(rank: str, suit: int = 0) -> int:
    return suit * 13 + g.RANKS.index(rank)


def deal(monkeypatch, player, dealer, rest=(), bet=10):
    """Deals a fixed shoe: player 2 cards, dealer 2 cards, then `rest` in order."""
    order = [player[0], player[1], dealer[0], dealer[1], *rest]
    monkeypatch.setattr(g, "new_shoe", lambda rng=None: [card(r) for r in reversed(order)] or [])
    return g.bj_deal(bet)


def test_blackjack_pays_3_to_2_and_dealer_peeks(monkeypatch):
    s = deal(monkeypatch, ["A", "K"], ["9", "7"])
    assert s["finished"] and s["payout"] == 25 and s["hands"][0]["result"] == "blackjack"
    s = deal(monkeypatch, ["9", "7"], ["A", "Q"])                 # dealer blackjack: over at once
    assert s["finished"] and s["payout"] == 0 and len(s["dealer"]) == 2
    s = deal(monkeypatch, ["A", "K"], ["9", "7"], bet=15)          # 37.5: 37 or 38, fair on average
    assert s["payout"] in (37, 38)
    s = deal(monkeypatch, ["A", "Q"], ["K", "A"])
    assert s["hands"][0]["result"] == "push" and s["payout"] == 10


def test_dealer_stands_on_soft_17_and_hits_16(monkeypatch):
    s = deal(monkeypatch, ["10", "8"], ["A", "6"], rest=["5"])
    g.bj_act(s, "stand")
    assert len(s["dealer"]) == 2 and s["hands"][0]["result"] == "win" and s["payout"] == 20
    s = deal(monkeypatch, ["10", "7"], ["10", "6"], rest=["5"])
    g.bj_act(s, "stand")
    assert g.hand_total(s["dealer"])[0] == 21 and s["hands"][0]["result"] == "lose"


def test_hit_bust_and_double(monkeypatch):
    s = deal(monkeypatch, ["10", "6"], ["10", "7"], rest=["K"])
    assert g.bj_actions(s) == ["hit", "stand", "double"]
    g.bj_act(s, "hit")
    assert s["finished"] and s["hands"][0]["result"] == "bust" and len(s["dealer"]) == 2   # dealer doesn't draw
    s = deal(monkeypatch, ["6", "5"], ["10", "6"], rest=["10", "9"])
    assert g.bj_extra_bet(s, "double") == 10
    g.bj_act(s, "double")
    assert s["hands"][0]["bet"] == 20 and len(s["hands"][0]["cards"]) == 3 and s["payout"] == 40


def test_split_and_split_aces(monkeypatch):
    s = deal(monkeypatch, ["8", "8"], ["10", "7"], rest=["3", "10", "10"])
    assert "split" in g.bj_actions(s)
    g.bj_act(s, "split")
    assert len(s["hands"]) == 2 and s["active"] == 0 and "split" not in g.bj_actions(s)
    assert "double" in g.bj_actions(s)                             # double after split
    g.bj_act(s, "stand")                                           # 8+3 = 11 stands
    g.bj_act(s, "stand")                                           # 8+10 = 18
    assert s["finished"] and [h["result"] for h in s["hands"]] == ["lose", "win"]
    s = deal(monkeypatch, ["A", "A"], ["10", "7"], rest=["K", "5"])
    g.bj_act(s, "split")                                           # aces: one card each, 21 is not a blackjack
    assert s["finished"] and s["hands"][0]["result"] == "win" and s["hands"][0]["payout"] == 20
    with pytest.raises(g.GameError):
        g.bj_act(s, "hit")


def basic_strategy(state) -> str:
    hand = state["hands"][state["active"]]
    up = g.card_value(state["dealer"][0])
    total, soft = g.hand_total(hand["cards"])
    acts = g.bj_actions(state)
    if "split" in acts and g.card_value(hand["cards"][0]) in (11, 8):
        return "split"
    if soft:
        if total >= 19 or (total == 18 and up <= 8):
            return "stand"
        return "hit"
    if total >= 17 or (total >= 13 and up <= 6) or (total == 12 and 4 <= up <= 6):
        return "stand"
    if "double" in acts and (total == 11 or (total == 10 and up <= 9)):
        return "double"
    return "hit"


def test_blackjack_rtp_is_close_to_a_real_casino():
    rng = random.Random(2024)
    bet, wagered, paid = 100, 0, 0
    for _ in range(20_000):
        shoe = g.new_shoe(rng)
        orig = g.new_shoe
        g.new_shoe = lambda r=None, s=shoe: s
        try:
            s = g.bj_deal(bet)
        finally:
            g.new_shoe = orig
        while not s["finished"]:
            g.bj_act(s, basic_strategy(s))
        wagered += g.bj_wagered(s)
        paid += s["payout"]
    assert 0.97 < paid / wagered < 1.02          # ≈ 99.5 % with full basic strategy (this one is simplified)


# ------------------------------------------------------------ Plugin + website

@pytest.fixture
def casino_env(pb, monkeypatch):  # noqa: F811
    from cogs.player_accounts import PlayerAccounts, _hash
    pb.bot.config = replace(pb.bot.config, player_accounts=True, economy_enabled=True)

    async def no_loop(self):
        return None
    monkeypatch.setattr(PlayerAccounts, "cog_load", no_loop)

    async def setup():
        await pb.bot.add_cog(PlayerAccounts(pb.bot))
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        await pb.m.set_enabled("casino", True)
    asyncio.run(setup())
    pb.db.add_player_session(_hash("tok"), 7, "Gamer7", int(time.time()) + 3600)
    pb.db.add_points(7, 1000, "test")
    return pb


def test_plugin_money_and_limits(casino_env, monkeypatch):
    casino = casino_env.m.running("casino")
    db = casino_env.db
    spin = casino.play_slots(7, 100)
    assert casino.balance(7) == 1000 - 100 + spin.payout
    with pytest.raises(g.GameError, match="between"):
        casino.play_slots(7, 5)                                    # below the smallest bet
    with pytest.raises(g.GameError, match="Not enough"):
        casino.play_slots(7, 5000)
    db.add_points(7, 100_000, "test")
    assert casino._lost_today(7) == 0                              # only tracked while a limit is set
    asyncio.run(casino_env.m.save_settings("casino", {"daily_loss_limit": 250}))
    monkeypatch.setattr(g, "flip_coin", lambda bet, pick, rng=None: ("tails", 0))     # always lose
    casino.play_coinflip(7, 100, "heads")
    casino.play_coinflip(7, 100, "heads")
    with pytest.raises(g.GameError, match="loss limit"):
        casino.play_coinflip(7, 100, "heads")                      # 300 > 250
    casino.play_coinflip(7, 50, "heads")                           # 250 is still allowed
    assert casino._lost_today(7) == 250
    status = casino.status()
    assert status["Rounds played"] >= 2 and status["Points bet"] - status["Points paid out"] == status["House profit"]


def test_blackjack_through_the_plugin(casino_env, monkeypatch):
    casino = casino_env.m.running("casino")
    order = [card("10"), card("6"), card("10"), card("7"), card("5")]
    monkeypatch.setattr(g, "new_shoe", lambda rng=None: list(reversed(order)))
    s = casino.bj_start(7, 100)
    assert not s["finished"] and casino.balance(7) == 900
    with pytest.raises(g.GameError, match="Finish"):
        casino.bj_start(7, 100)
    with pytest.raises(g.GameError, match="older page"):
        casino.bj_action(7, "hit", 99)
    s = casino.bj_action(7, "double", 0)                          # 16 + 5 = 21 vs 17
    assert s["finished"] and s["payout"] == 400 and casino.balance(7) == 900 - 100 + 400


def test_casino_pages(casino_env):
    async def main():
        from web.server import WebInterface
        web = WebInterface(casino_env.bot, env_path=casino_env.tmp / ".env",
                           secret_path=casino_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            page = await (await c.get("/casino")).text()
            assert "Log in to play" in page and 'href="/casino"' in page            # nav link
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            page = await (await c.get("/casino")).text()
            assert "Slots" in page and "Blackjack" in page and "97.3 %" in page and "1 000" in page
            r = await c.post("/casino/slots", data={"bet": "100"})
            text = await r.text()
            assert r.status == 200 and "reel" in text and r.headers["Cache-Control"] == "no-store"
            assert text.count('class="strip"') == 3 and "--t:5.10s" in text and "Spinning" in text   # 5 s spin
            assert "script-src 'self'" in r.headers["Content-Security-Policy"] and "'unsafe-inline'" not in \
                r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]
            src = re.search(r'<script src="(/casino\.js\?v=\w+)" defer>', text)[1]
            js = await c.get(src)
            assert js.content_type == "application/javascript" and "immutable" in js.headers["Cache-Control"]
            assert "fetch(" in await js.text()
            r = await c.post("/casino/slots", data={"bet": "100"}, headers={"Origin": "https://evil.example"})
            assert r.status == 403
            r = await c.post("/casino/slots", data={"bet": "100"}, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
            assert r.status == 200                                                           # privacy mode
            r = await c.post("/casino/slots", data={"bet": "100"}, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
            assert r.status == 403                                                           # sandboxed frame elsewhere
            r = await c.post("/casino/slots", data={"bet": "100"}, headers={"Origin": "null"})
            assert r.status == 403
            r = await c.post("/casino/roulette", data={"bet": "100", "kind": "number", "number": "40"})
            assert "Pick a number from 0 to 36" in await r.text()
            r = await c.post("/casino/dice", data={"bet": "50", "target": "50"})
            assert "rolled" in await r.text()
            r = await c.post("/casino/blackjack", data={"bet": "50", "action": "deal"})
            assert "Dealer" in await r.text()
            assert (await c.get("/casino/coinflip")).status == 200
            await casino_env.m.save_settings("casino", {"slots": False})
            assert (await c.get("/casino/slots")).status == 404
        finally:
            await c.close()
            await casino_env.bot.rcon.close()
    asyncio.run(main())


def test_roulette_wheel_stops_on_the_winning_pocket():
    import re
    from web import casino_pages as cp
    for pocket in (0, 17, 26, 32):
        html = cp._wheel(pocket)
        end = float(re.search(r"--end:(-?[\d.]+)deg", html)[1])
        # the pocket's centre turns to the top (the pointer), within its own slice
        offset = (-end) % 360
        i = cp.WHEEL.index(pocket)
        assert i * cp.STEP <= offset <= (i + 1) * cp.STEP and -end > 360 * 5
    assert "animation:none" in cp._wheel(None)


def test_the_server_decides_and_keeps_its_secrets(casino_env, monkeypatch):
    """Results come only from the server's OS random source: the browser sends its choices, never a seed, and
    the page never shows the dealer's hidden card or the deck."""
    import secrets
    assert isinstance(g.RNG, secrets.SystemRandom)              # OS entropy – there is no seed to know or send
    order = [card("8"), card("7"), card("10"), card("9", 1)]     # dealer's hole card: 9♥
    monkeypatch.setattr(g, "new_shoe", lambda rng=None: [card("2")] * 30 + list(reversed(order)))

    async def main():
        from web.server import WebInterface
        web = WebInterface(casino_env.bot, env_path=casino_env.tmp / ".env",
                           secret_path=casino_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            # extra fields (a "seed", a forced result) are simply ignored
            r = await c.post("/casino/blackjack", data={"bet": "100", "action": "deal", "seed": "1", "card": "A♠"})
            text = await r.text()
            assert "10♠" in text and "9♥" not in text and "2♠" not in text          # hole card + deck stay hidden
            assert "shoe" not in text and casino_env.m.running("casino").bj_state(7)["dealer"][1] == card("9", 1)
            assert set(re.findall(r'<(?:input|select|button)[^>]*\bname="(\w+)"', text)) <= {"action", "step", "bet"}
        finally:
            await c.close()
            await casino_env.bot.rcon.close()
    asyncio.run(main())


# ------------------------------------------------------------ Poker tables

def test_poker_tables_money_flow_and_restart_refund(casino_env):
    from cogs.player_accounts import _hash
    casino = casino_env.m.running("casino")
    db = casino_env.db
    db.add_player_session(_hash("tok8"), 8, "Mate8", int(time.time()) + 3600)
    db.add_points(8, 1000, "test")
    assert set(casino.tables) == {"low-stakes", "high-stakes"}
    t = casino.tables["low-stakes"]
    with pytest.raises(g.GameError, match="Buy in"):
        casino.poker_sit(7, "Gamer7", "low-stakes", 50)                      # below the smallest buy-in
    casino.poker_sit(7, "Gamer7", "low-stakes", 400)
    casino.poker_sit(8, "Mate8", "low-stakes", 300)
    assert casino.balance(7) == 600 and casino.balance(8) == 700
    with pytest.raises(g.GameError, match="already sitting"):
        casino.poker_sit(7, "Gamer7", "high-stakes", 2000)
    assert not t.tick(time.time() + 3) and t.view(7, time.time())["next_in"] > 0      # dealer's timer runs
    assert t.tick(time.time() + 9) and t.in_hand_now
    casino._after(t)
    view7, view8 = casino.poker_view(7, "low-stakes"), casino.poker_view(8, "low-stakes")
    me7 = view7["seats"][view7["you"]]
    assert len(me7["cards"]) == 2 and view8["seats"][view7["you"]]["cards"] is None    # can't see my cards
    actor = t.seats[t.to_act].user_id
    casino.poker_act(actor, "low-stakes", "call")
    # crash: the saved table comes back on the next start and every chip is returned
    saved = casino.data.get("poker")
    assert saved["low-stakes"]["street"] == "preflop"
    asyncio.run(casino_env.m.set_enabled("casino", False))                # stops the plugin: tables closed
    assert casino.balance(7) == 1000 and casino.balance(8) == 1000
    casino.data.set("poker", saved)                                       # as if the bot had crashed instead
    asyncio.run(casino_env.m.set_enabled("casino", True))
    assert casino_env.db.wallet(7)["points"] + casino_env.db.wallet(8)["points"] == 2000 + 400 + 300


def test_poker_pages_and_long_poll(casino_env):
    casino = casino_env.m.running("casino")

    async def main():
        from web.server import WebInterface
        web = WebInterface(casino_env.bot, env_path=casino_env.tmp / ".env",
                           secret_path=casino_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            page = await (await c.get("/casino/poker")).text()
            assert "Low stakes" in page and "Blinds <b>5 / 10</b>" in page
            page = await (await c.get("/casino/poker/low-stakes")).text()
            assert re.search(r'<script src="/poker\.js\?v=\w+" defer>', page) and 'data-table="low-stakes"' in page
            js = await c.get(re.search(r'src="(/poker\.js\?v=\w+)"', page)[1])
            assert js.content_type == "application/javascript"
            r = await (await c.post("/casino/poker/low-stakes/sit", data={"buyin": "300"})).json()
            assert r["ok"] and r["balance"] == 700 and r["state"]["you"] is not None
            r = await (await c.post("/casino/poker/low-stakes/sit", data={"buyin": "300"})).json()
            assert "already sitting" in r["error"]
            v = r["state"]["version"]
            state = await (await c.get(f"/casino/poker/low-stakes/state?v={v - 1}")).json()   # changed: at once
            assert state["version"] == v
            waited = asyncio.create_task(c.get(f"/casino/poker/low-stakes/state?v={v}"))
            await asyncio.sleep(0.2)
            assert not waited.done()                                         # nothing changed: it waits …
            casino.poker_act(7, "low-stakes", "away")                        # … until the table changes
            got = await asyncio.wait_for(waited, 3)
            assert (await got.json())["version"] > v
            r = await (await c.post("/casino/poker/low-stakes/leave", data={})).json()
            assert r["ok"] and r["balance"] == 1000
            assert (await c.post("/casino/poker/low-stakes/sit", data={"buyin": "300"},
                                 headers={"Origin": "https://evil.example"})).status == 403
            assert (await c.get("/casino/poker/nope", allow_redirects=False)).status == 303
        finally:
            await c.close()
            await casino_env.bot.rcon.close()
    asyncio.run(main())


# ------------------------------------------------------------ Plinko

def test_plinko_rtp_is_exactly_97_5_percent_on_every_board():
    from math import comb
    assert set(g.PLINKO_TABLES) == {(r, n) for r in g.PLINKO_RISKS for n in g.PLINKO_ROWS}
    for (risk, rows), table in g.PLINKO_TABLES.items():
        assert len(table) == rows + 1 and table == table[::-1] and min(table) > 0
        assert all(table[k] >= table[k + 1] for k in range(rows // 2)), (risk, rows)   # biggest at the edges
        rtp = Fraction(sum(comb(rows, k) * c for k, c in enumerate(table)), 100 * 2 ** rows)
        assert rtp == g.PLINKO_RTP == Fraction(39, 40), (risk, rows)
    for rows in g.PLINKO_ROWS:                                       # more risk = bigger edges
        assert g.PLINKO_TABLES[("low", rows)][0] < g.PLINKO_TABLES[("medium", rows)][0] \
            < g.PLINKO_TABLES[("high", rows)][0]
    assert g.fmt_multiplier(144600) == "1446" and g.fmt_multiplier(30) == "0.3" and g.fmt_multiplier(105) == "1.05"


def test_plinko_ball_follows_its_path():
    class Path(random.Random):
        def __init__(self, bits):
            super().__init__(0)
            self.bits = list(bits)

        def randrange(self, n):
            return self.bits.pop(0) if self.bits else 0
    d = g.plinko_drop(100, "high", 8, Path([1] * 8))                 # always right: the right edge
    assert d["slot"] == 8 and d["path"] == [1] * 8 and d["payout"] == 3890
    d = g.plinko_drop(100, "low", 8, Path([0, 1] * 4))
    assert d["slot"] == 4 and d["payout"] == 52
    with pytest.raises(ValueError):
        g.plinko_drop(100, "extreme", 8)


def test_plinko_many_balls_through_the_plugin(casino_env):
    casino = casino_env.m.running("casino")
    drops = casino.play_plinko(7, 10, "medium", 12, 5)
    assert len(drops) == 5 and all(len(d["path"]) == 12 and d["slot"] == sum(d["path"]) for d in drops)
    assert casino.balance(7) == 1000 - 50 + sum(d["payout"] for d in drops)
    assert casino.data.get("stats:plinko")["rounds"] == 5
    assert casino.data.get("stats:plinko")["wagered"] == 50
    with pytest.raises(g.GameError, match="1 to 20 balls"):
        casino.play_plinko(7, 10, "medium", 12, 21)
    with pytest.raises(g.GameError, match="between"):
        casino.play_plinko(7, 5, "medium", 12, 3)                    # the bet range is per ball
    with pytest.raises(g.GameError, match="Not enough"):
        casino.play_plinko(7, 1000, "low", 8, 20)
    with pytest.raises(g.GameError, match="rows"):
        casino.play_plinko(7, 10, "low", 7, 1)
    with pytest.raises(g.GameError, match="risk"):
        casino.play_plinko(7, 10, "huge", 8, 1)
    asyncio.run(casino_env.m.save_settings("casino", {"daily_loss_limit": 100}))
    with pytest.raises(g.GameError, match="loss limit"):
        casino.play_plinko(7, 10, "low", 8, 11)                      # 110 at once is over the limit


def test_plinko_pages(casino_env):
    async def main():
        import json
        from web.server import WebInterface
        web = WebInterface(casino_env.bot, env_path=casino_env.tmp / ".env",
                           secret_path=casino_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            assert "97.5 %" in await (await c.get("/casino")).text()
            page = await (await c.get("/casino/plinko")).text()
            assert "<canvas" in page and 'name="balls"' in page and page.count('name="risk"') == 3
            assert "127×" in page                                     # medium, 16 rows by default
            src = re.search(r'<script src="(/plinko\.js\?v=\w+)" defer>', page)[1]
            assert "requestAnimationFrame" in await (await c.get(src)).text()
            r = await c.post("/casino/plinko/drop", data={"bet": "10", "balls": "4", "risk": "high", "rows": "8",
                                                          "seed": "1", "slot": "0"})
            data = await r.json()
            assert data["ok"] and len(data["balls"]) == 4 and data["bet"] == 10
            assert data["balance"] == 1000 - 40 + sum(b["payout"] for b in data["balls"])
            assert all(b["slot"] == sum(b["path"]) and len(b["path"]) == 8 for b in data["balls"])
            r = await c.post("/casino/plinko/drop", data={"bet": "10", "balls": "99", "risk": "low", "rows": "8"})
            assert "balls at once" in (await r.json())["error"]
            r = await c.post("/casino/plinko", data={"bet": "10", "balls": "2", "risk": "low", "rows": "9"})
            text = await r.text()
            assert "Ball 2:" in text and 'value="9" selected' in text                # works without the script
            r = await c.post("/casino/plinko/drop", data={"bet": "10"}, headers={"Origin": "https://evil.example"})
            assert r.status == 403
            json.loads(re.search(r'data-tables="([^"]+)"', page)[1].replace("&quot;", '"'))
            await casino_env.m.save_settings("casino", {"plinko": False})
            assert (await c.get("/casino/plinko")).status == 404
        finally:
            await c.close()
            await casino_env.bot.rcon.close()
    asyncio.run(main())


def test_plinko_on_discord(casino_env, monkeypatch):
    """/plinko: bets taken, an animated board (GIF) in an embed, the result added once the balls landed."""
    import io
    from types import SimpleNamespace as NS

    from PIL import Image

    import plugins.casino as cm
    casino = casino_env.m.running("casino")
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(cm.asyncio, "sleep", no_sleep)

    def interaction():
        out = NS(ephemeral=[], sent=[], edits=[], deferred=False)

        async def send_message(text, ephemeral=False):
            out.ephemeral.append(text)

        async def defer(thinking=False):
            out.deferred = True

        class Msg:
            async def edit(self, embed=None, view=None):
                out.edits.append((embed, view))

        async def followup_send(embed=None, file=None, wait=False):
            out.sent.append((embed, file))
            return Msg()
        out.it = NS(user=NS(id=7, display_name="Gamer7"), response=NS(send_message=send_message, defer=defer),
                    followup=NS(send=followup_send))
        return out

    o = interaction()
    asyncio.run(casino.discord_plinko(o.it, 10, 3, "high", 12))
    embed, file = o.sent[0]
    assert o.deferred and file.filename == "plinko.gif" and embed.image.url == "attachment://plinko.gif"
    assert "Dropping" in embed.description and "3 × 10 points" in embed.description
    gif = Image.open(io.BytesIO(file.fp.read()))
    assert gif.format == "GIF" and gif.n_frames > 20 and "loop" not in gif.info          # plays once
    final, view = o.edits[0]
    assert slept and "Dropping" not in final.description and final.description.count("×**") == 3
    assert "Balance:" in final.footer.text and isinstance(view, cm.PlinkoAgain)
    paid = casino.data.get("stats:plinko")
    assert casino.balance(7) == 1000 - 30 + paid["paid"] and paid["rounds"] == 3

    other = interaction()                                     # someone else can't press my "Drop again"
    other.it.user = NS(id=8, display_name="X")
    assert asyncio.run(view.interaction_check(other.it)) is False and "your own" in other.ephemeral[0]
    o2 = interaction()
    asyncio.run(view.again.callback(o2.it))
    assert len(o2.sent) == 1 and casino.data.get("stats:plinko")["rounds"] == 6

    o3 = interaction()
    asyncio.run(casino.discord_plinko(o3.it, 10, 50, "low", 8))                # over the most balls
    assert "balls at once" in o3.ephemeral[0] and not o3.sent
    asyncio.run(casino_env.m.save_settings("casino", {"plinko_discord": False}))
    o4 = interaction()
    asyncio.run(casino.discord_plinko(o4.it, 10, 1, "low", 8))
    assert "switched off" in o4.ephemeral[0]


def _fake_interaction(user_id=7):
    from types import SimpleNamespace as NS
    out = NS(ephemeral=[], sent=[], edits=[], responded=[], deferred=False)

    async def send_message(text=None, ephemeral=False, embed=None, file=None, view=None):
        (out.ephemeral if ephemeral else out.responded).append(text if ephemeral else (embed, file, view))

    async def edit_message(embed=None, attachments=None, view=None):
        out.edits.append((embed, attachments, view))

    async def defer(thinking=False):
        out.deferred = True

    class Msg:
        async def edit(self, embed=None, view=None):
            out.edits.append((embed, None, view))

    async def followup_send(embed=None, file=None, wait=False):
        out.sent.append((embed, file))
        return Msg()
    out.it = NS(user=NS(id=user_id, display_name="Gamer7"),
                response=NS(send_message=send_message, defer=defer, edit_message=edit_message),
                followup=NS(send=followup_send))
    return out


def test_quick_games_on_discord(casino_env, monkeypatch):
    """/slots, /coinflip, /dice, /roulette: the bet is settled, an animated GIF, then the result + Play again."""
    import io

    from PIL import Image

    import plugins.casino as cm
    casino = casino_env.m.running("casino")

    async def no_sleep(seconds):
        return None
    monkeypatch.setattr(cm.asyncio, "sleep", no_sleep)
    for game, choice, word in (("slots", (), "bet 10 points"), ("coinflip", ("tails",), "on **Tails**"),
                               ("dice", (30,), "30 % chance (pays 3.17×)"), ("roulette", ("red", None), "on **Red**"),
                               ("race", (2,), "on **3. Bradley** (pays 5.59×)")):
        before, paid_before = casino.balance(7), (casino.data.get(f"stats:{game}") or {}).get("paid", 0)
        o = _fake_interaction()
        asyncio.run(casino.discord_quick(o.it, game, 10, *choice))
        embed, file = o.sent[0]
        assert o.deferred and file.filename == "game.gif" and word in embed.description, game
        gif = Image.open(io.BytesIO(file.fp.read()))
        assert gif.format == "GIF" and gif.n_frames > 30 and "loop" not in gif.info, game     # plays once
        final, _, view = o.edits[0]
        paid = casino.data.get(f"stats:{game}")["paid"] - paid_before
        assert casino.balance(7) == before - 10 + paid, game
        assert "Balance:" in final.footer.text and isinstance(view, cm.PlayAgain) and view.game == game
        assert ("won" in final.description) == (paid > 10), game
    o = _fake_interaction()
    asyncio.run(casino.discord_quick(o.it, "roulette", 10, "number", None))
    assert "Pick a number" in o.ephemeral[0] and not o.sent
    o = _fake_interaction()
    asyncio.run(casino.discord_quick(o.it, "slots", 5))
    assert "between" in o.ephemeral[0]
    asyncio.run(casino_env.m.save_settings("casino", {"discord_games": False}))
    o = _fake_interaction()
    asyncio.run(casino.discord_quick(o.it, "dice", 10, 50))
    assert "switched off" in o.ephemeral[0]


def test_blackjack_on_discord_with_buttons(casino_env, monkeypatch):
    casino = casino_env.m.running("casino")
    order = [card("10"), card("6"), card("10"), card("7"), card("5")]           # 16 vs 17, then a 5 → 21
    monkeypatch.setattr(g, "new_shoe", lambda rng=None: list(reversed(order)))
    o = _fake_interaction()
    asyncio.run(casino.blackjack_command.callback(casino, o.it, 100))
    embed, file, view = o.responded[0]
    labels = [b.label for b in view.children]
    assert labels == ["Hit", "Stand", "Double"] and file.filename == "table.png"
    assert "Your move: Hit, Stand, Double" in embed.description and casino.balance(7) == 900
    o2 = _fake_interaction()
    asyncio.run(casino.blackjack_command.callback(casino, o2.it, 50))            # unfinished: shows the same hand
    assert "unfinished" in o2.responded[0][0].description and casino.balance(7) == 900
    stranger = _fake_interaction(8)
    assert asyncio.run(view.interaction_check(stranger.it)) is False
    o3 = _fake_interaction()
    asyncio.run(view.children[2].callback(o3.it))                               # Double: 16 + 5 = 21 vs 17
    embed, files, view2 = o3.edits[0]
    assert "**Win**" in embed.description and "won" in embed.description and files[0].filename == "table.png"
    assert casino.balance(7) == 900 - 100 + 400 and [b.label for b in view2.children] == ["Deal again"]
    o4 = _fake_interaction()
    asyncio.run(view.children[0].callback(o4.it))                               # old button: refused
    assert "no hand running" in o4.ephemeral[0]



# ------------------------------------------------------------ Horse racing

def test_racing_rtp_is_exactly_95_percent_on_every_horse():
    assert sum(chance for _, _, chance in g.HORSES) == 100
    for i, (_, _, chance) in enumerate(g.HORSES):
        assert Fraction(chance, 100) * g.horse_pays(i) == Fraction(95, 100)        # every horse: 95 %
    for bet, i in ((100, 0), (7, 5), (1000, 3)):                                  # fair rounding: exact on average
        chance = g.HORSES[i][2]
        assert sum(g.fair(bet * 95, chance, Fixed(k)) for k in range(chance)) == bet * 95
    with pytest.raises(ValueError):
        g.run_race(10, 6)


def test_race_winners_follow_the_chances_and_the_order_is_complete():
    rng = random.Random(42)
    wins = [0] * len(g.HORSES)
    n = 60_000
    for _ in range(n):
        race = g.run_race(100, 0, rng)
        assert sorted(race.order) == list(range(len(g.HORSES)))                   # every horse finishes once
        assert (race.payout > 0) == (race.order[0] == 0)
        wins[race.order[0]] += 1
    for i, (_, _, chance) in enumerate(g.HORSES):
        assert abs(wins[i] / n - chance / 100) < 0.01, (i, wins[i] / n)


def test_race_paths_cross_the_line_in_the_drawn_order():
    for seed in range(50):
        rng = random.Random(seed)
        order = list(range(len(g.HORSES)))
        rng.shuffle(order)
        paths = g.race_paths(order, rng)
        finishes = [paths[h]["finish"] for h in order]
        assert finishes == sorted(finishes) and len(set(finishes)) == len(finishes) and finishes[0] == 1.0
        for h, path in paths.items():
            pts = path["points"]
            assert pts[0] == (0.0, 0.0) and pts[-1] == (path["finish"], 1.0)
            assert all(b[1] >= a[1] and b[0] > a[0] for a, b in zip(pts, pts[1:]))   # only forward
            assert all(p < 1.0 for _, p in pts[:-1])                                   # at the line only at the end


def test_racing_through_the_plugin_and_the_website(casino_env):
    casino = casino_env.m.running("casino")
    race = casino.play_race(7, 100, 4)
    assert casino.balance(7) == 1000 - 100 + race.payout
    with pytest.raises(g.GameError, match="Pick a horse"):
        casino.play_race(7, 100, 9)
    assert casino.balance(7) == 1000 - 100 + race.payout                           # nothing taken for a bad pick

    async def main():
        from web.server import WebInterface
        web = WebInterface(casino_env.bot, env_path=casino_env.tmp / ".env",
                           secret_path=casino_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            page = await (await c.get("/casino")).text()
            assert "Horse racing" in page and 'href="/casino/race"' in page
            page = await (await c.get("/casino/race")).text()
            assert page.count('class="lane') == 6 and "Oil Rig Ollie" in page and "11.88×" in page and "8 % win chance" in page
            r = await c.post("/casino/race", data={"bet": "50", "horse": "2"})
            text = await r.text()
            assert r.status == 200 and "@keyframes hr0" in text and "@keyframes hr5" in text and "wins!" in text
            assert 'class="lane mine"' in text and text.count('class="place reveal') == 6
            # the race's keyframes are inside the game card – casino.js swaps in only the card (no page reload)
            assert text.index('<div class="casino">') < text.index("@keyframes hr0")
            assert 'value="2" checked' in text                                         # the same horse again
            r = await c.post("/casino/race", data={"bet": "50", "horse": "x"})
            assert "Pick a horse" in await r.text()
            await casino_env.m.save_settings("casino", {"race": False})
            assert (await c.get("/casino/race")).status == 404
        finally:
            await c.close()
            await casino_env.bot.rcon.close()
    asyncio.run(main())
