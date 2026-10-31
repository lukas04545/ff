"""A picture of a kit like the in-game inventory screen: GEAR (7 slots), INVENTORY (6 per row) and BELT (6), dark
slot tiles with the item pictures (the same PC pictures the kit builder uses, web/item_images.py) and "x2" amounts.

Used by the kit builder (download), the custom kits list (download) and the web store (the picture on each kit card).
Items that know their container (kit builder, in-game kits) keep it; the others (custom kits) are placed like the kit
builder does: clothing in GEAR, weapons/tools/medical in the BELT, the rest in the INVENTORY. Pillow only.
"""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from rce import items as itemlist

SIZES = {"Wear": 7, "Main": 24, "Belt": 6}
HEADS = {"Wear": "GEAR", "Main": "INVENTORY", "Belt": "BELT"}
W = 900                                     # picture width (px)
PAD, GAP = 20, 8
BG_TOP, BG_BOTTOM = (34, 40, 44), (14, 17, 19)
TILE, TILE_EDGE, TILE_EMPTY = (44, 49, 45), (24, 28, 26), (31, 35, 33)
WHITE, INK = (255, 255, 255), (0, 0, 0)
BOLD_FONTS = ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "segoeuib.ttf")
CACHE_KEEP = 300                            # rendered pictures kept on disk

_CATEGORY = {short: category for (category, _icon), items in itemlist.CATALOGUE.items() for short in items}
_fonts: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    if size not in _fonts:
        font = None
        for name in BOLD_FONTS:
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:                # old Pillow: one fixed size
                font = ImageFont.load_default()
        _fonts[size] = font
    return _fonts[size]


def _container_for(short: str, free: dict[str, int]) -> str:
    category = _CATEGORY.get(short, "")
    if category == "Attire" and free["Wear"]:
        return "Wear"
    if category in ("Weapons", "Tools", "Medical") and free["Belt"]:
        return "Belt"
    return "Main"


def layout(items: list[tuple]) -> dict[str, list[tuple[str, int] | None]]:
    """Items -> slots per container. Each item is (shortname, amount) or (shortname, amount, container) with
    container Wear / Main / Belt (any case); without one it's placed like the kit builder does. Overflow goes to the
    inventory (it grows by whole rows)."""
    slots: dict[str, list] = {c: [] for c in SIZES}
    later = []
    for entry in items:
        short, amount = str(entry[0]), int(entry[1])
        container = str(entry[2]).title() if len(entry) > 2 and entry[2] else ""
        if container in SIZES and len(slots[container]) < SIZES[container]:
            slots[container].append((short, amount))
        else:
            later.append((short, amount))
    for short, amount in later:
        free = {c: SIZES[c] - len(slots[c]) for c in SIZES}
        target = _container_for(short, free)
        slots[target if free[target] > 0 or target == "Main" else "Main"].append((short, amount))
    return slots


