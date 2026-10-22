"""Casino pages of the player website (/casino …): play the plugins/casino.py games with your points.

Server-rendered forms that work on their own; casino.js (website_assets/, scripts only from the site itself) plays
without reloading the page, counts the balance and fires confetti on a win. Players log in with the website's player
login; the login cookie is SameSite=Lax, so other websites can't place bets for them, and a POST naming another
origin is refused.
"""
from __future__ import annotations

import random
from urllib.parse import urlparse

from aiohttp import web

from support import casino_games as g
from support.casino_games import GameError
from web.account_pages import COOKIE
from web.store_pages import STORE_CSS, esc, notice

SPIN = 5.0                  # seconds a spin runs before the result shows
WHEEL = (0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22,
         18, 29, 7, 28, 12, 35, 3, 26)            # European wheel order, clockwise from the top
STEP = 360 / len(WHEEL)
CELL = 84                   # px: height of a slot/dice window

# Animations are CSS (they also run without the script) and only move transform/opacity, so they stay smooth.
# The result, the new balance and the play button appear when the spin ends (var --t); "reduce motion" skips it all.
CASINO_CSS = STORE_CSS + """
.games{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.game{display:block;text-decoration:none;text-align:center;padding:20px 12px}
.game .ico{font-size:2.6rem;line-height:1.2;transition:transform .25s}.game:hover .ico{transform:scale(1.15) rotate(-6deg)}
.game h2{margin:6px 0 2px}.rtp{font-size:.85rem;color:var(--muted)}
.bal{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.swap{display:inline-grid}.swap>*{grid-area:1/1}
.reveal{animation:reveal .45s cubic-bezier(.2,.9,.3,1.3) var(--t,0s) both}
.hide-at-end{animation:vanish .2s linear var(--t,0s) both}
@keyframes reveal{from{opacity:0;transform:scale(.6);visibility:hidden}to{opacity:1;transform:none;visibility:visible}}
@keyframes vanish{from{opacity:1}to{opacity:0;visibility:hidden}}
.lockwrap{position:relative}.lockwrap .pay{animation:unlock 0s linear var(--t,0s) both}
.lockwrap .spinning{position:absolute;inset:auto 0 0 0;height:46px;display:flex;align-items:center;justify-content:center;
font-family:var(--head);letter-spacing:.2em;text-transform:uppercase;color:var(--gold);
animation:vanish 0s linear var(--t,0s) both}
@keyframes unlock{from{visibility:hidden}to{visibility:visible}}
.reels{display:flex;justify-content:center;gap:10px;margin:6px 0 14px;padding:10px;border-radius:12px;
background:linear-gradient(#1a0f0a,#0b0705);box-shadow:inset 0 0 0 2px rgba(247,147,30,.35),0 0 26px rgba(229,52,28,.25)}
.reel{width:84px;height:84px;overflow:hidden;position:relative;border-radius:8px;
background:linear-gradient(#f6efe6,#d9cfc2);box-shadow:inset 0 10px 14px rgba(0,0,0,.35),inset 0 -10px 14px rgba(0,0,0,.35)}
.strip{display:flex;flex-direction:column;animation:roll var(--d,5s) cubic-bezier(.15,.75,.2,1.02) both;will-change:transform}
.strip>div{height:84px;flex:none;display:flex;align-items:center;justify-content:center;font-size:2.8rem}
.dice .strip>div{font-family:var(--head);font-size:2.6rem;color:#111}
@keyframes roll{from{transform:translateY(0)}to{transform:translateY(var(--end))}}
.payline{position:absolute;left:0;right:0;top:50%;height:2px;background:rgba(229,52,28,.5)}
.result{text-align:center;font-family:var(--head);font-size:1.6rem;letter-spacing:.05em;text-transform:uppercase;
margin:4px 0}
.result.win{color:var(--ok);text-shadow:0 0 16px rgba(61,220,106,.6)}.result.lose{color:var(--muted)}
.result.win{animation:reveal .45s cubic-bezier(.2,.9,.3,1.3) var(--t,0s) both,pulse 1s ease-in-out calc(var(--t,0s) + .5s) 3}
@keyframes pulse{50%{transform:scale(1.08)}}
.big-num{font-family:var(--head);font-size:3rem;text-align:center;line-height:1.1}
.stage{perspective:700px;display:flex;justify-content:center;padding:80px 0 6px}
.coin{width:120px;height:120px;position:relative;transform-style:preserve-3d;
animation:flip var(--t,5s) cubic-bezier(.2,.7,.25,1) both}
.coin .face{position:absolute;inset:0;border-radius:50%;backface-visibility:hidden;-webkit-backface-visibility:hidden;
display:flex;align-items:center;justify-content:center;font-size:3.6rem;
background:radial-gradient(circle at 35% 30%,#ffe9a8,#e0a526 55%,#9c6a08);box-shadow:inset 0 0 0 6px #b77d0d}
.coin .tails{transform:rotateY(180deg)}
@keyframes flip{0%{transform:translateY(0) rotateY(0)}40%{transform:translateY(-64px) rotateY(calc(var(--end) * .6))}
100%{transform:translateY(0) rotateY(var(--end))}}
.die{font-size:2.4rem;display:inline-block;animation:tumble var(--t,5s) cubic-bezier(.2,.7,.25,1) both}
@keyframes tumble{from{transform:rotate(0)}to{transform:rotate(1800deg)}}
.dicebox{display:flex;align-items:center;justify-content:center;gap:16px;margin:8px 0}
.wheel-wrap{position:relative;width:260px;height:260px;margin:8px auto 4px}
.wheel-wrap::before{content:"";position:absolute;left:50%;top:-6px;margin-left:-11px;z-index:3;border:11px solid transparent;
border-top:20px solid var(--gold);filter:drop-shadow(0 2px 2px #000)}
.wheel{width:260px;height:260px;border-radius:50%;position:relative;border:8px solid #5a2e0e;
box-shadow:0 0 0 2px var(--gold),0 0 30px rgba(229,52,28,.35);
animation:spin var(--t,5s) cubic-bezier(.12,.62,.12,1) both;will-change:transform}
.wheel b{position:absolute;left:50%;top:50%;width:20px;margin:-8px 0 0 -10px;text-align:center;font-size:.72rem;color:#fff;
transform:rotate(var(--a)) translateY(-106px) rotate(0deg)}
.wheel::after{content:"";position:absolute;inset:34%;border-radius:50%;
background:radial-gradient(circle at 40% 35%,#f7c35b,#7a4a10 70%);box-shadow:0 0 0 4px #3b1e08}
@keyframes spin{from{transform:rotate(0)}to{transform:rotate(var(--end))}}
.pocket{display:inline-flex;width:64px;height:64px;border-radius:50%;align-items:center;justify-content:center;
font-family:var(--head);font-size:1.8rem;color:#fff;border:3px solid var(--gold)}
.pocket.red{background:#c1121f}.pocket.black{background:#2a2a2a;box-shadow:inset 0 0 0 3px #000}
.pocket.green{background:#127a3a}.pocket-label{text-align:center;color:var(--muted);margin-top:4px}
.cards{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0;perspective:600px}.cardx{width:56px;height:80px;background:#f6f1ea;
color:#111;border-radius:6px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:1.25rem;
box-shadow:0 2px 6px rgba(0,0,0,.5)}.cardx.red{color:#c1121f}.cardx.back{background:repeating-linear-gradient(45deg,
#7a1d0e 0 6px,#a8290f 6px 12px);color:transparent}
.cardx.deal{animation:deal .5s cubic-bezier(.2,.8,.3,1) var(--at,0s) both}
.cardx.turn{animation:turn .5s ease-out var(--at,0s) both}
@keyframes deal{from{opacity:0;transform:translate(160px,-90px) rotate(18deg)}to{opacity:1;transform:none}}
@keyframes turn{from{transform:rotateY(90deg)}to{transform:none}}
.hand{padding:10px;border:1px solid var(--line);margin:8px 0}.hand.active{border-color:var(--orange)}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch}.acts form{margin:0}
.acts button{min-width:110px;width:auto;margin:0;height:100%;padding:11px 18px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media (max-width:520px){.row2{grid-template-columns:1fr}}
table.pay td{padding:4px 6px}
@media (prefers-reduced-motion:reduce){.casino *,.casino *::before,.casino *::after{animation-duration:.01s!important;
animation-delay:0s!important;animation-iteration-count:1!important}}
"""
RESULT_WORDS = {"win": "Win", "blackjack": "Blackjack!", "push": "Push – bet back", "lose": "Lose", "bust": "Bust"}
FX = random.Random()        # only for the look of the spins (filler symbols) – results come from the game RNG


