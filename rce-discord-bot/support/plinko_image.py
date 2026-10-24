"""Plinko board pictures for Discord (/plinko, plugins/casino.py): an animated GIF of the balls falling peg by peg
along the paths the server picked, ending on the landed slots. Same look as the website board (plinko.js): dark
board, white pegs, red-to-yellow slots, pink balls. Pillow only, no files.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from support import casino_games as g

WIDTH = 520
BG = (15, 33, 46)
PEG = (255, 255, 255)
BALL = (255, 45, 85)
BALL_LIGHT = (255, 157, 176)
FRAME_MS = 55                    # one half row
STAGGER = 2                      # frames between the balls of one drop
HOLD_MS = 4000                   # the last frame (landed) – the GIF plays once and stays there


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


class Board:
    def __init__(self, risk: str, rows: int, width: int = WIDTH) -> None:
        self.risk, self.rows, self.w = risk, rows, width
        self.s = width / (rows + 2)                          # peg spacing
        self.row_h = self.s * 0.92
        self.top = self.s * 0.9
        self.peg_r = max(1.6, self.s * 0.11)
        self.ball_r = max(3.0, self.s * 0.27)
        self.slot_y = self.top + (rows - 1) * self.row_h + self.row_h * 0.75
        self.slot_h = max(18.0, self.s * 0.72)
        self.h = int(self.slot_y + self.slot_h + 10)
        self.cx = width / 2
        self.table = g.PLINKO_TABLES[(risk, rows)]
        self.font = self._font(max(7, min(13, int(self.s * 0.36))))
        self._base = self._draw_base()

    @staticmethod
    def _font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:                                    # old Pillow: one fixed size
            return ImageFont.load_default()

    def _slot_box(self, k: int, dy: float = 0) -> tuple[float, float, float, float]:
        x = self.cx + (k - self.rows / 2) * self.s - self.s / 2 + 1.5
        y = self.slot_y + dy
        return x, y, x + self.s - 3, y + self.slot_h

    def _draw_base(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        for r in range(self.rows):
            for j in range(r + 3):
                x, y, rad = self.cx + (j - 1 - r / 2) * self.s, self.top + r * self.row_h, self.peg_r
                d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=PEG)
        for k in range(self.rows + 1):
            self._draw_slot(d, k)
        return img

    def _draw_slot(self, d: ImageDraw.ImageDraw, k: int, *, hit: bool = False) -> None:
        x0, y0, x1, y1 = self._slot_box(k, 4 if hit else 0)
        d.rounded_rectangle((x0, y0 + 3, x1, y1 + 3), radius=4, fill=(8, 18, 26))
        d.rounded_rectangle((x0, y0, x1, y1), radius=4, fill=_rgb(g.plinko_colour(k, self.rows)),
                            outline=(255, 255, 255) if hit else None, width=2 if hit else 0)
        label = g.fmt_multiplier(self.table[k]) + ("×" if self.s > 34 else "")
        font = self.font
        while font.getlength(label) > self.s - 5 and getattr(font, "size", 0) > 6:
            font = self._font(font.size - 1)
        d.text(((x0 + x1) / 2, (y0 + y1) / 2 + 0.5), label, fill=(26, 9, 0), font=font, anchor="mm")

    def ball_at(self, path: list[int], t: float) -> tuple[float, float]:
        """Where a ball is after t rows (0 = above the board, rows + 1 = in its slot)."""
        i, f = int(t), t - int(t)

        def point(r: int) -> tuple[float, float]:
            if r < 0:
                return self.cx, self.top - self.row_h * 1.3
            x = self.cx + (sum(path[:r]) - r / 2) * self.s
            if r >= self.rows:
                return x, self.slot_y + self.slot_h * 0.35
            return x, self.top + r * self.row_h - self.peg_r - self.ball_r

        (ax, ay), (zx, zy) = point(i - 1), point(i)
        lift = 0 if i == 0 else self.row_h * 0.42
        return ax + (zx - ax) * f, ay + (zy - ay) * f * f - lift * 4 * f * (1 - f) * (1 - f)

    def frame(self, balls: list[tuple[list[int], float]], hits: set[int] = frozenset()) -> Image.Image:
        img = self._base.copy()
        d = ImageDraw.Draw(img)
        for k in hits:
            self._draw_slot(d, k, hit=True)
        rad = self.ball_r
        for path, t in balls:
            x, y = self.ball_at(path, t)
            d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=BALL)
            d.ellipse((x - rad * 0.55, y - rad * 0.65, x - rad * 0.05, y - rad * 0.15), fill=BALL_LIGHT)
        return img


def plinko_gif(risk: str, rows: int, paths: list[list[int]]) -> tuple[bytes, float]:
    """Animated GIF of the drop (plays once). Returns (bytes, seconds until every ball has landed)."""
    board = Board(risk, rows)
    steps = 2 * (rows + 1)                                   # half-row frames per ball
    total = steps + STAGGER * (len(paths) - 1)
    frames, durations = [], []
    for n in range(total + 1):
        flying, landed = [], set()
        for i, path in enumerate(paths):
            t = (n - i * STAGGER) / 2
            if t < 0:
                continue
            if t >= rows + 1:
                landed.add(sum(path))
            else:
                flying.append((path, t))
        frames.append(board.frame(flying, landed))
        durations.append(FRAME_MS)
    durations[-1] = HOLD_MS
    palette = frames[-1].quantize(colors=96)                 # one palette for all frames: fast, no flicker
    out = io.BytesIO()
    first, *rest = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    first.save(out, format="GIF", save_all=True, append_images=rest, duration=durations, optimize=False,
               disposal=1)                                    # no loop: plays once and stays on the result
    return out.getvalue(), total * FRAME_MS / 1000


def plinko_png(risk: str, rows: int, hits: set[int] = frozenset()) -> bytes:
    out = io.BytesIO()
    Board(risk, rows).frame([], set(hits)).save(out, format="PNG", optimize=True)
    return out.getvalue()
