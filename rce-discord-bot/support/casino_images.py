"""Pictures of the casino games for Discord (plugins/casino.py: /slots, /coinflip, /dice, /roulette, /race, /blackjack).

Same idea as the Plinko board (support/plinko_image.py): the server has already decided the result, the picture only
shows it – an animated GIF (plays once, stays on the result) for the quick games and a still picture of the table for
blackjack. Drawn at twice the size and averaged down (smooth edges). Pillow's built-in font has no emoji or card
suits, so the slot symbols and suits are drawn as shapes. Pillow only, no files.
"""
from __future__ import annotations

import io
import math
import random

from PIL import Image, ImageDraw

from support import casino_games as g
from support.plinko_image import _font, _mix

SS = 2
FRAME_MS = 33
HOLD_MS = 8000
FX = random.Random()             # only for the look (filler symbols, spin speed) – never for results

BG_TOP, BG_BOTTOM = (19, 41, 56), (11, 26, 36)
GOLD, GOLD_DARK, GOLD_LIGHT = (240, 180, 40), (170, 110, 10), (255, 232, 150)
WIN, LOSE, WHITE, INK = (61, 220, 106), (255, 70, 90), (255, 255, 255), (20, 20, 24)
RED_POCKET, BLACK_POCKET, GREEN_POCKET = (200, 22, 38), (34, 34, 38), (20, 140, 70)
WHEEL = (0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22,
         18, 29, 7, 28, 12, 35, 3, 26)                  # European wheel order, clockwise


def ease_out(f: float) -> float:
    return 1 - (1 - min(max(f, 0.0), 1.0)) ** 3


def background(w: int, h: int, top=BG_TOP, bottom=BG_BOTTOM) -> Image.Image:
    img = Image.new("RGB", (w, h), bottom)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 4 * SS):
        d.rectangle((0, y, w, y + 4 * SS), fill=_mix(top, bottom, y / h))
    return img