def _page(site, base: str, title: str, body: str, status: int = 200) -> web.Response:
    script = f'<script src="{base}/casino.js?v={site.asset_version("casino.js")}" defer></script>'
    page = site.page(base, "casino", title, f'<h1>{esc(title)}</h1><div class="casino">{body}</div>{script}')
    return web.Response(status=status, text=page.replace("</style>", CASINO_CSS + "</style>", 1),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


def _same_origin(request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None or origin == "null":   # none / privacy mode: the SameSite=Lax login cookie still protects
        return True
    return (urlparse(origin).netloc or "").lower() == (request.host or "").lower()


def _bet_field(casino, value: int | None) -> str:
    low, high = casino.setting("min_bet"), casino.setting("max_bet")
    return (f'<label class="field"><span>Bet ({esc(casino.fmt_points(low))} – {esc(casino.fmt_points(high))})</span>'
            f'<input type="number" name="bet" min="{low}" max="{high}" step="1" inputmode="numeric" required '
            f'value="{value or low}"></label>')


def _button(text: str, spinning: bool) -> str:
    """The play button – while a spin runs it's hidden behind a "Spinning …" label."""
    if not spinning:
        return f'<button class="pay" type="submit">{text}</button>'
    return (f'<div class="lockwrap"><button class="pay" type="submit">{text}</button>'
            f'<div class="spinning">Spinning …</div></div>')


def _balance(casino, uid: int, base: str, back: bool = True, before: int | None = None, t: float = 0) -> str:
    """Balance bar; with `before` the old balance shows until the spin ends, then the new one pops in."""
    link = f'<a href="{base}/casino">← All games</a>' if back else ""
    now = casino.balance(uid)
    points = f'<span class="price">{esc(casino.fmt_points(now))}</span>'
    if before is not None and before != now:
        points = (f'<span class="swap" style="--t:{t:.2f}s" data-before="{before}" data-after="{now}">'
                  f'<span class="price hide-at-end">'
                  f'{esc(casino.fmt_points(before))}</span><span class="price reveal">'
                  f'{esc(casino.fmt_points(now))}</span></span>')
    return f'<div class="card bal"><div>Your balance: {points}</div>{link}</div>'


def _outcome(casino, payout: int, bet: int) -> str:
    if payout > bet:
        return f'<div class="result win">You win {esc(casino.fmt_points(payout))}!</div>'
    if payout == bet:
        return '<div class="result reveal">Bet back</div>'
    if payout:
        return f'<div class="result lose reveal">{esc(casino.fmt_points(payout))} back</div>'
    return '<div class="result lose reveal">No luck this time</div>'


def _int(value, default: int | None = None) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.lstrip("-").isdigit() and len(text) < 12 else default


def _strip(final: str, fillers, count: int, seconds: float) -> str:
    """A spinning column that ends on `final` (the real result)."""
    cells = [FX.choice(fillers) for _ in range(count - 1)] + [final]
    end = -(count - 1) * CELL
    return (f'<div class="strip" style="--end:{end}px;--d:{seconds:.2f}s">'
            + "".join(f"<div>{c}</div>" for c in cells) + "</div>")


# ------------------------------------------------------------ Pages

def lobby(site, casino, base: str, uid: int) -> web.Response:
    tiles = "".join(f'<a class="card game" href="{base}/casino/{gid}"><div class="ico">{icon}</div><h2>{esc(name)}</h2>'
                    f'<div class="rtp">Return to player {esc(rtp)}</div></a>'
                    for gid, (name, icon, rtp) in casino_games(casino).items())
    body = (_balance(casino, uid, base, back=False)
            + (f'<div class="games">{tiles}</div>' if tiles else '<p class="muted">All games are closed right now.</p>')
            + '<p class="muted">Play money only: points are earned on the server and can\'t be bought or cashed out. '
              'Results come from the server\'s secure random generator.</p>')
    return _page(site, base, "Casino", body)


def casino_games(casino) -> dict:
    from plugins.casino import GAMES
    return {gid: GAMES[gid] for gid in casino.enabled_games()}


def slots_page(site, casino, base, uid, *, bet=None, spin=None, error="", before=None) -> web.Response:
    if spin:      # the three reels stop one after another; the last one stops after SPIN seconds
        reels = "".join(f'<div class="reel">{_strip(r, g.SLOT_SYMBOLS, 18 + 7 * i, SPIN * (0.62 + 0.19 * i))}'
                        f'<div class="payline"></div></div>' for i, r in enumerate(spin.reels))
    else:
        reels = "".join(f'<div class="reel"><div class="strip" style="animation:none"><div>{r}</div></div></div>'
                        for r in ("🍒", "7️⃣", "💎"))
    result = _outcome(casino, spin.payout, bet) if spin else ""
    pays = "".join(f"<tr><td>{sym}</td><td class='r'>{pay}×</td></tr>" for sym, pay, _ in g.SLOT_WINS)
    t = SPIN + 0.1 if spin else 0
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s"><div class="reels">{reels}</div>{result}'
              f'<form method="post" action="{base}/casino/slots">{_bet_field(casino, bet)}'
              f'{_button("🎰 Spin", bool(spin))}</form></div>'
            + f'<div class="card"><h2>Pays</h2><table class="pay">{pays}</table>'
              f'<p class="muted">Return to player 95 %.</p></div>')
    return _page(site, base, "Slots", body)


def coinflip_page(site, casino, base, uid, *, bet=None, pick="heads", side=None, payout=0, error="", before=None):
    end = 3600 + (180 if side == "tails" else 0)
    shown = (f'<div class="stage"><div class="coin" style="--end:{end}deg{"" if side else ";animation:none"}">'
             '<div class="face heads">👑</div><div class="face tails">🦅</div></div></div>')
    if side:
        shown += f'<div class="result reveal">{side}</div>' + _outcome(casino, payout, bet)
    picks = "".join(f'<label><input type="radio" name="pick" value="{p}"{" checked" if p == pick else ""}> '
                    f'{p.title()} {"👑" if p == "heads" else "🦅"}</label> ' for p in ("heads", "tails"))
    t = SPIN if side else 0
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s">{shown}<form method="post" action="{base}/casino/coinflip">'
              f'<p>{picks}</p>{_bet_field(casino, bet)}{_button("🪙 Flip", bool(side))}</form>'
              '<p class="muted">Right side pays 1.9× your bet. Return to player 95 %.</p></div>')
    return _page(site, base, "Coin flip", body)


