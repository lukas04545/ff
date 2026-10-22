"""Casino pages of the player website (/casino …): play the plugins/casino.py games with your points.

Server-rendered forms, no scripts (the website allows none). Players log in with the website's player login; the
login cookie is SameSite=Lax, so other websites can't place bets for them; a POST naming another origin is refused.
"""
from __future__ import annotations

from urllib.parse import urlparse

from aiohttp import web

from support import casino_games as g
from support.casino_games import GameError
from web.account_pages import COOKIE
from web.store_pages import STORE_CSS, esc, notice

CASINO_CSS = STORE_CSS + """
.games{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.game{display:block;text-decoration:none;text-align:center;padding:20px 12px}
.game .ico{font-size:2.6rem;line-height:1.2}
.game h2{margin:6px 0 2px}.rtp{font-size:.85rem;color:var(--muted)}
.bal{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.reels{display:flex;justify-content:center;gap:10px;margin:6px 0 14px}
.reel{width:84px;height:84px;display:flex;align-items:center;justify-content:center;font-size:2.8rem;overflow:hidden;
background:radial-gradient(circle at 50% 40%,rgba(255,255,255,.08),rgba(0,0,0,.45));border:1px solid var(--line)}
.reel span{display:block;animation:drop .45s cubic-bezier(.2,.9,.3,1.2) both}
.reel:nth-child(2) span{animation-delay:.12s}.reel:nth-child(3) span{animation-delay:.24s}
@keyframes drop{from{transform:translateY(-110%);opacity:0}to{transform:none;opacity:1}}
@media (prefers-reduced-motion:reduce){.reel span{animation:none}}
.result{text-align:center;font-family:var(--head);font-size:1.6rem;letter-spacing:.05em;text-transform:uppercase;
margin:4px 0}
.result.win{color:var(--ok)}.result.lose{color:var(--muted)}
.big-num{font-family:var(--head);font-size:3rem;text-align:center;line-height:1.1}
.pocket{display:inline-flex;width:84px;height:84px;border-radius:50%;align-items:center;justify-content:center;
font-family:var(--head);font-size:2.2rem;color:#fff;border:3px solid var(--gold)}
.pocket.red{background:#c1121f}.pocket.black{background:#2a2a2a;box-shadow:inset 0 0 0 3px #000}
.pocket.green{background:#127a3a}.pocket-label{text-align:center;color:var(--muted);margin-top:4px}
.cards{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}.cardx{width:56px;height:80px;background:#f6f1ea;color:#111;
border-radius:6px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:1.25rem;
box-shadow:0 2px 6px rgba(0,0,0,.5)}.cardx.red{color:#c1121f}.cardx.back{background:repeating-linear-gradient(45deg,
#7a1d0e 0 6px,#a8290f 6px 12px);color:transparent}
.hand{padding:10px;border:1px solid var(--line);margin:8px 0}.hand.active{border-color:var(--orange)}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch}.acts form{margin:0}
.acts button{min-width:110px;width:auto;margin:0;height:100%;padding:11px 18px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media (max-width:520px){.row2{grid-template-columns:1fr}}
table.pay td{padding:4px 6px}
"""
RESULT_WORDS = {"win": "Win", "blackjack": "Blackjack!", "push": "Push – bet back", "lose": "Lose", "bust": "Bust"}


def _page(site, base: str, title: str, body: str, status: int = 200) -> web.Response:
    page = site.page(base, "casino", title, f"<h1>{esc(title)}</h1>{body}")
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


def _balance(casino, uid: int, base: str, back: bool = True) -> str:
    link = f'<a href="{base}/casino">← All games</a>' if back else ""
    points = esc(casino.fmt_points(casino.balance(uid)))
    return f'<div class="card bal"><div>Your balance: <span class="price">{points}</span></div>{link}</div>'


def _outcome(casino, payout: int, bet: int) -> str:
    if payout > bet:
        return f'<div class="result win">You win {esc(casino.fmt_points(payout))}!</div>'
    if payout == bet:
        return '<div class="result">Bet back</div>'
    if payout:
        return f'<div class="result lose">{esc(casino.fmt_points(payout))} back</div>'
    return '<div class="result lose">No luck this time</div>'


def _int(value, default: int | None = None) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.lstrip("-").isdigit() and len(text) < 12 else default


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


