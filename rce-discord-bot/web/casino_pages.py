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


def _page(site, base: str, title: str, body: str, status: int = 200, *, scripts=("casino.js",),
          css: str = "") -> web.Response:
    tags = "".join(f'<script src="{base}/{name}?v={site.asset_version(name)}" defer></script>' for name in scripts)
    page = site.page(base, "casino", title, f'<h1>{esc(title)}</h1><div class="casino">{body}</div>{tags}')
    return web.Response(status=status, text=page.replace("</style>", CASINO_CSS + css + "</style>", 1),
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
                    f'<div class="rtp">{esc(rtp)}</div></a>'
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


POKER_CSS = """
.ptables{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.ptables .card{text-decoration:none;display:block}.ptables .seats{color:var(--muted);font-size:.9rem}
.felt{position:relative;width:100%;max-width:820px;aspect-ratio:16/9;margin:70px auto 120px;border-radius:50%/45%;
background:radial-gradient(ellipse at 50% 40%,#1f7a46,#0c4426 70%,#062a17);
box-shadow:0 0 0 10px #4a2410,0 0 0 13px #8a5a2b,0 18px 40px rgba(0,0,0,.6),inset 0 0 60px rgba(0,0,0,.55)}
.felt .center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center;width:70%}
.felt .board{display:flex;gap:6px;justify-content:center;min-height:62px}
.felt .pot{margin-top:8px;font-family:var(--head);color:var(--gold);letter-spacing:.06em;font-size:1.1rem}
.felt .banner{margin-top:6px;color:#fff;font-weight:700;text-shadow:0 1px 3px #000;min-height:1.4em}
.pcard{width:42px;height:60px;border-radius:5px;background:#f6f1ea;color:#111;display:inline-flex;align-items:center;
justify-content:center;font-weight:800;font-size:1rem;box-shadow:0 2px 5px rgba(0,0,0,.5);
animation:pdeal .35s cubic-bezier(.2,.8,.3,1) both}
.pcard.red{color:#c1121f}.pcard.back{background:repeating-linear-gradient(45deg,#7a1d0e 0 5px,#a8290f 5px 10px)}
@keyframes pdeal{from{opacity:0;transform:translateY(-14px) rotateY(80deg)}to{opacity:1;transform:none}}
.pseat{position:absolute;transform:translate(-50%,-50%);width:132px;text-align:center}
.pseat .box{background:rgba(10,7,5,.92);border:1px solid var(--line);border-radius:10px;padding:6px 6px 5px;
position:relative;overflow:hidden}
.pseat.turn .box{border-color:var(--gold);box-shadow:0 0 18px rgba(255,182,39,.55)}
.pseat.folded .box,.pseat.away .box{opacity:.5}.pseat .nm{font-weight:700;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}.pseat .st{color:var(--gold);font-family:var(--head);letter-spacing:.04em}
.pseat .act{font-size:.78rem;color:var(--muted);min-height:1.1em}.pseat .hole{display:flex;gap:4px;justify-content:center;
margin-bottom:4px;min-height:60px}.pseat .bet{position:absolute;left:50%;transform:translateX(-50%);
background:rgba(0,0,0,.6);color:var(--gold);border-radius:12px;padding:1px 9px;font-size:.85rem;white-space:nowrap}
.pseat .timer{position:absolute;left:0;bottom:0;height:3px;background:var(--gold);transform-origin:left}
.pseat .dealer{position:absolute;right:-10px;top:50%;z-index:2;width:22px;height:22px;border-radius:50%;background:#fff;color:#111;
font-weight:800;font-size:.8rem;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 3px #000}
.pseat.empty .box{border-style:dashed;cursor:pointer;color:var(--muted)}.pseat.win .box{border-color:var(--ok);
box-shadow:0 0 22px rgba(61,220,106,.6)}
.pbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:center}
.pbar button{margin:0;width:auto;min-width:96px;padding:11px 16px}.pbar input[type=range]{flex:1;min-width:160px}
.pbar .amt{width:110px;padding:9px;border:1px solid var(--line);background:rgba(0,0,0,.5);color:var(--text);font:inherit}
.plog{font-size:.85rem;color:var(--muted);max-height:160px;overflow:auto}
@media (max-width:640px){.pseat{width:92px;font-size:.8rem}.pcard{width:30px;height:44px;font-size:.78rem}
.pseat .hole{min-height:44px}.felt{width:calc(100% - 70px);aspect-ratio:3/4;border-radius:44%/36%;margin:80px auto 130px}}
"""


def _poker_page(site, base: str, title: str, body: str) -> web.Response:
    return _page(site, base, title, body, scripts=("poker.js",), css=POKER_CSS)


def poker_lobby(site, casino, base: str, uid: int) -> web.Response:
    cards = []
    for t in casino.tables.values():
        seated = [s for s in t.seats if s is not None]
        mine = t.seat_of(uid) is not None
        cards.append(f'<a class="card" href="{base}/casino/poker/{esc(t.id)}"><h2>{esc(t.name)}'
                     f'{" · you sit here" if mine else ""}</h2><div>Blinds <b>{t.sb} / {t.bb}</b> · buy-in '
                     f'{esc(casino.fmt_points(t.min_buy))} – {esc(casino.fmt_points(t.max_buy))}</div>'
                     f'<div class="seats">{len(seated)} / {poker_seats()} players'
                     f'{": " + esc(", ".join(s.name for s in seated)) if seated else ""}</div></a>')
    body = (_balance(casino, uid, base)
            + (f'<div class="ptables">{"".join(cards)}</div>' if cards else
               '<p class="muted">No poker tables are set up.</p>')
            + '<div class="card"><h2>How it works</h2><p class="muted">Texas Hold\'em, no limit, up to 6 players. '
              'Sit down with a buy-in from your points; when you leave, your chips go back to your points. You play '
              'against the other players – the casino deals and keeps the rules. If you run out of time you check or '
              'fold and sit out until you click "I\'m back".</p></div>')
    return _poker_page(site, base, "Poker", body)


def poker_seats() -> int:
    from support.poker import MAX_SEATS
    return MAX_SEATS


def poker_table_page(site, casino, base: str, uid: int, tid: str) -> web.Response:
    t = casino.table(tid)
    body = (_balance(casino, uid, base).replace(f'href="{base}/casino"', f'href="{base}/casino/poker"', 1)
            .replace("← All games", "← All tables")
            + f'<div id="poker" data-base="{esc(base)}" data-table="{esc(t.id)}" data-min="{t.min_buy}" '
              f'data-max="{t.max_buy}" data-points="{esc(casino.bot.config.points_name)}">'
              f'<div class="card"><h2>{esc(t.name)} · blinds {t.sb} / {t.bb}</h2>'
              '<noscript><p class="notice bad">Poker needs JavaScript switched on.</p></noscript>'
              '<div class="felt"><div class="center"><div class="board"></div><div class="pot"></div>'
              '<div class="banner"></div></div></div><div class="pbar" id="pbar"></div></div>'
              '<div class="card"><h2>Table log</h2><div class="plog" id="plog"></div></div></div>')
    return _poker_page(site, base, t.name, body)


async def poker_handle(site, casino, request, base: str, uid: int, name: str, parts: list[str]) -> web.Response:
    if not parts:
        return poker_lobby(site, casino, base, uid)
    tid = parts[0]
    try:
        casino.table(tid)
    except GameError:
        raise web.HTTPSeeOther(f"{base}/casino/poker") from None
    what = parts[1] if len(parts) > 1 else ""
    if request.method != "POST":
        if what == "state":
            version = _int(request.query.get("v"), -1)
            await casino.poker_wait(tid, version)
            return web.json_response(casino.poker_view(uid, tid), headers={"Cache-Control": "no-store"})
        return poker_table_page(site, casino, base, uid, tid)
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Only from this website.")
    form = await request.post()
    try:
        if what == "sit":
            casino.poker_sit(uid, name, tid, _int(form.get("buyin"), 0) or 0)
        elif what == "leave":
            casino.poker_leave(uid, tid)
        elif what == "act":
            casino.poker_act(uid, tid, str(form.get("action", "")), _int(form.get("amount"), 0) or 0)
        else:
            raise GameError("Unknown action.")
    except GameError as exc:
        return web.json_response({"error": str(exc), "state": casino.poker_view(uid, tid),
                                  "balance": casino.balance(uid)}, headers={"Cache-Control": "no-store"})
    return web.json_response({"ok": True, "state": casino.poker_view(uid, tid), "balance": casino.balance(uid)},
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------ Plinko (plinko.js drops the balls; works without it)

PLINKO_CSS = """
.plk{display:grid;grid-template-columns:260px 1fr;gap:12px;align-items:start}
@media (max-width:760px){.plk{grid-template-columns:1fr}.plk .board{order:-1;display:flex;flex-direction:column}
.plk .phist{position:static;flex-direction:row;width:auto;min-height:26px;order:-1;margin-bottom:6px}
.plk .phist div{flex:0 0 52px}}
.plk form{margin:0}.plk select{width:100%}
.plk .seg{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:4px;border-radius:8px;background:#0f212e}
.plk .seg label{margin:0;text-align:center;padding:8px 0;border-radius:6px;cursor:pointer;font-weight:700;
color:#b1bad3;text-transform:capitalize}.plk .seg input{display:none}
.plk .seg label:has(input:checked){background:#2f4553;color:#fff}
.plk .board{background:#0f212e;border-radius:12px;padding:12px 8px 14px;position:relative;overflow:hidden;
box-shadow:inset 0 0 0 1px #213743}
.plk canvas{display:block;width:100%;touch-action:manipulation}
.pslots{display:flex;gap:3px;margin-top:6px}.pslots div{flex:1;min-width:0;text-align:center;border-radius:4px;
padding:6px 0;font-weight:800;font-size:.72rem;color:#1a0900;box-shadow:0 3px 0 rgba(0,0,0,.35);overflow:hidden}
.pslots .hit{outline:2px solid #fff}
.plk.live .pslots{display:none}
.plk.dropping .seg,.plk.dropping select{opacity:.55;pointer-events:none}
.phist{position:absolute;right:8px;top:10px;display:flex;flex-direction:column;gap:4px;width:46px}
.phist div{border-radius:6px;padding:5px 0;text-align:center;font-size:.72rem;font-weight:800;color:#1a0900;
animation:reveal .3s ease-out both}
.plist{max-height:220px;overflow:auto;font-size:.9rem}.plist div{display:flex;justify-content:space-between;
padding:3px 0;border-bottom:1px solid var(--line)}
.plk .pay{margin-top:4px}.plk .muted{font-size:.8rem}
"""


def plinko_page(site, casino, base, uid, *, bet=None, balls=1, risk="medium", rows=16, drops=None, error="",
                before=None) -> web.Response:
    import json
    risk = risk if risk in g.PLINKO_RISKS else "medium"
    rows = rows if rows in g.PLINKO_ROWS else 16
    table = g.PLINKO_TABLES[(risk, rows)]
    most = casino.setting("plinko_max_balls")
    hits = {d["slot"] for d in drops or ()}
    slots = "".join(f'<div style="background:{g.plinko_colour(k, rows)}"{" class=\"hit\"" if k in hits else ""}>'
                    f'{g.fmt_multiplier(c)}×</div>' for k, c in enumerate(table))
    risks = "".join(f'<label><input type="radio" name="risk" value="{r}"{" checked" if r == risk else ""}>{r}</label>'
                    for r in g.PLINKO_RISKS)
    row_opts = "".join(f'<option value="{n}"{" selected" if n == rows else ""}>{n}</option>' for n in g.PLINKO_ROWS)
    result = ""
    if drops:
        total = sum(d["payout"] for d in drops)
        lines = "".join(f'<div><span>Ball {i}: {g.fmt_multiplier(d["cents"])}×</span>'
                        f'<b>{esc(casino.fmt_points(d["payout"]))}</b></div>' for i, d in enumerate(drops, 1))
        result = _outcome(casino, total, (bet or 0) * len(drops)) + f'<div class="plist">{lines}</div>'
    tables = {f"{r}-{n}": t for (r, n), t in g.PLINKO_TABLES.items()}
    colours = {n: [g.plinko_colour(k, n) for k in range(n + 1)] for n in g.PLINKO_ROWS}
    body = (_balance(casino, uid, base, before=before)
            + notice(error, "bad")
            + f'<div class="plk" id="plinko" data-drop="{base}/casino/plinko/drop" data-balance="{casino.balance(uid)}" '
              f'data-unit=" {esc(casino.bot.config.points_name)}" '
              f'data-tables="{esc(json.dumps(tables, separators=(",", ":")))}" '
              f'data-colours="{esc(json.dumps(colours, separators=(",", ":")))}">'
              f'<div class="card"><form method="post" action="{base}/casino/plinko">{_bet_field(casino, bet)}'
              f'<label class="field"><span>Balls per drop (1 – {most})</span><input type="number" name="balls" '
              f'min="1" max="{most}" step="1" value="{balls}" inputmode="numeric" required></label>'
              f'<label class="field"><span>Risk</span></label><div class="seg">{risks}</div>'
              f'<label class="field"><span>Rows</span><select name="rows">{row_opts}</select></label>'
              f'<button class="pay" type="submit">🔻 Drop</button></form>'
              f'<p class="muted">Each ball is one bet and falls left or right at every peg – where it lands, it pays '
              f'that multiplier. Drop again while balls are still falling. Return to player 97.5 %.</p>'
              f'<div class="presult">{result}</div></div>'
              f'<div class="board"><canvas width="600" height="520" aria-label="Plinko board"></canvas>'
              f'<div class="pslots">{slots}</div><div class="phist"></div></div></div>')
    return _page(site, base, "Plinko", body, scripts=("plinko.js",), css=PLINKO_CSS)


def _plinko_form(form) -> tuple[int, int, str, int]:
    return (_int(form.get("bet"), 0) or 0, _int(form.get("balls"), 1) or 1, str(form.get("risk", "medium")),
            _int(form.get("rows"), 16) or 16)


async def plinko_handle(site, casino, request, base: str, uid: int, parts: list[str]) -> web.Response:
    if request.method != "POST":
        if parts:
            raise web.HTTPSeeOther(f"{base}/casino/plinko")
        return plinko_page(site, casino, base, uid)
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Bets can only be placed on this website.")
    bet, balls, risk, rows = _plinko_form(await request.post())
    before = casino.balance(uid)
    if parts == ["drop"]:                         # plinko.js: the server picks every path, the page animates it
        try:
            drops = casino.play_plinko(uid, bet, risk, rows, balls)
        except GameError as exc:
            return web.json_response({"error": str(exc), "balance": casino.balance(uid)},
                                     headers={"Cache-Control": "no-store"})
        return web.json_response({"ok": True, "bet": bet, "balance": casino.balance(uid), "before": before,
                                  "balls": [{"path": d["path"], "slot": d["slot"], "payout": d["payout"],
                                             "multiplier": g.fmt_multiplier(d["cents"])} for d in drops]},
                                 headers={"Cache-Control": "no-store"})
    try:
        drops = casino.play_plinko(uid, bet, risk, rows, balls)
    except GameError as exc:
        return plinko_page(site, casino, base, uid, bet=bet, balls=balls, risk=risk, rows=rows, error=str(exc))
    return plinko_page(site, casino, base, uid, bet=bet, balls=balls, risk=risk, rows=rows, drops=drops,
                       before=before)


# ------------------------------------------------------------ Horse racing

RACE_SECONDS = 7.0           # the winner crosses the line this long after the gates open
COUNTDOWN = 2.4              # "3 – 2 – 1 – GO!" before the gates open
STRIDE = 0.36                # one gallop stride (legs, body bob, dust)
COATS = g.HORSE_COATS
ORDINAL = ("1st", "2nd", "3rd", "4th", "5th", "6th")

RACE_CSS = """
.rc-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 8px;flex-wrap:wrap}
.rc-title{font-family:var(--head);letter-spacing:.12em;text-transform:uppercase;color:var(--gold);font-size:1.05rem}
.ticker{position:relative;height:24px;min-width:220px;flex:1;text-align:right;overflow:hidden;font-family:var(--head);
letter-spacing:.06em;text-transform:uppercase;font-size:.95rem;color:#fff}
.ticker span{position:absolute;right:0;top:0;opacity:0;white-space:nowrap;
animation:tick var(--b,1s) linear var(--a,0s) both}
.ticker span.stay{animation:reveal .45s cubic-bezier(.2,.9,.3,1.3) var(--a,0s) both;color:var(--gold)}
@keyframes tick{0%{opacity:0;transform:translateY(6px)}12%,85%{opacity:1;transform:none}100%{opacity:0}}
.track{position:relative;margin:0 0 14px;border-radius:14px;overflow:hidden;padding:14px 0 10px;
background:repeating-linear-gradient(90deg,#2f6b34 0 46px,#2a6130 46px 92px);
box-shadow:inset 0 0 0 3px rgba(255,255,255,.08),0 10px 30px rgba(0,0,0,.45)}
.track::before,.track::after{content:"";position:absolute;left:0;right:0;height:8px;z-index:4;
background:repeating-linear-gradient(90deg,#f4f4f4 0 26px,#cfcfcf 26px 30px);box-shadow:0 2px 4px rgba(0,0,0,.5)}
.track::before{top:0}.track::after{bottom:0}
.marks{position:absolute;inset:0 58px 0 74px;pointer-events:none;z-index:1}
.marks i{position:absolute;top:10px;bottom:10px;border-left:2px dashed rgba(255,255,255,.18)}
.marks i b{position:absolute;top:-2px;left:4px;font:700 .62rem var(--head);color:rgba(255,255,255,.45)}
.finish{position:absolute;top:8px;bottom:8px;right:44px;width:14px;z-index:2;
background:repeating-conic-gradient(#fff 0 25%,#111 0 50%) 0 0/14px 14px;box-shadow:0 0 12px rgba(255,255,255,.35)}
.finish b{position:absolute;top:-1px;right:18px;font:800 .62rem var(--head);letter-spacing:.15em;color:#fff;
writing-mode:vertical-rl;text-shadow:0 1px 2px #000}
.gate{position:absolute;top:8px;bottom:8px;left:62px;width:10px;z-index:5;border-radius:3px;
background:repeating-linear-gradient(#e5341c 0 10px,#fff 10px 20px);box-shadow:0 0 8px rgba(0,0,0,.6);
transform-origin:top;animation:gate .5s ease-in var(--g,0s) both}
@keyframes gate{to{transform:translateY(-110%);opacity:0}}
.lane{position:relative;display:flex;align-items:center;height:52px;padding:0 10px;z-index:3}
.lane+.lane{border-top:1px solid rgba(255,255,255,.14)}
.lane.mine{background:linear-gradient(90deg,rgba(247,193,78,.28),rgba(247,193,78,.06) 60%,transparent)}
.lane.mine::after{content:"YOU";position:absolute;left:10px;width:30px;bottom:1px;text-align:center;
font:800 .55rem var(--head);letter-spacing:.1em;color:var(--gold)}
.lane .no{flex:none;width:30px;height:30px;border-radius:6px;display:flex;align-items:center;justify-content:center;
font:800 .95rem var(--head);color:#fff;text-shadow:0 1px 2px #000;box-shadow:inset 0 -3px 0 rgba(0,0,0,.25),
0 2px 4px rgba(0,0,0,.4);margin-right:26px}
.run{position:relative;flex:1;height:100%;margin-right:44px}
.runner{position:absolute;left:0;bottom:2px;width:calc(100% - 74px);height:48px;will-change:transform}
.horse{width:74px;height:48px;overflow:visible;filter:drop-shadow(0 3px 2px rgba(0,0,0,.45))}
.horse .leg{transform-box:fill-box;transform-origin:50% 0}
.horse.go .body{animation:bob var(--s) ease-in-out var(--d) var(--n)}
.horse.go .lf{animation:gal var(--s) linear var(--d) var(--n)}
.horse.go .lb{animation:gal var(--s) linear calc(var(--d) - var(--s) * .5) var(--n)}
.horse.go .hf{animation:gal var(--s) linear calc(var(--d) - var(--s) * .25) var(--n)}
.horse.go .hb{animation:gal var(--s) linear calc(var(--d) - var(--s) * .75) var(--n)}
@keyframes gal{0%{transform:rotate(-34deg)}50%{transform:rotate(30deg)}100%{transform:rotate(-34deg)}}
@keyframes bob{50%{transform:translateY(-3px)}}
.dust{position:absolute;left:-6px;bottom:2px;width:22px;height:12px;border-radius:50%;opacity:0;
background:radial-gradient(circle,rgba(214,190,150,.85),rgba(214,190,150,0) 70%);
animation:puff var(--s) ease-out var(--d) var(--n)}
@keyframes puff{0%{opacity:.9;transform:scale(.4) translateX(0)}100%{opacity:0;transform:scale(1.6) translateX(-18px)}}
.lane .place{position:absolute;right:136px;top:50%;margin-top:-13px;font:800 .95rem var(--head);padding:3px 9px;
border-radius:999px;background:rgba(0,0,0,.55);color:#eee;letter-spacing:.06em}
.lane .place.p1{background:linear-gradient(#ffe48a,#d39b17);color:#3a2500}
.lane .place.p2{background:linear-gradient(#f2f2f2,#a9a9a9);color:#222}
.lane .place.p3{background:linear-gradient(#f0b07a,#a0581e);color:#2b1300}
.count{position:absolute;inset:0;z-index:6;display:flex;align-items:center;justify-content:center;pointer-events:none}
.count span{position:absolute;font:900 4.2rem var(--head);color:#fff;text-shadow:0 0 22px rgba(229,52,28,.9),0 4px 0 #000;
opacity:0;animation:cd .6s cubic-bezier(.2,.9,.3,1.2) var(--a) both}
.count span.go{color:var(--gold)}
@keyframes cd{0%{opacity:0;transform:scale(2.2)}25%{opacity:1;transform:scale(1)}80%{opacity:1}100%{opacity:0;transform:scale(.8)}}
.winner{display:flex;align-items:center;justify-content:center;gap:14px;margin:4px 0 6px}
.winner .silk{width:56px;height:56px}.winner .w-name{font:800 1.6rem var(--head);letter-spacing:.06em;
text-transform:uppercase;color:var(--gold);text-shadow:0 0 18px rgba(247,193,78,.55)}
.podium{display:grid;grid-template-columns:1fr 1fr 1fr;align-items:end;gap:10px;max-width:440px;margin:6px auto 4px}
.podium>div{text-align:center}.podium .step{border-radius:8px 8px 0 0;display:flex;align-items:flex-start;
justify-content:center;padding-top:6px;font:900 1.4rem var(--head);color:#2a1a00;box-shadow:inset 0 -6px 0 rgba(0,0,0,.18)}
.podium .s1 .step{height:76px;background:linear-gradient(#ffe48a,#d39b17)}
.podium .s2 .step{height:54px;background:linear-gradient(#f2f2f2,#9d9d9d)}
.podium .s3 .step{height:40px;background:linear-gradient(#f0b07a,#a0581e)}
.podium .silk{width:40px;height:40px;display:block;margin:0 auto 2px}
.podium .pn{font-size:.85rem;margin-bottom:4px}.podium .mine .pn{color:var(--gold);font-weight:800}
.rest{text-align:center;color:var(--muted);font-size:.85rem;margin:4px 0 8px}
.horses{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px;margin:6px 0 12px}
.horses label{position:relative;display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:4px 10px;
padding:9px 11px;border:1px solid var(--line);border-radius:10px;cursor:pointer;background:rgba(255,255,255,.02);
transition:transform .15s,border-color .15s,background .15s}
.horses label:hover{transform:translateY(-2px);border-color:rgba(247,147,30,.6)}
.horses input{position:absolute;opacity:0;pointer-events:none}
.horses label:has(input:checked){border-color:var(--orange);background:rgba(247,147,30,.12);
box-shadow:0 0 0 1px var(--orange),0 6px 18px rgba(229,52,28,.25)}
.horses .silk{width:38px;height:38px;grid-row:span 2}
.horses .hn{font-weight:800}.horses .pay{font:800 1.15rem var(--head);color:var(--gold);grid-row:span 2}
.horses .meta{display:block}
.horses .bar{display:block;height:6px;border-radius:3px;background:rgba(255,255,255,.1);overflow:hidden;margin:3px 0 2px}
.horses .bar i{display:block;height:6px;min-width:6px;background:linear-gradient(90deg,var(--orange),var(--gold))}
.horses .ch{display:block;font-size:.75rem;color:var(--muted)}
@media (max-width:560px){.lane .no{margin-right:8px}.marks{left:46px}.gate{left:44px}.lane .place{right:104px}
.horse{width:56px;height:36px}.runner{width:calc(100% - 56px);height:36px;bottom:8px}
.ticker{flex-basis:100%;min-width:0;text-align:left}.ticker span{right:auto;left:0}}
"""


def _horse_svg(silk: str, coat: str, number: int, *, running: bool = False, vars_: str = "") -> str:
    """A galloping horse with its jockey in racing silks (facing right). Legs, body bob and dust are CSS."""
    dark = "#1c120c"
    cls = "horse go" if running else "horse"
    style = f' style="{vars_}"' if vars_ else ""
    leg = (lambda c, x, y: f'<path class="leg {c}" d="M{x} {y} l1 9 -1 8" stroke="{coat}" stroke-width="3.4" '
                           f'stroke-linecap="round" fill="none"/><circle class="leg {c}" cx="{x}" cy="{y + 17}" r="0"/>')
    return (f'<svg class="{cls}" viewBox="0 0 80 52"{style} aria-hidden="true">'
            f'<g class="legs">{leg("hb", 22, 28)}{leg("lb", 50, 28)}</g>'
            f'<g class="body">'
            f'<path d="M16 22 C6 22 4 32 2 40 C8 34 10 30 16 28Z" fill="{dark}"/>'                       # tail
            f'<ellipse cx="34" cy="26" rx="20" ry="9.5" fill="{coat}"/>'                                    # barrel
            f'<path d="M46 22 L58 8 L64 9 L56 28Z" fill="{coat}"/>'                                         # neck
            f'<path d="M57 6 L72 12 L73 17 L67 18 L58 13Z" fill="{coat}"/>'                                 # head
            f'<path d="M58 7 L60 2 L62 7Z" fill="{coat}"/>'                                                # ear
            f'<path d="M47 21 L58 7 L56 6 L45 19Z" fill="{dark}"/>'                                        # mane
            f'<circle cx="66" cy="11.5" r="1.1" fill="#000"/>'
            f'<rect x="27" y="17" width="15" height="11" rx="2" fill="{silk}"/>'                           # saddle cloth
            f'<text x="34.5" y="25.5" font-size="8" font-weight="800" text-anchor="middle" fill="#fff" '
            f'style="paint-order:stroke" stroke="#000" stroke-width="1.2">{number}</text>'
            f'<path d="M30 17 L38 4 L46 7 L41 18Z" fill="{silk}"/>'                                         # jockey body
            f'<path d="M41 9 L52 12" stroke="{silk}" stroke-width="3" stroke-linecap="round"/>'              # arm
            f'<circle cx="44" cy="3.5" r="4.2" fill="#f1c9a5"/>'                                           # face
            f'<path d="M39.6 3 A4.6 4.6 0 0 1 48.6 2.4 L49.5 3.6 Z" fill="{silk}"/>'                      # cap
            f'</g><g class="legs">{leg("hf", 26, 30)}{leg("lf", 46, 30)}</g></svg>')


def _silk_svg(silk: str, number: int) -> str:
    """The jockey's silks – a little badge with the cap and the number."""
    return (f'<svg class="silk" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="19" fill="{silk}" '
            f'stroke="rgba(0,0,0,.35)" stroke-width="2"/><path d="M8 17 A12 12 0 0 1 32 17 L34 20 L6 20Z" '
            f'fill="rgba(255,255,255,.28)"/><text x="20" y="31" font-size="13" font-weight="900" text-anchor="middle" '
            f'fill="#fff" style="paint-order:stroke" stroke="#000" stroke-width="2">{number}</text></svg>')


def race_page(site, casino, base, uid, *, bet=None, horse=0, race=None, error="", before=None) -> web.Response:
    horse = horse if isinstance(horse, int) and 0 <= horse < len(g.HORSES) else 0
    keyframes, lanes = "", []
    t = 0.0
    if race:
        paths = g.race_paths(race.order, FX)
        total = RACE_SECONDS * max(p["finish"] for p in paths.values())
        t = COUNTDOWN + total + 0.3
        places = {h: n for n, h in enumerate(race.order)}
    for i, (name, silk, _) in enumerate(g.HORSES):
        coat = COATS[i % len(COATS)]
        runner = f'<div class="runner">{_horse_svg(silk, coat, i + 1)}</div>'
        place = ""
        if race:
            path = paths[i]
            steps = "".join(f"{100 * tt * RACE_SECONDS / total:.2f}%{{transform:translateX({100 * pp:.2f}%)}}"
                            for tt, pp in path["points"])
            keyframes += f"@keyframes hr{i}{{{steps}100%{{transform:translateX(100%)}}}}"
            finish = path["finish"] * RACE_SECONDS
            strides = max(1, int(finish / STRIDE))
            css_vars = f"--s:{STRIDE}s;--d:{COUNTDOWN}s;--n:{strides}"
            runner = (f'<div class="runner" style="animation:hr{i} {total:.2f}s linear {COUNTDOWN}s both;{css_vars}">'
                      f'<span class="dust"></span>{_horse_svg(silk, coat, i + 1, running=True, vars_=css_vars)}</div>')
            rank = places[i]
            place = (f'<span class="place reveal p{rank + 1}" style="--t:{COUNTDOWN + finish:.2f}s">'
                     f'{ORDINAL[rank]}</span>')
        lanes.append(f'<div class="lane{" mine" if race and i == horse else ""}">'
                     f'<span class="no" style="background:{silk}">{i + 1}</span>'
                     f'<div class="run">{runner}</div>{place}</div>')
    marks = "".join(f'<i style="left:{p}%"><b>{label}</b></i>' for p, label in ((25, "¼"), (50, "½"), (75, "¾")))
    count, ticker = "", ""
    if race:
        count = ('<div class="count">'
                 + "".join(f'<span style="--a:{k * 0.6:.1f}s">{n}</span>' for k, n in enumerate(("3", "2", "1")))
                 + f'<span class="go" style="--a:{COUNTDOWN - 0.6:.1f}s">GO!</span></div>')
        win = g.HORSES[race.order[0]][0]
        lines = ((COUNTDOWN, 1.8, "And they're off!"), (COUNTDOWN + total * 0.35, 1.8, "Neck and neck …"),
                 (COUNTDOWN + total * 0.62, 1.6, "Into the final stretch!"),
                 (COUNTDOWN + RACE_SECONDS * 0.86, 1.0, "Photo finish …"))
        ticker = "".join(f'<span style="--a:{a:.2f}s;--b:{b:.2f}s">{esc(text)}</span>' for a, b, text in lines)
        ticker += f'<span class="stay" style="--a:{COUNTDOWN + RACE_SECONDS:.2f}s">🏆 {esc(win)} wins!</span>'
    gate = f'<div class="gate" style="--g:{COUNTDOWN if race else 999}s"></div>'
    track = (f'<div class="rc-head"><span class="rc-title">🏇 Rust Downs · 6 runners</span>'
             f'<span class="ticker">{ticker or "Pick your horse below"}</span></div>'
             f'<div class="track"><div class="marks">{marks}</div><div class="finish"><b>FINISH</b></div>{gate}'
             f'{count}{"".join(lanes)}</div>')
    shown = ""
    if race:
        w = race.order[0]
        podium = "".join(
            f'<div class="s{rank + 1}{" mine" if h == horse else ""}">{_silk_svg(g.HORSES[h][1], h + 1)}'
            f'<div class="pn">{esc(g.HORSES[h][0])}</div><div class="step">{rank + 1}</div></div>'
            for rank, h in ((1, race.order[1]), (0, race.order[0]), (2, race.order[2])))
        rest = " · ".join(f'{ORDINAL[n]} {esc(g.HORSES[h][0])}{" (you)" if h == horse else ""}'
                          for n, h in enumerate(race.order) if n >= 3)
        shown = (f'<div class="winner reveal">{_silk_svg(g.HORSES[w][1], w + 1)}'
                 f'<span class="w-name">{esc(g.HORSES[w][0])} wins!</span></div>'
                 + _outcome(casino, race.payout, bet)
                 + f'<div class="podium reveal">{podium}</div><div class="rest reveal">{rest}</div>')
    picks = "".join(
        f'<label><input type="radio" name="horse" value="{i}"{" checked" if i == horse else ""}>'
        f'{_silk_svg(silk, i + 1)}<span class="hn">{esc(name)}</span>'
        f'<span class="pay">{g.fmt_multiplier(round(float(g.horse_pays(i)) * 100))}×</span>'
        f'<span class="meta"><span class="bar"><i style="width:{chance * 100 // 30}%"></i></span>'
        f'<span class="ch">{chance} % win chance</span></span></label>'
        for i, (name, silk, chance) in enumerate(g.HORSES))
    body = (_balance(casino, uid, base, before=before, t=t) + notice(error, "bad")
            + f'<div class="card" style="--t:{t:.2f}s">{f"<style>{keyframes}</style>" if keyframes else ""}'
              f'{track}{shown}'
              f'<form method="post" action="{base}/casino/race"><div class="horses">{picks}</div>'
              f'{_bet_field(casino, bet)}{_button("🏇 Start the race", bool(race))}</form>'
              '<p class="muted">Pick a horse: the favourite wins most often but pays less, a longshot pays more. Every '
              'horse pays 95 ÷ its win chance × your bet – return to player 95 %.</p></div>')
    # the race's own keyframes sit in the card: casino.js swaps in only the card when playing without a reload
    return _page(site, base, "Horse racing", body, css=RACE_CSS)


PAGES = {"slots": slots_page, "coinflip": coinflip_page, "dice": dice_page, "roulette": roulette_page,
         "blackjack": blackjack_page, "race": race_page}


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
    parts = [p for p in sub.split("/") if p]
    if parts[:1] == ["poker"]:
        if "poker" not in casino.enabled_games():
            return _page(site, base, "Casino", notice("This game is closed.", "bad"), status=404)
        return await poker_handle(site, casino, request, base, uid, sess["name"], parts[1:])
    if parts[:1] == ["plinko"]:
        if "plinko" not in casino.enabled_games():
            return _page(site, base, "Casino", notice("This game is closed.", "bad"), status=404)
        return await plinko_handle(site, casino, request, base, uid, parts[1:])
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
        if sub == "race":
            horse = _int(form.get("horse"), -1)
            race = casino.play_race(uid, bet or 0, horse)
            return race_page(site, casino, base, uid, bet=bet, horse=horse, race=race, before=before)
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
        if sub == "race":
            kwargs["horse"] = _int(form.get("horse"), 0)
        return PAGES[sub](site, casino, base, uid, **kwargs)