def dice_page(site, casino, base, uid, *, bet=None, target=50, roll=None, payout=0, error="", before=None):
    shown = ""
    if roll is not None:
        numbers = [str(n) for n in range(1, 101)]
        shown = (f'<div class="dicebox"><span class="die">🎲</span><div class="reels dice"><div class="reel">'
                 f'{_strip(str(roll), numbers, 34, SPIN)}</div></div><span class="die">🎲</span></div>'
                 f'<div class="result reveal">rolled {roll} – needed {target} or less</div>'
                 + _outcome(casino, payout, bet))
    t = SPIN if roll is not None else 0
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s">{shown}<form method="post" action="{base}/casino/dice">'
              f'<div class="row2"><label class="field"><span>Win chance in % ({g.DICE_MIN}–{g.DICE_MAX}) – roll '
              f'1–100 at or under it</span><input type="number" name="target" min="{g.DICE_MIN}" '
              f'max="{g.DICE_MAX}" value="{target}" required></label>{_bet_field(casino, bet)}</div>'
              f'{_button("🎲 Roll", roll is not None)}</form><p class="muted">Pays 95 ÷ win chance × your bet '
              '(50 % → 1.9×, 10 % → 9.5×). Return to player 95 %.</p></div>')
    return _page(site, base, "Dice", body)