def slots_page(site, casino, base, uid, *, bet=None, spin=None, error="") -> web.Response:
    reels = spin.reels if spin else ["🍒", "7️⃣", "💎"]
    show = "".join(f'<div class="reel"><span>{r}</span></div>' for r in reels)
    result = _outcome(casino, spin.payout, bet) if spin else ""
    pays = "".join(f"<tr><td>{sym}</td><td class='r'>{pay}×</td></tr>" for sym, pay, _ in g.SLOT_WINS)
    body = (_balance(casino, uid, base) + notice(error, "bad")
            + f'<div class="card"><div class="reels">{show}</div>{result}'
              f'<form method="post" action="{base}/casino/slots">{_bet_field(casino, bet)}'
              f'<button class="pay" type="submit">🎰 Spin</button></form></div>'
            + f'<div class="card"><h2>Pays</h2><table class="pay">{pays}</table>'
              f'<p class="muted">Return to player 95 %.</p></div>')
    return _page(site, base, "Slots", body)


def coinflip_page(site, casino, base, uid, *, bet=None, pick="heads", side=None, payout=0, error=""):
    shown = ""
    if side:
        shown = (f'<div class="big-num">{"👑" if side == "heads" else "🦅"}</div><div class="result">{side}</div>'
                 + _outcome(casino, payout, bet))
    picks = "".join(f'<label><input type="radio" name="pick" value="{p}"{" checked" if p == pick else ""}> '
                    f'{p.title()}</label> ' for p in ("heads", "tails"))
    body = (_balance(casino, uid, base) + notice(error, "bad")
            + f'<div class="card">{shown}<form method="post" action="{base}/casino/coinflip">'
              f'<p>{picks}</p>{_bet_field(casino, bet)}<button class="pay" type="submit">🪙 Flip</button></form>'
              '<p class="muted">Right side pays 1.9× your bet. Return to player 95 %.</p></div>')
    return _page(site, base, "Coin flip", body)


def dice_page(site, casino, base, uid, *, bet=None, target=50, roll=None, payout=0, error=""):
    shown = ""
    if roll is not None:
        shown = (f'<div class="big-num">{roll}</div><div class="result">rolled – needed {target} or less</div>'
                 + _outcome(casino, payout, bet))
    body = (_balance(casino, uid, base) + notice(error, "bad")
            + f'<div class="card">{shown}<form method="post" action="{base}/casino/dice"><div class="row2">'
              f'<label class="field"><span>Win chance in % ({g.DICE_MIN}–{g.DICE_MAX}) – roll 1–100 at or '
              f'under it</span><input type="number" name="target" min="{g.DICE_MIN}" max="{g.DICE_MAX}" value="{target}" '
              f'required></label>{_bet_field(casino, bet)}</div><button class="pay" type="submit">🎲 Roll</button>'
              '</form><p class="muted">Pays 95 ÷ win chance × your bet (50 % → 1.9×, 10 % → 9.5×). '
              'Return to player 95 %.</p></div>')
    return _page(site, base, "Dice", body)


def roulette_page(site, casino, base, uid, *, bet=None, kind="red", number=None, pocket=None, payout=0, error=""):
    shown = ""
    if pocket is not None:
        shown = (f'<div style="text-align:center"><span class="pocket {g.roulette_colour(pocket)}">{pocket}</span>'
                 f'</div><div class="pocket-label">{pocket} · {g.roulette_colour(pocket)}</div>'
                 + _outcome(casino, payout, bet))
    options = "".join(f'<option value="{k}"{" selected" if k == kind else ""}>{esc(label)} – pays {pay - 1}:1'
                      f'</option>' for k, (label, pay) in g.ROULETTE_BETS.items())
    body = (_balance(casino, uid, base) + notice(error, "bad")
            + f'<div class="card">{shown}<form method="post" action="{base}/casino/roulette"><div class="row2">'
              f'<label class="field"><span>Bet on</span><select name="kind">{options}</select></label>'
              f'<label class="field"><span>Number (only for "Single number")</span><input type="number" '
              f'name="number" min="0" max="36" value="{"" if number is None else number}"></label></div>'
              f'{_bet_field(casino, bet)}<button class="pay" type="submit">🎡 Spin</button></form>'
              '<p class="muted">European wheel with one zero (0–36). On 0 every bet except "0" loses. '
              'Return to player 97.3 % – like a real casino.</p></div>')
    return _page(site, base, "Roulette", body)