def cache_key(items: list[tuple], title: str, footer: str, logo_key: str = "") -> str:
    raw = "v2|" + repr([tuple(map(str, i)) for i in items]) + "|" + title + "|" + footer + "|" + logo_key
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _background(w: int, h: int, logo: bytes | None = None) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_BOTTOM)
    d = ImageDraw.Draw(img)
    for y in range(h):
        f = y / max(1, h - 1)
        d.line((0, y, w, y), fill=tuple(round(a + (b - a) * f) for a, b in zip(BG_TOP, BG_BOTTOM)))
    glow = Image.new("L", (w, h), 0)                         # soft light in the middle, darker edges
    ImageDraw.Draw(glow).ellipse((-w * 0.2, -h * 0.1, w * 1.2, h * 1.05), fill=60)
    glow = glow.filter(ImageFilter.GaussianBlur(min(w, h) // 5))
    img = Image.composite(Image.new("RGB", (w, h), (60, 70, 74)), img, glow)
    if logo:                                                 # the server logo, big and faint, behind the slots
        try:
            mark = Image.open(io.BytesIO(logo)).convert("RGBA")
        except Exception:
            return img
        side = int(min(w, h) * 0.78)
        mark.thumbnail((side, side), Image.LANCZOS)
        alpha = mark.getchannel("A").point(lambda a: int(a * 0.11))   # very faint
        mark.putalpha(alpha)
        img.paste(mark, ((w - mark.width) // 2, (h - mark.height) // 2), mark)
    return img


def _item_picture(data: bytes | None, size: int) -> Image.Image | None:
    if not data:
        return None
    try:
        pic = Image.open(io.BytesIO(data))
        pic.load()
    except Exception:                                        # a broken picture: the shortname is shown instead
        return None
    pic = pic.convert("RGBA")
    pic.thumbnail((size, size), Image.LANCZOS)
    return pic


def _text(d: ImageDraw.ImageDraw, xy, value: str, size: int, *, anchor: str, fill=WHITE, stroke: int = 3) -> None:
    d.text(xy, value, font=_font(size), fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=INK)


def _name_label(d: ImageDraw.ImageDraw, label: str, box) -> None:
    """No picture for the item: its name, on up to two lines, as big as fits the tile."""
    width = (box[2] - box[0]) * 0.86
    # a long dotted shortname (e.g. "wall.external.high.stone") has no spaces, so also break it on its dots
    words = label.replace(".", ". ").split()
    lines = []
    for size in (16, 14, 12, 11):
        font = _font(size)
        lines, line = [], ""
        for word in words:
            test = f"{line} {word}".strip()
            if d.textlength(test, font=font) <= width or not line:
                line = test
            else:
                lines.append(line)
                line = word
        lines.append(line)
        if len(lines) <= 2 and all(d.textlength(x, font=font) <= width for x in lines):
            break
    lines = lines[:2]
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2 - 8
    for n, text in enumerate(lines):
        _text(d, (cx, cy + (n - (len(lines) - 1) / 2) * (size + 3)), text, size, anchor="mm", stroke=2)


def render(items: list[tuple], pictures: dict[str, bytes | None], *, title: str = "", footer: str = "",
           logo: bytes | None = None) -> bytes:
    """The kit picture as PNG. pictures: shortname -> image bytes (None = no picture: the name is written).
    logo: the server logo, drawn big and faint in the background."""
    slots = layout(items)
    cols = {"Wear": 7, "Main": 6, "Belt": 6}
    rows = {"Wear": 1, "Main": max(1, math.ceil(len(slots["Main"]) / 6)), "Belt": 1}
    tile = {c: (W - 2 * PAD - (cols[c] - 1) * GAP) / cols[c] for c in SIZES}
    head_h = 46
    title_h = 64 if title else 6
    h = title_h + sum(head_h + rows[c] * (tile[c] + GAP) for c in SIZES) + (40 if footer else 14)
    img = _background(W, int(h), logo)
    d = ImageDraw.Draw(img)
    if title:
        _text(d, (W / 2, 36), title.upper(), 34, anchor="mm", stroke=4)
    y = title_h
    for c in ("Wear", "Main", "Belt"):
        _text(d, (PAD, y + head_h / 2 + 2), HEADS[c], 30, anchor="lm")
        y += head_h
        size = tile[c]
        for n in range(rows[c] * cols[c]):
            r, k = divmod(n, cols[c])
            x0, y0 = PAD + k * (size + GAP), y + r * (size + GAP)
            box = (round(x0), round(y0), round(x0 + size), round(y0 + size))
            entry = slots[c][n] if n < len(slots[c]) else None
            d.rectangle(box, fill=TILE if entry else TILE_EMPTY, outline=TILE_EDGE, width=3)
            d.line((box[0] + 3, box[1] + 3, box[2] - 3, box[1] + 3), fill=(58, 64, 59), width=1)   # top light
            if entry is None:
                continue
            short, amount = entry
            pic = _item_picture(pictures.get(short), int(size * 0.8))
            if pic is not None:
                img.paste(pic, (round(x0 + (size - pic.width) / 2), round(y0 + (size - pic.height) / 2 - size * 0.03)),
                          pic)
            else:
                _name_label(d, itemlist.COMMON_ITEMS.get(short, short), box)
            _text(d, (box[2] - 8, box[3] - 6), f"x{amount}", max(18, round(size * 0.19)), anchor="rs")
        y += rows[c] * (size + GAP)
    if footer:
        _text(d, (W / 2, h - 20), footer, 16, anchor="mm", fill=(190, 196, 200), stroke=2)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def kit_picture(items: list[tuple], images, cache_dir: Path, *, title: str = "", footer: str = "",
                      logo: bytes | None = None, logo_key: str = "", wait: float = 8.0) -> bytes:
    """Renders (or takes from the cache) the picture of a kit. images: the web interface's ItemImages (it downloads
    missing item pictures – up to `wait` seconds for each new one)."""
    import asyncio
    key = cache_key(items, title, footer, logo_key)
    path = cache_dir / f"{key}.png"
    if path.exists():
        return path.read_bytes()
    shorts = list(dict.fromkeys(str(i[0]) for i in items))
    found = await asyncio.gather(*(images.get(s, wait=wait) for s in shorts), return_exceptions=True)
    pictures = {s: (f[0] if isinstance(f, tuple) else None) for s, f in zip(shorts, found)}
    data = await asyncio.to_thread(render, items, pictures, title=title, footer=footer, logo=logo)
    if all(pictures.values()):                               # only cache complete pictures
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        old = sorted(cache_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for stale in old[:-CACHE_KEEP]:
            stale.unlink(missing_ok=True)
    return data