def _wheel(pocket: int | None) -> str:
    stops = []
    for i, n in enumerate(WHEEL):
        colour = {"green": "#127a3a", "red": "#b3121d", "black": "#161616"}[g.roulette_colour(n)]
        stops.append(f"{colour} {i * STEP:.3f}deg {(i + 1) * STEP:.3f}deg")
    labels = "".join(f'<b style="--a:{(i + 0.5) * STEP:.2f}deg">{n}</b>' for i, n in enumerate(WHEEL))
    if pocket is None:
        motion = "animation:none"
    else:     # several turns, then the winning pocket stops under the pointer (a little off-centre, like a real ball)
        i = WHEEL.index(pocket)
        end = -(360 * 6 + (i + 0.5) * STEP + FX.uniform(-0.3, 0.3) * STEP)
        motion = f"--end:{end:.2f}deg"
    return (f'<div class="wheel-wrap"><div class="wheel" style="background:conic-gradient({",".join(stops)});'
            f'{motion}">{labels}</div></div>')


def roulette_page(site, casino, base, uid, *, bet=None, kind="red", number=None, pocket=None, payout=0, error="",
                  before=None):
    shown = _wheel(pocket)
    if pocket is not None:
        shown += (f'<div class="reveal" style="text-align:center"><span class="pocket {g.roulette_colour(pocket)}">'
                  f'{pocket}</span><div class="pocket-label">{pocket} · {g.roulette_colour(pocket)}</div></div>'
                  + _outcome(casino, payout, bet))
    options = "".join(f'<option value="{k}"{" selected" if k == kind else ""}>{esc(label)} – pays {pay - 1}:1'
                      f'</option>' for k, (label, pay) in g.ROULETTE_BETS.items())
    t = SPIN if pocket is not None else 0
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s">{shown}<form method="post" action="{base}/casino/roulette">'
              f'<div class="row2"><label class="field"><span>Bet on</span><select name="kind">{options}</select>'
              f'</label><label class="field"><span>Number (only for "Single number")</span><input type="number" '
              f'name="number" min="0" max="36" value="{"" if number is None else number}"></label></div>'
              f'{_bet_field(casino, bet)}{_button("🎡 Spin", pocket is not None)}</form>'
              '<p class="muted">European wheel with one zero (0–36). On 0 every bet except "0" loses. '
              'Return to player 97.3 % – like a real casino.</p></div>')
    return _page(site, base, "Roulette", body)


