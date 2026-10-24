"""Plinko board pictures for Discord (/plinko, plugins/casino.py): an animated GIF of the balls falling peg by peg
along the paths the server picked, ending on the landed slots. Same look as the website board (plinko.js): dark
board, white pegs, red-to-yellow slots, pink balls.

Drawn at twice the size and scaled down (smooth, round edges), ~33 frames a second: balls with light and shadow and a
short trail, pegs glow when a ball hits them, a slot bounces and shows the multiplier when a ball lands in it, and the
last frame marks every slot that was hit (×N when several balls landed there). Plays once and stays on the result.
Pillow only, no files.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

from support import casino_games as g

WIDTH = 600                      # final picture width (px)
SS = 2                           # drawn this many times bigger, then scaled down (anti-aliasing)
BG_TOP, BG_BOTTOM = (19, 41, 56), (11, 26, 36)
PEG = (236, 242, 248)
BALL = (255, 38, 84)
BALL_DARK = (190, 16, 58)
BALL_LIGHT = (255, 190, 205)
FRAME_MS = 30                    # ~33 frames a second
ROW_MS = 150                     # time a ball needs from one row to the next
LAUNCH_MS = 1500                 # all balls of one drop leave the top within this time
GLOW_MS = 220                    # a hit peg glows this long
BOUNCE_MS = 260                  # a slot bounces this long when a ball lands
POPUP_MS = 700                   # "2.31×" floats above the slot this long
HOLD_MS = 6000                   # the last frame


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, f: float) -> tuple[int, int, int]:
    """Colour between a (f=0) and b (f=1)."""
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                         # old Pillow: one fixed size
        return ImageFont.load_default()


class Board:
    def __init__(self, risk: str, rows: int, width: int = WIDTH) -> None:
        self.risk, self.rows = risk, rows
        self.w = width * SS
        self.s = self.w / (rows + 2)                         # peg spacing
        self.row_h = self.s * 0.92
        self.top = self.s * 1.15
        self.peg_r = max(1.8 * SS, self.s * 0.105)
        self.ball_r = max(3.5 * SS, self.s * 0.27)
        self.slot_y = self.top + (rows - 1) * self.row_h + self.row_h * 0.8
        self.slot_h = max(20.0 * SS, self.s * 0.74)
        self.h = int(self.slot_y + self.slot_h + 14 * SS)
        self.cx = self.w / 2
        self.table = g.PLINKO_TABLES[(risk, rows)]
        self.colours = [_rgb(g.plinko_colour(k, rows)) for k in range(rows + 1)]
        self.labels = [g.fmt_multiplier(c) + ("x" if self.s > 34 * SS else "") for c in self.table]   # (the built-in font has no ×)
        self.fonts = [self._fit(label) for label in self.labels]
        self.big_font = _font(int(max(11, min(17, self.s / SS * 0.5)) * SS))
        self._empty = self._draw_board()                     # background + pegs
        self._board = self._empty.copy()                     # … + the slots at rest (only moving ones are redrawn)
        draw = ImageDraw.Draw(self._board)
        for k in range(rows + 1):
            self._slot(draw, k)

    def _fit(self, label: str):
        size = int(max(7, min(14, self.s / SS * 0.38)) * SS)
        font = _font(size)
        while size > 6 * SS and font.getlength(label) > self.s - 6 * SS:
            size -= SS
            font = _font(size)
        return font

    # ------------------------------------------------------------ geometry
    def peg(self, r: int, j: int) -> tuple[float, float]:
        return self.cx + (j - 1 - r / 2) * self.s, self.top + r * self.row_h

    def slot_centre(self, k: int) -> float:
        return self.cx + (k - self.rows / 2) * self.s

    def ball_at(self, path: list[int], t: float) -> tuple[float, float]:
        """Where a ball is after t rows (0 = above the board, rows + 1 = in its slot)."""
        i, f = int(t), t - int(t)

        def point(r: int) -> tuple[float, float]:
            if r < 0:
                return self.cx, self.top - self.row_h * 1.25
            x = self.cx + (sum(path[:r]) - r / 2) * self.s
            if r >= self.rows:
                return x, self.slot_y + self.slot_h * 0.3
            return x, self.top + r * self.row_h - self.peg_r - self.ball_r

        (ax, ay), (zx, zy) = point(i - 1), point(i)
        lift = 0 if i == 0 else self.row_h * 0.45             # bounces up off the peg, then falls
        ease = f * f * (3 - 2 * f)                            # sideways: smooth start and stop
        return ax + (zx - ax) * ease, ay + (zy - ay) * f * f - lift * 4 * f * (1 - f) * (1 - f)

    # ------------------------------------------------------------ drawing
    def _draw_board(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG_BOTTOM)
        d = ImageDraw.Draw(img)
        for y in range(0, self.h, 4 * SS):                    # soft vertical gradient
            d.rectangle((0, y, self.w, y + 4 * SS), fill=_mix(BG_TOP, BG_BOTTOM, y / self.h))
        for r in range(self.rows):
            for j in range(r + 3):
                x, y = self.peg(r, j)
                rad = self.peg_r
                d.ellipse((x - rad, y - rad + SS, x + rad, y + rad + SS), fill=_mix(BG_BOTTOM, (0, 0, 0), 0.4))
                d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=PEG)
        return img

    def _slot(self, d: ImageDraw.ImageDraw, k: int, dy: float = 0, glow: float = 0, outline: bool = False) -> None:
        x0 = self.slot_centre(k) - self.s / 2 + 2 * SS
        x1 = x0 + self.s - 4 * SS
        y0 = self.slot_y + dy
        y1 = y0 + self.slot_h
        colour = _mix(self.colours[k], (255, 255, 255), 0.45 * glow)
        d.rounded_rectangle((x0, self.slot_y + 4 * SS, x1, self.slot_y + self.slot_h + 4 * SS), radius=5 * SS,
                            fill=_mix(self.colours[k], (0, 0, 0), 0.55))          # the slot's dark base
        d.rounded_rectangle((x0, y0, x1, y1), radius=5 * SS, fill=colour,
                            outline=(255, 255, 255) if outline else None, width=2 * SS if outline else 0)
        d.text(((x0 + x1) / 2, (y0 + y1) / 2 + SS), self.labels[k], fill=(30, 10, 0), font=self.fonts[k],
               anchor="mm")

    def _ball(self, d: ImageDraw.ImageDraw, x: float, y: float, fade: float = 1.0, scale: float = 1.0) -> None:
        rad = self.ball_r * scale
        if fade < 1:                                          # trail: a faded, smaller ball
            d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=_mix(BG_BOTTOM, BALL, fade))
            return
        d.ellipse((x - rad * 0.9, y + rad * 0.55, x + rad * 0.9, y + rad * 1.05), fill=_mix(BG_BOTTOM, (0, 0, 0), .35))
        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=BALL_DARK)
        d.ellipse((x - rad * 0.88, y - rad * 0.95, x + rad * 0.8, y + rad * 0.7), fill=BALL)
        d.ellipse((x - rad * 0.55, y - rad * 0.68, x - rad * 0.05, y - rad * 0.2), fill=BALL_LIGHT)

    def frame(self, now: float, balls: list[dict], *, final: bool = False) -> Image.Image:
        """One picture at `now` ms. balls: {"path", "start" (ms)}."""
        img = self._board.copy()
        d = ImageDraw.Draw(img)
        # pegs that were just hit
        for b in balls:
            for r in range(self.rows):
                age = now - (b["start"] + (r + 1) * ROW_MS)
                if 0 <= age < GLOW_MS:
                    k = 1 - age / GLOW_MS
                    x, y = self.peg(r, sum(b["path"][:r]) + 1)
                    for grow, f in ((2.6, 0.18), (1.9, 0.35)):
                        rad = self.peg_r * (1 + (grow - 1) * k)
                        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=_mix(BG_TOP, (255, 255, 255), f * k))
                    d.ellipse((x - self.peg_r, y - self.peg_r, x + self.peg_r, y + self.peg_r), fill=(255, 255, 255))
        # slots: bounce + flash when a ball lands
        landed: dict[int, int] = {}
        bounce: dict[int, float] = {}
        for b in balls:
            land = b["start"] + (self.rows + 1) * ROW_MS
            if now >= land:
                landed[b["slot"]] = landed.get(b["slot"], 0) + 1
                age = now - land
                if age < BOUNCE_MS:
                    bounce[b["slot"]] = max(bounce.get(b["slot"], 0), 1 - age / BOUNCE_MS)
        for k in range(self.rows + 1):
            k_b = bounce.get(k, 0)
            if k_b or (final and k in landed):                # redraw only a moving / marked slot
                x0 = int(self.slot_centre(k) - self.s / 2)
                box = (x0, int(self.slot_y - 2 * SS), int(x0 + self.s + 1), int(self.slot_y + self.slot_h + 12 * SS))
                img.paste(self._empty.crop(box), box[:2])
                self._slot(d, k, dy=math.sin(k_b * math.pi) * 7 * SS, glow=k_b, outline=final and k in landed)
        # balls (with a short trail)
        for b in balls:
            t = (now - b["start"]) / ROW_MS
            if 0 <= t < self.rows + 1:
                for lag, fade, scale in ((1.2, 0.18, 0.7), (0.6, 0.35, 0.85)):
                    if t - lag / 4 > 0:
                        self._ball(d, *self.ball_at(b["path"], t - lag / 4), fade=fade, scale=scale)
                self._ball(d, *self.ball_at(b["path"], t))
        # "2.31×" floating up from the slot
        newest: dict[int, float] = {}                         # one popup per slot: the latest landing
        for b in balls:
            age = now - (b["start"] + (self.rows + 1) * ROW_MS)
            if 0 <= age < POPUP_MS and not final:
                newest[b["slot"]] = min(age, newest.get(b["slot"], age))
        for k, age in newest.items():
            f = age / POPUP_MS
            colour = _mix(self.colours[k], BG_TOP, max(0.0, f * 1.4 - 0.4))
            d.text((self.slot_centre(k), self.slot_y - 6 * SS - f * 26 * SS), self.labels[k].rstrip("x") + "x",
                   fill=colour, font=self.big_font, anchor="mb", stroke_width=SS, stroke_fill=BG_BOTTOM)
        if final:                                             # how many balls landed where
            for k, n in landed.items():
                if n > 1:
                    x, y = self.slot_centre(k), self.slot_y - 5 * SS
                    d.text((x, y), f"x{n}", fill=(255, 255, 255), font=self.big_font, anchor="mb",
                           stroke_width=SS, stroke_fill=BG_BOTTOM)
        return img.reduce(SS)                                 # average of SS×SS pixels: smooth edges, fast


def _starts(count: int) -> list[float]:
    """When each ball leaves the top: one after another, all within LAUNCH_MS."""
    gap = min(110.0, LAUNCH_MS / max(1, count - 1)) if count > 1 else 0
    return [i * gap for i in range(count)]


def plinko_gif(risk: str, rows: int, paths: list[list[int]]) -> tuple[bytes, float]:
    """Animated GIF of the drop (plays once). Returns (bytes, seconds until every ball has landed)."""
    board = Board(risk, rows)
    balls = [{"path": p, "slot": sum(p), "start": s} for p, s in zip(paths, _starts(len(paths)))]
    end = max(b["start"] for b in balls) + (rows + 1) * ROW_MS + max(BOUNCE_MS, POPUP_MS)
    frames = [board.frame(now, balls) for now in range(0, int(end), FRAME_MS)]
    frames.append(board.frame(end, balls, final=True))
    durations = [FRAME_MS] * (len(frames) - 1) + [HOLD_MS]
    sample = Image.new("RGB", (frames[0].width, frames[0].height * 2))             # palette from a busy frame
    sample.paste(frames[len(frames) // 2], (0, 0))                                  # and the result
    sample.paste(frames[-1], (0, frames[0].height))
    palette = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    out = io.BytesIO()
    first, *rest = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    first.save(out, format="GIF", save_all=True, append_images=rest, duration=durations, optimize=False,
               disposal=1)                                    # no loop: plays once and stays on the result
    landed = (max(b["start"] for b in balls) + (rows + 1) * ROW_MS) / 1000
    return out.getvalue(), landed


def plinko_png(risk: str, rows: int, hits: set[int] = frozenset()) -> bytes:
    board = Board(risk, rows)
    balls = [{"path": [1] * k + [0] * (rows - k), "slot": k, "start": -10_000} for k in hits]
    out = io.BytesIO()
    board.frame(0, balls, final=True).save(out, format="PNG", optimize=True)
    return out.getvalue()