def _cards(cards: list[int], hide_second: bool = False) -> str:
    out = []
    for i, c in enumerate(cards):
        if hide_second and i == 1:
            out.append('<div class="cardx back">?</div>')
            continue
        label = g.card_label(c)
        out.append(f'<div class="cardx{" red" if label[-1] in "♥♦" else ""}">{esc(label)}</div>')
    return f'<div class="cards">{"".join(out)}</div>'


def blackjack_page(site, casino, base, uid, *, bet=None, error="") -> web.Response:
    state = casino.bj_state(uid)
    table = ""
    if state is not None:
        playing = not state["finished"]
        dealer_total = "?" if playing else g.hand_total(state["dealer"])[0]
        table = f'<h2>Dealer – {dealer_total}</h2>{_cards(state["dealer"], hide_second=playing)}'
        for i, hand in enumerate(state["hands"]):
            total, soft = g.hand_total(hand["cards"])
            active = playing and i == state["active"]
            label = f"Hand {i + 1}" if len(state["hands"]) > 1 else "You"
            result = (f' – <b>{RESULT_WORDS[hand["result"]]}</b> ({esc(casino.fmt_points(hand["payout"]))})'
                      if "result" in hand else "")
            table += (f'<div class="hand{" active" if active else ""}"><b>{label}</b> – {total}'
                      f'{" soft" if soft and total < 21 else ""} · bet {esc(casino.fmt_points(hand["bet"]))}'
                      f'{" (doubled)" if hand["doubled"] else ""}{result}{_cards(hand["cards"])}</div>')
        if playing:
            names = {"hit": "Hit", "stand": "Stand", "double": "Double", "split": "Split"}
            buttons = "".join(f'<form method="post" action="{base}/casino/blackjack"><input type="hidden" '
                              f'name="action" value="{a}"><input type="hidden" name="step" value="{state["step"]}">'
                              f'<button class="{"pay" if a in ("hit", "stand") else "ghost"}" type="submit">'
                              f'{names[a]}</button></form>' for a in g.bj_actions(state))
            table += f'<div class="acts">{buttons}</div>'
        else:
            table += _outcome(casino, state["payout"], g.bj_wagered(state))
    deal = ""
    if state is None or state["finished"]:
        last = bet or (state["hands"][0]["bet"] // (2 if state["hands"][0]["doubled"] else 1) if state else None)
        deal = (f'<form method="post" action="{base}/casino/blackjack"><input type="hidden" name="action" '
                f'value="deal">{_bet_field(casino, last)}<button class="pay" type="submit">🃏 Deal</button></form>')
    body = (_balance(casino, uid, base) + notice(error, "bad") + f'<div class="card">{table}{deal}</div>'
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
    try:
        if sub == "slots":
            spin = casino.play_slots(uid, bet or 0)
            return slots_page(site, casino, base, uid, bet=bet, spin=spin)
        if sub == "coinflip":
            pick = str(form.get("pick", "heads"))
            side, payout = casino.play_coinflip(uid, bet or 0, pick)
            return coinflip_page(site, casino, base, uid, bet=bet, pick=pick, side=side, payout=payout)
        if sub == "dice":
            target = _int(form.get("target"), 50)
            roll, payout = casino.play_dice(uid, bet or 0, target)
            return dice_page(site, casino, base, uid, bet=bet, target=target, roll=roll, payout=payout)
        if sub == "roulette":
            kind, number = str(form.get("kind", "")), _int(form.get("number"))
            pocket, payout = casino.play_roulette(uid, bet or 0, kind, number)
            return roulette_page(site, casino, base, uid, bet=bet, kind=kind, number=number, pocket=pocket,
                                 payout=payout)
        action = str(form.get("action", ""))
        if action == "deal":
            casino.bj_start(uid, bet or 0)
        else:
            casino.bj_action(uid, action, _int(form.get("step"), -1))
        return blackjack_page(site, casino, base, uid, bet=bet)
    except GameError as exc:
        kwargs = {"bet": bet, "error": str(exc)}
        if sub == "dice":
            kwargs["target"] = _int(form.get("target"), 50)
        return PAGES[sub](site, casino, base, uid, **kwargs)