def _card(c: int, cls: str = "", at: float | None = None) -> str:
    label = g.card_label(c)
    style = f' style="--at:{at:.2f}s"' if at is not None else ""
    return f'<div class="cardx{" red" if label[-1] in "♥♦" else ""}{cls}"{style}>{esc(label)}</div>'


def blackjack_page(site, casino, base, uid, *, bet=None, error="", before=None, animate=False) -> web.Response:
    state = casino.bj_state(uid)
    table, t = "", 0.0
    if state is not None:
        playing = not state["finished"]
        moved = state.get("moved") if animate else None
        dealt = moved == "deal"
        # player cards: on a deal all four cards fly in one by one, later only the new card(s)
        anim: dict[tuple, float] = {}
        if dealt:
            anim = {("p", 0, 0): 0.0, ("d", 0): 0.4, ("p", 0, 1): 0.8, ("d", 1): 1.2}
            t = 1.7
        elif moved:
            for hi in moved:
                anim[("p", hi, len(state["hands"][hi]["cards"]) - 1)] = 0.0
                if len(moved) > 1:        # a split: both hands get their second card
                    anim[("p", hi, 1)] = 0.35 * hi
            t = 0.6
        dealer_cards = []
        for i, c in enumerate(state["dealer"]):
            if playing and i == 1:
                at = anim.get(("d", 1))
                style = f' style="--at:{at:.2f}s"' if at is not None else ""
                dealer_cards.append(f'<div class="cardx back{" deal" if at is not None else ""}"{style}>?</div>')
            elif not playing and animate and i >= 1 and not dealt:
                at = t + 0.6 * (i - 1)                       # the hole card turns, then the dealer draws
                dealer_cards.append(_card(c, " turn" if i == 1 else " deal", at))
            elif ("d", i) in anim:
                dealer_cards.append(_card(c, " deal", anim[("d", i)]))
            else:
                dealer_cards.append(_card(c))
        if not playing and animate and not dealt:
            t += 0.6 * (len(state["dealer"]) - 1) + 0.2
        elif not playing and dealt:
            t += 0.3
        dealer_total = "?" if playing else (f'<span class="swap"><span class="hide-at-end">?</span>'
                                            f'<span class="reveal">{g.hand_total(state["dealer"])[0]}</span></span>')
        table = f'<h2>Dealer – {dealer_total}</h2><div class="cards">{"".join(dealer_cards)}</div>'
        for i, hand in enumerate(state["hands"]):
            total, soft = g.hand_total(hand["cards"])
            active = playing and i == state["active"]
            label = f"Hand {i + 1}" if len(state["hands"]) > 1 else "You"
            result = (f'<span class="reveal"> – <b>{RESULT_WORDS[hand["result"]]}</b> '
                      f'({esc(casino.fmt_points(hand["payout"]))})</span>' if "result" in hand else "")
            cards = "".join(_card(c, " deal", anim[("p", i, j)]) if ("p", i, j) in anim else _card(c)
                            for j, c in enumerate(hand["cards"]))
            table += (f'<div class="hand{" active" if active else ""}"><b>{label}</b> – {total}'
                      f'{" soft" if soft and total < 21 else ""} · bet {esc(casino.fmt_points(hand["bet"]))}'
                      f'{" (doubled)" if hand["doubled"] else ""}{result}<div class="cards">{cards}</div></div>')
        if playing:
            names = {"hit": "Hit", "stand": "Stand", "double": "Double", "split": "Split"}
            buttons = "".join(f'<form method="post" action="{base}/casino/blackjack"><input type="hidden" '
                              f'name="action" value="{a}"><input type="hidden" name="step" value="{state["step"]}">'
                              f'<button class="{"pay" if a in ("hit", "stand") else "ghost"} reveal" type="submit">'
                              f'{names[a]}</button></form>' for a in g.bj_actions(state))
            table += f'<div class="acts">{buttons}</div>'
        else:
            table += _outcome(casino, state["payout"], g.bj_wagered(state))
    deal = ""
    if state is None or state["finished"]:
        last = bet or (state["hands"][0]["bet"] // (2 if state["hands"][0]["doubled"] else 1) if state else None)
        deal = (f'<form method="post" action="{base}/casino/blackjack"><input type="hidden" name="action" '
                f'value="deal">{_bet_field(casino, last)}{_button("🃏 Deal", animate)}</form>')
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s">{table}{deal}</div>'
            '<div class="card"><h2>Rules</h2><p class="muted">6 decks, dealer stands on all 17s and checks for '
            'blackjack. Blackjack pays 3:2, wins pay 1:1. Double on any two cards (also after a split), split '
            'once; split aces get one card each. Return to player ≈ 99.5 % with good play – like a real casino.'
            '</p></div>')
    return _page(site, base, "Blackjack", body)