def to_gif(frames: list[Image.Image], hold_ms: int = HOLD_MS, frame_ms: int = FRAME_MS) -> bytes:
    """Frames (already at final size) -> GIF that plays once and stays on the last frame."""
    sample = Image.new("RGB", (frames[0].width, frames[0].height * 2))
    sample.paste(frames[len(frames) // 2], (0, 0))
    sample.paste(frames[-1], (0, frames[0].height))
    palette = sample.quantize(colors=160, method=Image.Quantize.MEDIANCUT)
    first, *rest = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    out = io.BytesIO()
    first.save(out, format="GIF", save_all=True, append_images=rest, optimize=False, disposal=1,
               duration=[frame_ms] * (len(frames) - 1) + [hold_ms])
    return out.getvalue()


def to_png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def text(d: ImageDraw.ImageDraw, xy, value: str, size: int, fill=WHITE, anchor="mm", stroke=0) -> None:
    d.text(xy, value, fill=fill, font=_font(size * SS), anchor=anchor,
           stroke_width=stroke * SS, stroke_fill=BG_BOTTOM if stroke else None)


# ------------------------------------------------------------ Shapes (no emoji in the built-in font)

def star(d, cx, cy, r, fill, points=5, inner=0.45) -> None:
    pts = []
    for i in range(points * 2):
        a = -math.pi / 2 + i * math.pi / points
        rr = r if i % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


def heart(d, cx, cy, r, fill) -> None:
    d.ellipse((cx - r, cy - r * 0.75, cx, cy + r * 0.25), fill=fill)
    d.ellipse((cx, cy - r * 0.75, cx + r, cy + r * 0.25), fill=fill)
    d.polygon([(cx - r * 0.98, cy - r * 0.1), (cx + r * 0.98, cy - r * 0.1), (cx, cy + r)], fill=fill)


def spade(d, cx, cy, r, fill) -> None:
    d.ellipse((cx - r, cy - r * 0.25, cx, cy + r * 0.75), fill=fill)
    d.ellipse((cx, cy - r * 0.25, cx + r, cy + r * 0.75), fill=fill)
    d.polygon([(cx - r * 0.98, cy + r * 0.1), (cx + r * 0.98, cy + r * 0.1), (cx, cy - r)], fill=fill)
    d.polygon([(cx, cy + r * 0.3), (cx - r * 0.35, cy + r), (cx + r * 0.35, cy + r)], fill=fill)


def club(d, cx, cy, r, fill) -> None:
    q = r * 0.48
    for dx, dy in ((0, -r * 0.45), (-r * 0.5, r * 0.15), (r * 0.5, r * 0.15)):
        d.ellipse((cx + dx - q, cy + dy - q, cx + dx + q, cy + dy + q), fill=fill)
    d.polygon([(cx, cy), (cx - r * 0.35, cy + r), (cx + r * 0.35, cy + r)], fill=fill)


def diamond(d, cx, cy, r, fill, wide=0.72) -> None:
    d.polygon([(cx, cy - r), (cx + r * wide, cy), (cx, cy + r), (cx - r * wide, cy)], fill=fill)


SUIT_SHAPES = {"♠": (spade, INK), "♥": (heart, (210, 20, 40)), "♦": (diamond, (210, 20, 40)), "♣": (club, INK)}


def symbol(d, name: str, cx: float, cy: float, r: float) -> None:
    """A slot symbol (the emoji in support/casino_games.py) drawn as shapes."""
    if name == "🍒":
        d.line([(cx - r * 0.45, cy + r * 0.2), (cx + r * 0.1, cy - r * 0.75), (cx + r * 0.45, cy + r * 0.15)],
               fill=(60, 140, 40), width=int(r * 0.13), joint="curve")
        d.ellipse((cx + r * 0.05, cy - r * 0.95, cx + r * 0.6, cy - r * 0.6), fill=(70, 170, 50))   # leaf
        for x in (-0.45, 0.45):
            k = r * 0.42
            d.ellipse((cx + r * x - k, cy + r * 0.25 - k, cx + r * x + k, cy + r * 0.25 + k), fill=(205, 15, 40))
            d.ellipse((cx + r * x - k * 0.5, cy + r * 0.25 - k * 0.6, cx + r * x - k * 0.1, cy + r * 0.25 - k * 0.2),
                      fill=(255, 140, 150))
    elif name == "🍋":
        d.ellipse((cx - r * 0.85, cy - r * 0.6, cx + r * 0.85, cy + r * 0.6), fill=(250, 215, 30))
        d.ellipse((cx - r * 0.95, cy - r * 0.12, cx - r * 0.7, cy + r * 0.12), fill=(250, 215, 30))
        d.ellipse((cx + r * 0.7, cy - r * 0.12, cx + r * 0.95, cy + r * 0.12), fill=(250, 215, 30))
        d.ellipse((cx - r * 0.45, cy - r * 0.4, cx - r * 0.05, cy - r * 0.15), fill=(255, 245, 160))
    elif name == "🔔":
        d.pieslice((cx - r * 0.7, cy - r * 0.85, cx + r * 0.7, cy + r * 0.5), 180, 360, fill=GOLD)
        d.polygon([(cx - r * 0.7, cy - r * 0.18), (cx + r * 0.7, cy - r * 0.18), (cx + r * 0.95, cy + r * 0.55),
                   (cx - r * 0.95, cy + r * 0.55)], fill=GOLD)
        d.ellipse((cx - r * 0.2, cy + r * 0.5, cx + r * 0.2, cy + r * 0.9), fill=GOLD_DARK)
        d.ellipse((cx - r * 0.1, cy - r * 1.0, cx + r * 0.1, cy - r * 0.78), fill=GOLD_DARK)
        d.line([(cx - r * 0.35, cy - r * 0.45), (cx - r * 0.5, cy + r * 0.35)], fill=GOLD_LIGHT, width=int(r * 0.1))
    elif name == "7️⃣":
        d.text((cx, cy + r * 0.05), "7", font=_font(int(r * 2.1)), anchor="mm", fill=(220, 20, 40),
               stroke_width=int(r * 0.09), stroke_fill=(120, 0, 15))
    elif name == "💎":
        d.polygon([(cx - r * 0.9, cy - r * 0.3), (cx - r * 0.5, cy - r * 0.75), (cx + r * 0.5, cy - r * 0.75),
                   (cx + r * 0.9, cy - r * 0.3), (cx, cy + r * 0.9)], fill=(60, 190, 255))
        d.polygon([(cx - r * 0.9, cy - r * 0.3), (cx + r * 0.9, cy - r * 0.3), (cx, cy + r * 0.9)],
                  fill=(30, 130, 220))
        d.polygon([(cx - r * 0.5, cy - r * 0.75), (cx - r * 0.1, cy - r * 0.3), (cx - r * 0.45, cy - r * 0.3)],
                  fill=(190, 240, 255))
    else:                                                   # ⭐
        star(d, cx, cy, r * 0.95, (255, 200, 40))
        star(d, cx - r * 0.05, cy - r * 0.05, r * 0.45, (255, 235, 140))


# ------------------------------------------------------------ Slots

def slots_gif(reels: list[str], win: bool) -> tuple[bytes, float]:
    """Three reels spin and stop one after another on `reels`. -> (GIF, seconds until the last reel stops)."""
    cell, pad = 120 * SS, 18 * SS
    w, h = cell * 3 + pad * 4, cell + pad * 2 + 40 * SS
    base = background(w, h, (40, 16, 10), (14, 6, 4))
    d = ImageDraw.Draw(base)
    top = pad + 20 * SS
    for i in range(3):
        x0 = pad + i * (cell + pad)
        d.rounded_rectangle((x0 - 4 * SS, top - 4 * SS, x0 + cell + 4 * SS, top + cell + 4 * SS), radius=14 * SS,
                            fill=GOLD_DARK)
    stops = [1300, 1850, 2400]                              # ms – the last reel stops after ~2.4 s
    strips = []
    for i, final in enumerate(reels):
        n = 14 + 6 * i
        strips.append([FX.choice(g.SLOT_SYMBOLS) for _ in range(n)] + [final])
    frames = []
    end = stops[-1] + 500
    for now in list(range(0, end, FRAME_MS)) + [end]:
        img = base.copy()
        d = ImageDraw.Draw(img)
        for i, strip in enumerate(strips):
            x0 = pad + i * (cell + pad)
            window = Image.new("RGB", (cell, cell), (246, 239, 230))
            wd = ImageDraw.Draw(window)
            f = now / stops[i]
            last = len(strip) - 1
            pos = last * ease_out(f) if f < 1 else last
            if 1 <= f < 1 + 160 / stops[i]:                 # a small bounce when the reel stops
                pos = last - 0.12 * math.sin((f - 1) * stops[i] / 160 * math.pi)
            for k in range(int(pos) - 1, int(pos) + 2):
                if 0 <= k <= last:
                    cy = cell / 2 + (pos - k) * cell
                    symbol(wd, strip[k], cell / 2, cy, cell * 0.33)
            for y in range(0, cell // 4, SS):              # shade at the top and bottom of the window
                a = 0.45 * (1 - y / (cell / 4))
                wd.line([(0, y), (cell, y)], fill=_mix((246, 239, 230), (60, 40, 30), a))
                wd.line([(0, cell - 1 - y), (cell, cell - 1 - y)], fill=_mix((246, 239, 230), (60, 40, 30), a))
            img.paste(window, (int(x0), int(top)))
        line = WIN if (win and now >= stops[-1]) else (229, 52, 28)
        d.line([(pad / 2, top + cell / 2), (w - pad / 2, top + cell / 2)], fill=line, width=3 * SS)
        if win and now >= stops[-1]:
            k = min(1.0, (now - stops[-1]) / 300)
            for i in range(3):
                x0 = pad + i * (cell + pad)
                d.rounded_rectangle((x0 - 6 * SS, top - 6 * SS, x0 + cell + 6 * SS, top + cell + 6 * SS),
                                    radius=14 * SS, outline=_mix(GOLD_DARK, WIN, k), width=4 * SS)
        frames.append(img.reduce(SS))
    return to_gif(frames), stops[-1] / 1000


# ------------------------------------------------------------ Coin flip

def _coin_face(side: str, r: int) -> Image.Image:
    face = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    d = ImageDraw.Draw(face)
    d.ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=GOLD_DARK)
    d.ellipse((r * 0.08, r * 0.08, 2 * r * 0.96, 2 * r * 0.96), fill=GOLD)
    d.ellipse((r * 0.22, r * 0.22, 2 * r * 0.89, 2 * r * 0.89), outline=GOLD_DARK, width=max(2, r // 30))
    if side == "heads":                                     # a crown
        d.polygon([(r * 0.5, r * 1.25), (r * 0.5, r * 0.7), (r * 0.75, r * 0.95), (r, r * 0.55), (r * 1.25, r * 0.95),
                   (r * 1.5, r * 0.7), (r * 1.5, r * 1.25)], fill=GOLD_DARK)
        d.rectangle((r * 0.5, r * 1.3, r * 1.5, r * 1.42), fill=GOLD_DARK)
    else:                                                   # a shield with a star
        d.polygon([(r * 0.55, r * 0.55), (r * 1.45, r * 0.55), (r * 1.45, r * 1.05), (r, r * 1.5), (r * 0.55, r * 1.05)],
                  fill=GOLD_DARK)
        star(d, r, r * 0.95, r * 0.28, GOLD)
    d.text((r, r * 1.68), side.upper(), font=_font(int(r * 0.2)), anchor="mm", fill=GOLD_DARK)
    return face


def coin_gif(side: str, won: bool) -> tuple[bytes, float]:
    w, h, r = 360 * SS, 300 * SS, 90 * SS
    base = background(w, h)
    faces = {s: _coin_face(s, r) for s in ("heads", "tails")}
    flip_ms = 2200
    turns = 9 + (0.5 if side == "tails" else 0)             # half turns: ends on the right face
    frames = []
    end = flip_ms + 600
    for now in list(range(0, end, FRAME_MS)) + [end]:
        img = base.copy()
        f = min(1.0, now / flip_ms)
        angle = turns * 2 * math.pi * ease_out(f)
        squash = abs(math.cos(angle))
        up = math.sin(math.pi * f) * 70 * SS               # thrown up and caught
        shown = faces["heads" if math.cos(angle) >= 0 else "tails"]
        fw = max(2, int(2 * r * squash))
        coin = shown.resize((fw, 2 * r))
        cy = h / 2 + 20 * SS - up
        shadow = ImageDraw.Draw(img)
        sw = r * (1 - 0.4 * (up / (70 * SS)))
        shadow.ellipse((w / 2 - sw, h - 38 * SS, w / 2 + sw, h - 26 * SS), fill=_mix(BG_BOTTOM, (0, 0, 0), 0.5))
        edge = Image.new("RGBA", (fw + 6 * SS, 2 * r), (0, 0, 0, 0))
        ImageDraw.Draw(edge).ellipse((0, 0, fw + 6 * SS - 1, 2 * r - 1), fill=GOLD_DARK + (255,))
        img.paste(edge, (int(w / 2 - fw / 2), int(cy - r + 3 * SS)), edge)
        img.paste(coin, (int(w / 2 - fw / 2), int(cy - r)), coin)
        if f >= 1:
            k = min(1.0, (now - flip_ms) / 300)
            d = ImageDraw.Draw(img)
            text(d, (w / 2, 26 * SS), side.upper(), 26, fill=_mix(BG_TOP, WIN if won else LOSE, k), stroke=1)
        frames.append(img.reduce(SS))
    return to_gif(frames), flip_ms / 1000


# ------------------------------------------------------------ Dice (roll 1–100, win at or under the target)

def dice_gif(roll: int, target: int, won: bool) -> tuple[bytes, float]:
    w, h = 600 * SS, 230 * SS
    base = background(w, h)
    d = ImageDraw.Draw(base)
    x0, x1, by = 40 * SS, w - 40 * SS, 150 * SS
    span = x1 - x0
    xt = x0 + span * target / 100
    d.rounded_rectangle((x0 - 8 * SS, by - 16 * SS, x1 + 8 * SS, by + 16 * SS), radius=16 * SS, fill=(47, 69, 83))
    d.rounded_rectangle((x0, by - 7 * SS, xt, by + 7 * SS), radius=7 * SS, fill=WIN)
    d.rounded_rectangle((xt, by - 7 * SS, x1, by + 7 * SS), radius=7 * SS, fill=LOSE)
    for v in (0, 25, 50, 75, 100):
        text(d, (x0 + span * v / 100, by + 34 * SS), str(v), 13, fill=(140, 160, 175))
    text(d, (xt, by - 30 * SS), f"{target} or under wins", 13, fill=(200, 230, 210))
    roll_ms = 1800
    frames = []
    end = roll_ms + 500
    shown = FX.randint(1, 100)
    for n, now in enumerate(list(range(0, end, FRAME_MS)) + [end]):
        img = base.copy()
        d = ImageDraw.Draw(img)
        f = min(1.0, now / roll_ms)
        if f < 1 and n % max(1, int(1 + 6 * f * f)) == 0:  # the number spins, slower and slower
            shown = FX.randint(1, 100)
        value = roll if f >= 1 else shown
        colour = (WIN if won else LOSE) if f >= 1 else WHITE
        # marker with the number: swings along the bar while rolling, then slides to the roll
        swing = x0 + span * (0.5 + 0.42 * math.sin(now / 150))
        settle = ease_out((f - 0.75) / 0.25) if f > 0.75 else 0
        mx = swing + (x0 + span * roll / 100 - swing) * settle
        d.rounded_rectangle((mx - 22 * SS, by - 40 * SS, mx + 22 * SS, by - 12 * SS), radius=6 * SS,
                            fill=_mix((47, 69, 83), colour, 0.85 if f >= 1 else 0.25))
        d.polygon([(mx - 7 * SS, by - 12 * SS), (mx + 7 * SS, by - 12 * SS), (mx, by - 2 * SS)],
                  fill=_mix((47, 69, 83), colour, 0.85 if f >= 1 else 0.25))
        text(d, (mx, by - 26 * SS), str(value), 15, fill=INK if f >= 1 else WHITE)
        bounce = 1 + 0.12 * math.sin(min(1.0, (now - roll_ms) / 250) * math.pi) if f >= 1 else 1
        text(d, (w / 2, 60 * SS), str(value), int(52 * bounce), fill=colour, stroke=1)
        frames.append(img.reduce(SS))
    return to_gif(frames), roll_ms / 1000


# ------------------------------------------------------------ Roulette (the wheel stops with the pocket at the top)

def _wheel(r: int) -> Image.Image:
    img = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=(90, 46, 14))
    step = 360 / len(WHEEL)
    inner = r * 0.92
    for i, n in enumerate(WHEEL):
        a0 = -90 - step / 2 + i * step
        colour = GREEN_POCKET if n == 0 else RED_POCKET if n in g.RED else BLACK_POCKET
        d.pieslice((r - inner, r - inner, r + inner, r + inner), a0, a0 + step, fill=colour, outline=GOLD_DARK)
    font = _font(int(r * 0.085))
    for i, n in enumerate(WHEEL):                            # numbers, turned to face out
        a = math.radians(-90 + i * step)
        tile = Image.new("RGBA", (int(r * 0.2), int(r * 0.14)), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((tile.width / 2, tile.height / 2), str(n), font=font, anchor="mm", fill=WHITE)
        tile = tile.rotate(-math.degrees(a) - 90, expand=True, resample=Image.Resampling.BICUBIC)
        cx, cy = r + math.cos(a) * r * 0.8, r + math.sin(a) * r * 0.8
        img.paste(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)), tile)
    d.ellipse((r * 0.36, r * 0.36, r * 1.64, r * 1.64), fill=(122, 74, 16))
    d.ellipse((r * 0.5, r * 0.5, r * 1.5, r * 1.5), fill=(160, 104, 30))
    for k in range(4):                                       # the little cross in the middle
        a = math.radians(k * 90)
        d.line([(r, r), (r + math.cos(a) * r * 0.42, r + math.sin(a) * r * 0.42)], fill=GOLD, width=max(2, r // 40))
    d.ellipse((r * 0.9, r * 0.9, r * 1.1, r * 1.1), fill=GOLD)
    return img


def roulette_gif(pocket: int, won: bool) -> tuple[bytes, float]:
    """The wheel is drawn once at twice the size; the frames turn the normal-size wheel (fast)."""
    w = h = 420
    r = 180
    base = background(w * SS, h * SS).reduce(SS)
    wheel = _wheel(r * SS).reduce(SS)
    step = 360 / len(WHEEL)
    final = -WHEEL.index(pocket) * step                      # rotation that puts the pocket at the top
    turns = 4 * 360
    spin_ms = 3000
    colour = "green" if pocket == 0 else "red" if pocket in g.RED else "black"
    frames = []
    end = spin_ms + 600
    cx, cy = w / 2, h / 2 + 10
    for now in list(range(0, end, FRAME_MS)) + [end]:
        img = base.copy()
        f = min(1.0, now / spin_ms)
        angle = final - turns * (1 - ease_out(f))            # clockwise, slowing down
        turned = wheel.rotate(-angle, resample=Image.Resampling.BICUBIC)
        img.paste(turned, (int(cx - r), int(cy - r)), turned)
        over = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))   # ball + pointer drawn smooth
        d = ImageDraw.Draw(over)
        # the ball runs the other way round the rim, then drops into the pocket at the top
        ball_a = math.radians(-90 + 5 * 360 * (1 - ease_out(min(1.0, now / (spin_ms * 0.85)))))
        ball_r = r * (0.97 - 0.13 * ease_out(min(1.0, max(0.0, (now - spin_ms * 0.6) / (spin_ms * 0.35)))))
        bx, by = (cx + math.cos(ball_a) * ball_r) * SS, (cy + math.sin(ball_a) * ball_r) * SS
        br = 8 * SS
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=(245, 245, 245, 255), outline=(150, 150, 150, 255))
        d.polygon([(w / 2 * SS - 12 * SS, 2 * SS), (w / 2 * SS + 12 * SS, 2 * SS), (w / 2 * SS, 26 * SS)],
                  fill=GOLD + (255,), outline=GOLD_DARK + (255,))
        if f >= 1:
            k = min(1.0, (now - spin_ms) / 300)
            fill = {"red": RED_POCKET, "black": BLACK_POCKET, "green": GREEN_POCKET}[colour]
            rr = 46 * SS * (0.6 + 0.4 * k)
            d.ellipse((cx * SS - rr, cy * SS - rr, cx * SS + rr, cy * SS + rr), fill=fill + (255,),
                      outline=(WIN if won else GOLD) + (255,), width=4 * SS)
            text(d, (cx * SS, cy * SS), str(pocket), int(34 * (0.6 + 0.4 * k)))
        over = over.reduce(SS)
        img.paste(over, (0, 0), over)
        frames.append(img)
    return to_gif(frames), spin_ms / 1000


# ------------------------------------------------------------ Blackjack (a still picture of the table)

def card(d: ImageDraw.ImageDraw, x: float, y: float, c: int | None, w: int = 70 * SS, h: int = 100 * SS) -> None:
    """Card c (0–51, see support/casino_games.py) or the back (None) with its top-left corner at x, y."""
    d.rounded_rectangle((x + 3 * SS, y + 4 * SS, x + w + 3 * SS, y + h + 4 * SS), radius=8 * SS, fill=(0, 30, 15))
    if c is None:
        d.rounded_rectangle((x, y, x + w, y + h), radius=8 * SS, fill=(122, 29, 14), outline=WHITE, width=3 * SS)
        step = 10 * SS
        for gy in range(int(y + 12 * SS), int(y + h - 8 * SS), step):        # a little diamond pattern
            for gx in range(int(x + 12 * SS), int(x + w - 8 * SS), step):
                diamond(d, gx, gy, 3.5 * SS, (168, 41, 15), wide=1)
        return
    rank, suit = g.RANKS[c % 13], g.SUITS[(c // 13) % 4]          # 6 decks: 0–311
    shape, colour = SUIT_SHAPES[suit]
    d.rounded_rectangle((x, y, x + w, y + h), radius=8 * SS, fill=(250, 246, 240))
    d.text((x + 8 * SS, y + 6 * SS), rank, font=_font(20 * SS), fill=colour, anchor="la")
    shape(d, x + 15 * SS, y + 38 * SS, 7 * SS, colour)
    shape(d, x + w * 0.58, y + h * 0.62, 20 * SS, colour)


def blackjack_png(state: dict, player_name: str = "You") -> bytes:
    hands = state["hands"]
    w = max(560, 120 + 90 * max(len(state["dealer"]), max(len(h["cards"]) for h in hands)) * len(hands)) * SS
    h = 370 * SS
    img = background(w, h, (14, 92, 52), (8, 58, 32))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((10 * SS, 10 * SS, w - 10 * SS, h - 10 * SS), radius=30 * SS, outline=(196, 160, 60),
                        width=3 * SS)
    finished = state["finished"]
    dealer = state["dealer"]
    total = g.hand_total(dealer if finished else dealer[:1])[0]
    text(d, (30 * SS, 30 * SS), f"Dealer · {total}" if finished else f"Dealer · {total} + ?", 16, anchor="la")
    for i, c in enumerate(dealer):
        card(d, 30 * SS + i * 52 * SS, 52 * SS, c if (finished or i == 0) else None)
    col = (w - 40 * SS) / len(hands)
    for k, hand in enumerate(hands):
        x = 30 * SS + k * col
        tot, soft = g.hand_total(hand["cards"])
        label = f"{player_name if len(hands) == 1 else f'Hand {k + 1}'} · {'soft ' if soft and tot <= 21 else ''}{tot}"
        active = not finished and k == state["active"] and len(hands) > 1
        result = hand.get("result")
        colour = {"win": WIN, "blackjack": GOLD, "push": WHITE, "lose": LOSE, "bust": LOSE}.get(result, WHITE)
        text(d, (x, 186 * SS), label + ("  <" if active else ""), 16, fill=GOLD if active else WHITE, anchor="la")
        for i, c in enumerate(hand["cards"]):
            card(d, x + i * 52 * SS, 208 * SS, c)
        if result:
            word = {"win": "WIN", "blackjack": "BLACKJACK!", "push": "PUSH", "lose": "LOSE", "bust": "BUST"}[result]
            text(d, (x + 2 * SS, 322 * SS), word, 18, fill=colour, anchor="la", stroke=1)
    return to_png(img.reduce(SS))


# ------------------------------------------------------------ Horse racing (the horses cross the line in the drawn order)

COUNT_MS = 1600                    # 3 – 2 – 1 – GO before the gates open
RACE_MS = 5600                     # the winner crosses the line this long after the gates open
RACE_FRAME_MS = 50                 # a longer animation: fewer frames keep the GIF small
STRIDE_MS = 360
TURF, TURF_2, RAIL, SKIN = (47, 107, 52), (42, 97, 48), (244, 244, 244), (241, 201, 165)
MEDALS = ((255, 214, 90), (220, 220, 220), (214, 140, 80))
ORDINAL = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th")


def _hex(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _rot(px: float, py: float, ox: float, oy: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    dx, dy = px - ox, py - oy
    return ox + dx * math.cos(a) - dy * math.sin(a), oy + dx * math.sin(a) + dy * math.cos(a)


def _horse(d: ImageDraw.ImageDraw, x: float, y: float, coat, silk, number: int, phase: float | None, k: float) -> None:
    """Horse and jockey in racing silks, facing right – the same figure as on the website. (x, y) = the nose tip on
    the ground line; k = scale (pixels per unit of the 80×52 drawing); phase = stride 0..1, None = standing."""
    ox, oy = x - 73 * k, y - 50 * k                       # the drawing's (0, 0)
    P = lambda px, py: (ox + px * k, oy + py * k)         # noqa: E731
    dark = (28, 18, 12)
    bob = -3 * k * math.sin(phase * 2 * math.pi) ** 2 if phase is not None else 0

    def leg(hx, hy, offset):
        swing = 0 if phase is None else (-34 + 64 * abs(((phase + offset) % 1) * 2 - 1))
        hip = P(hx, hy)
        knee = _rot(hip[0] + 1 * k, hip[1] + 9 * k, hip[0], hip[1], swing)
        hoof = _rot(hip[0], hip[1] + 17 * k, hip[0], hip[1], swing)
        d.line((hip, knee, hoof), fill=coat, width=max(2, round(3.4 * k)), joint="curve")
        d.ellipse((hoof[0] - 1.6 * k, hoof[1] - 1.2 * k, hoof[0] + 1.6 * k, hoof[1] + 1.2 * k), fill=dark)
    leg(22, 28 + bob / k, 0.5)
    leg(50, 28 + bob / k, 0.0)
    B = lambda px, py: (ox + px * k, oy + py * k + bob)   # noqa: E731
    d.polygon([B(16, 22), B(6, 23), B(3, 33), B(2, 40), B(9, 33), B(16, 28)], fill=dark)               # tail
    d.ellipse((*B(14, 16.5), *B(54, 35.5)), fill=coat)                                                   # barrel
    d.polygon([B(46, 22), B(58, 8), B(64, 9), B(56, 28)], fill=coat)                                     # neck
    d.polygon([B(57, 6), B(72, 12), B(73, 17), B(67, 18), B(58, 13)], fill=coat)                        # head
    d.polygon([B(58, 7), B(60, 2), B(62, 7)], fill=coat)                                                 # ear
    d.polygon([B(47, 21), B(58, 7), B(56, 6), B(45, 19)], fill=dark)                                     # mane
    d.ellipse((*B(64.9, 10.4), *B(67.1, 12.6)), fill=(0, 0, 0))                                          # eye
    d.rounded_rectangle((*B(27, 17), *B(42, 28)), radius=2 * k, fill=silk)                               # saddle cloth
    text(d, B(34.5, 22.6), str(number), max(6, round(7 * k / SS)), fill=WHITE, stroke=1)
    d.polygon([B(30, 17), B(38, 4), B(46, 7), B(41, 18)], fill=silk)                                     # jockey
    d.line((B(41, 9), B(52, 12)), fill=silk, width=max(2, round(3 * k)))                                # arm
    d.ellipse((*B(39.8, -0.7), *B(48.2, 7.7)), fill=SKIN)                                                # face
    d.pieslice((*B(39.4, -1.4), *B(48.8, 8)), 180, 360, fill=silk)                                       # cap
    d.polygon([B(47, 3.2), B(51, 3.6), B(48, 2.2)], fill=silk)                                           # cap peak


def race_gif(order: list[int], pick: int, won: bool) -> tuple[bytes, float]:
    """The race like on the website: turf, rails, starting gate, a 3-2-1 countdown, galloping horses in silks with
    dust, the places in each lane and the winner on top. The horses follow g.race_paths, so they cross the line in
    exactly the order the server drew. The player's lane is framed."""
    lanes = len(g.HORSES)
    lane_h, top = 52 * SS, 58 * SS
    w, h = 680 * SS, top + lanes * lane_h + 12 * SS
    x_gate, x_fin = 92 * SS, w - 48 * SS
    k = 0.92 * SS                                              # horse size
    base = Image.new("RGB", (w, h), BG_BOTTOM)
    d = ImageDraw.Draw(base)
    for y in range(0, top, 4 * SS):                            # header
        d.rectangle((0, y, w, y + 4 * SS), fill=_mix(BG_TOP, BG_BOTTOM, y / top))
    ty, by = top - 6 * SS, h - 6 * SS
    for i, x in enumerate(range(0, w, 46 * SS)):               # mowing stripes
        d.rectangle((x, ty, x + 46 * SS, by), fill=TURF if i % 2 == 0 else TURF_2)
    for y in (ty, by - 4 * SS):                                # rails with posts
        d.rectangle((0, y, w, y + 5 * SS), fill=RAIL)
        for x in range(0, w, 30 * SS):
            d.rectangle((x, y, x + 3 * SS, y + 5 * SS), fill=(200, 200, 200))
    for frac, label in ((0.25, "1/4"), (0.5, "1/2"), (0.75, "3/4")):   # distance marks
        mx = x_gate + (x_fin - x_gate) * frac
        for yy in range(ty + 8 * SS, by - 8 * SS, 8 * SS):
            d.line((mx, yy, mx, yy + 4 * SS), fill=_mix(TURF, WHITE, 0.3), width=SS)
        text(d, (mx + 10 * SS, ty + 12 * SS), label, 8, fill=_mix(TURF, WHITE, 0.55))
    for i, (name, colour, _) in enumerate(g.HORSES):
        y = top + i * lane_h
        if i:
            d.line((0, y, w, y), fill=_mix(TURF, WHITE, 0.18), width=SS)
        if i == pick:
            d.rectangle((3 * SS, y + 2 * SS, w - 4 * SS, y + lane_h - 2 * SS), outline=GOLD, width=2 * SS)
        d.rounded_rectangle((10 * SS, y + 12 * SS, 38 * SS, y + 40 * SS), radius=5 * SS, fill=_hex(colour))
        text(d, (24 * SS, y + 26 * SS), str(i + 1), 13, fill=WHITE, stroke=1)
        if i == pick:
            text(d, (24 * SS, y + 47 * SS), "YOU", 7, fill=GOLD)
    sq = 7 * SS                                                # checkered finish line
    for row, yy in enumerate(range(ty + 5 * SS, by - 4 * SS, sq)):
        for col in range(2):
            fill = WHITE if (row + col) % 2 == 0 else INK
            d.rectangle((x_fin + col * sq, yy, x_fin + col * sq + sq, min(yy + sq, by - 4 * SS)), fill=fill)
    paths = g.race_paths(order, FX)
    places = {horse: rank for rank, horse in enumerate(order)}
    end = COUNT_MS + int(RACE_MS * max(p["finish"] for p in paths.values())) + 700
    dust_phase = {i: FX.random() for i in range(lanes)}
    frames = []
    win = order[0]
    for now in list(range(0, end, RACE_FRAME_MS)) + [end]:
        img = base.copy()
        d = ImageDraw.Draw(img)
        t_race = (now - COUNT_MS) / RACE_MS
        if now < COUNT_MS + 250:                               # the starting gate, lifted at GO
            lift = max(0.0, (now - COUNT_MS) / 250)
            gy = ty + 5 * SS - (by - ty) * lift
            for n, yy in enumerate(range(int(gy), int(gy + (by - ty) - 9 * SS), 10 * SS)):
                d.rectangle((x_gate - 6 * SS, yy, x_gate + 4 * SS, yy + 10 * SS),
                            fill=(229, 52, 28) if n % 2 == 0 else WHITE)
        for horse, path in paths.items():
            prog = 0.0
            if t_race > 0:
                prog = 1.0
                pts = path["points"]
                for (ta, pa), (tb, pb) in zip(pts, pts[1:]):
                    if t_race <= tb:
                        prog = pa + (pb - pa) * ((t_race - ta) / (tb - ta) if tb > ta else 1)
                        break
            running = 0 < prog < 1.0
            nose = x_gate + 70 * k + (x_fin - x_gate - 72 * k) * min(prog, 1.0)
            ground = top + horse * lane_h + lane_h - 5 * SS
            if running:                                        # dust behind the hooves
                f = ((now / STRIDE_MS) + dust_phase[horse]) % 1
                r = (4 + 9 * f) * SS
                cx, cy = nose - 72 * k - 14 * f * SS, ground - 2 * SS
                d.ellipse((cx - r, cy - r * 0.55, cx + r, cy + r * 0.55),
                          fill=_mix((214, 190, 150), TURF, 0.35 + 0.6 * f))
            phase = ((now / STRIDE_MS) + horse * 0.17) % 1 if running else None
            _horse(d, nose, ground, _hex(g.HORSE_COATS[horse % len(g.HORSE_COATS)]), _hex(g.HORSES[horse][1]),
                   horse + 1, phase, k)
            if prog >= 1.0:                                    # over the line: its place, in front of the horse
                rank = places[horse]
                bx, cy = nose - 102 * k, top + horse * lane_h + lane_h / 2
                fill = MEDALS[rank] if rank < 3 else (25, 30, 35)
                d.rounded_rectangle((bx - 22 * SS, cy - 11 * SS, bx + 22 * SS, cy + 11 * SS), radius=11 * SS, fill=fill)
                text(d, (bx, cy), ORDINAL[rank], 11, fill=INK if rank < 3 else WHITE)
        if now < COUNT_MS:                                     # 3 – 2 – 1 – GO!
            step = now // (COUNT_MS // 4)
            word = ("3", "2", "1", "GO!")[min(step, 3)]
            within = (now % (COUNT_MS // 4)) / (COUNT_MS // 4)
            size = int(54 - 18 * min(1.0, within * 3))
            text(d, (w / 2, top + lanes * lane_h / 2), word, size, fill=GOLD if word == "GO!" else WHITE, stroke=2)
            text(d, (w / 2, 28 * SS), "RUST DOWNS  ·  GET READY", 15, fill=GOLD, stroke=1)
        elif t_race < 1.0:
            line = ("AND THEY'RE OFF!" if t_race < 0.3 else "NECK AND NECK ..." if t_race < 0.62
                    else "INTO THE FINAL STRETCH!" if t_race < 0.86 else "PHOTO FINISH ...")
            text(d, (w / 2, 28 * SS), line, 16, fill=WHITE, stroke=1)
        else:                                                  # the winner, with its silks
            name = f"{g.HORSES[win][0].upper()} WINS!"
            tw = d.textlength(name, font=_font(18 * SS))
            sx = w / 2 - tw / 2 - 20 * SS
            d.ellipse((sx - 13 * SS, 15 * SS, sx + 13 * SS, 41 * SS), fill=_hex(g.HORSES[win][1]),
                      outline=GOLD, width=2 * SS)
            text(d, (sx, 28 * SS), str(win + 1), 12, fill=WHITE, stroke=1)
            text(d, (w / 2 + 10 * SS, 28 * SS), name, 18, fill=WIN if won else GOLD, stroke=1)
        frames.append(img.reduce(SS))
    return to_gif(frames, frame_ms=RACE_FRAME_MS), (COUNT_MS + RACE_MS) / 1000