PAGES = {"slots": slots_page, "coinflip": coinflip_page, "dice": dice_page, "roulette": roulette_page,
         "blackjack": blackjack_page}


# ------------------------------------------------------------ Routing

async def handle(site, casino, request, base: str, path: str) -> web.Response:
    accounts = site.accounts()
    sub = path.removeprefix("casino").strip("/")
    if accounts is None:
        return _page(site, base, "Casino", notice("Player login is switched off.", "bad"), status=404)
    sess = accounts.session(request.cookies.get(COOKIE))
    if sess is None:
        return _page(site, base, "Casino", '<div class="card"><p>Log in to play with your points.</p>'
                     f'<p><a class="btn" href="{base}/account">Log in</a></p></div>')
    uid = sess["user_id"]
    if not casino.points_enabled:
        return _page(site, base, "Casino", notice("The casino is closed right now.", "bad"))
    if sub not in PAGES:
        if sub:
            raise web.HTTPSeeOther(f"{base}/casino")
        return lobby(site, casino, base, uid)
    if sub not in casino.enabled_games():
        return _page(site, base, "Casino", notice("This game is closed.", "bad"), status=404)
    if request.method != "POST":
        return PAGES[sub](site, casino, base, uid)
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Bets can only be placed on this website.")
    form = await request.post()
    bet = _int(form.get("bet"))
    before = casino.balance(uid)
    try:
        if sub == "slots":
            spin = casino.play_slots(uid, bet or 0)
            return slots_page(site, casino, base, uid, bet=bet, spin=spin, before=before)
        if sub == "coinflip":
            pick = str(form.get("pick", "heads"))
            side, payout = casino.play_coinflip(uid, bet or 0, pick)
            return coinflip_page(site, casino, base, uid, bet=bet, pick=pick, side=side, payout=payout,
                                 before=before)
        if sub == "dice":
            target = _int(form.get("target"), 50)
            roll, payout = casino.play_dice(uid, bet or 0, target)
            return dice_page(site, casino, base, uid, bet=bet, target=target, roll=roll, payout=payout,
                             before=before)
        if sub == "roulette":
            kind, number = str(form.get("kind", "")), _int(form.get("number"))
            pocket, payout = casino.play_roulette(uid, bet or 0, kind, number)
            return roulette_page(site, casino, base, uid, bet=bet, kind=kind, number=number, pocket=pocket,
                                 payout=payout, before=before)
        action = str(form.get("action", ""))
        if action == "deal":
            casino.bj_start(uid, bet or 0)
        else:
            casino.bj_action(uid, action, _int(form.get("step"), -1))
        return blackjack_page(site, casino, base, uid, bet=bet, before=before, animate=True)
    except GameError as exc:
        kwargs = {"bet": bet, "error": str(exc)}
        if sub == "dice":
            kwargs["target"] = _int(form.get("target"), 50)
        return PAGES[sub](site, casino, base, uid, **kwargs)
