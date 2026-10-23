"""Pictures of in-game items for the kit builder and the player website.

The bot downloads an item's picture once (ITEM_IMAGE_URL, default RustLabs) and keeps it in data/item_images/.
Browsers then load it from the bot (/img/items/<shortname>.png) – no third-party requests from the panel, and it
keeps working if the source is down. Items without a picture are remembered for a day (no repeated downloads);
the web interface then shows the category emoji instead.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

from web.ratelimit import RateLimiter

log = logging.getLogger("rce.items.images")

try:                                    # optional: without Pillow the original pictures are sent
    from PIL import Image
except ImportError:                     # pragma: no cover
    Image = None

DEFAULT_URL = "https://rustlabs.com/img/items180/{shortname}.png"
# Tried after the configured source when it has no picture for an item (best effort – a source that is down or
# doesn't know the item is simply skipped). Your own pictures always win: put <shortname>.img in data/item_images/.
FALLBACK_URLS = (
    "https://cdn.rusthelp.com/images/public/{shortname}.png",
    "https://files.facepunch.com/rust/item/{shortname}_512.png",
)
SHORTNAME = re.compile(r"^[a-z0-9][a-z0-9._ -]{0,63}$")
MAX_BYTES = 512 * 1024
MISSING_RETRY = 86400           # seconds before an item without a picture (every source said 404) is tried again
SOURCE_TROUBLE_RETRY = 300      # a source refused/failed (403, 429, 5xx, timeout): try again after 5 minutes
WAIT_FOR_DOWNLOAD = 4.0         # a page request waits this long for a new picture, then gets the placeholder
DOWNLOADS_PER_MINUTE = 60       # new downloads from the source, all visitors together
THUMB_SIZES = (96,)              # sizes a page may ask for (?s=96): small WebP copies, many times smaller
MAX_PIXELS = 16_000_000         # bigger pictures aren't shrunk (decompression bombs)
MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"RIFF", "image/webp"),
         (b"GIF8", "image/gif"))


def name_variants(shortname: str) -> list[str]:
    """Spellings of a shortname to try: console names sometimes use spaces/underscores where PC uses dots."""
    out = [shortname]
    for variant in (shortname.replace(" ", "."), shortname.replace(" ", "_"), shortname.replace("_", "."),
                    shortname.replace(".", "_"), shortname.replace(" ", "").replace("_", "")):
        if variant not in out and SHORTNAME.match(variant):
            out.append(variant)
    return out[:4]


def image_type(data: bytes) -> str | None:
    for magic, kind in MAGIC:
        if data.startswith(magic) and (kind != "image/webp" or data[8:12] == b"WEBP"):
            return kind
    return None


def shrink(data: bytes, size: int) -> bytes | None:
    """A WebP copy of a picture that fits in size × size, or None (no Pillow, unreadable, or not smaller)."""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                return None
            im = im.convert("RGBA")
            im.thumbnail((size, size), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=82, method=4)
    except Exception as exc:            # broken/unsupported file: send the original
        log.debug("Could not shrink a picture: %s", exc)
        return None
    small = out.getvalue()
    return small if len(small) < len(data) else None


class SourceTrouble(Exception):
    """A picture source refused (403, 429 …), failed (5xx) or timed out – not "no picture"."""


class ItemImages:
    def __init__(self, folder: Path, url_template: str = DEFAULT_URL) -> None:
        self.folder = Path(folder)
        self.url_template = url_template or DEFAULT_URL
        self._missing: dict[str, float] = {}
        self._parallel = asyncio.Semaphore(4)
        self._session: aiohttp.ClientSession | None = None
        self._downloads = RateLimiter(DOWNLOADS_PER_MINUTE, 60)   # random names can't make the bot spam the source
        self._jobs: dict[str, asyncio.Task] = {}                    # downloads running in the background
        self.sources: dict[str, dict] = {}                          # per source host: ok / last error (diagnosis)

    def _path(self, shortname: str) -> Path:
        return self.folder / (shortname.replace(" ", "_") + ".img")

    def cached(self, shortname: str) -> bytes | None:
        try:
            return self._path(shortname).read_bytes()
        except OSError:
            return None

    def known_missing(self, shortname: str) -> bool:
        """True if every source recently said there's no picture (not just a hiccup)."""
        return time.time() - self._missing.get((shortname or "").strip().lower(), 0) < MISSING_RETRY

    async def get(self, shortname: str, size: int | None = None, *, wait: float | None = None
                  ) -> tuple[bytes, str] | None:
        """(image bytes, content type) or None if there is no picture (yet) for this item.
        size: one of THUMB_SIZES for a small WebP copy (made once, kept in data/item_images/thumbs/).
        wait: seconds to wait for a new download – it goes on in the background, the next request gets it."""
        shortname = (shortname or "").strip().lower()
        if not SHORTNAME.match(shortname):
            return None
        data = self.cached(shortname)
        if data is None:
            if self.known_missing(shortname):
                return None
            job = self._jobs.get(shortname)
            if job is None:
                job = self._jobs[shortname] = asyncio.create_task(self._download_once(shortname))
            try:
                data = await (asyncio.wait_for(asyncio.shield(job), wait) if wait is not None else asyncio.shield(job))
            except asyncio.TimeoutError:
                return None
        kind = image_type(data) if data else None
        if not kind:
            return None
        if size in THUMB_SIZES:
            small = await self._thumb(shortname, data, size)
            if small:
                return small, "image/webp"
        return data, kind

    async def _thumb(self, shortname: str, data: bytes, size: int) -> bytes | None:
        """The small copy from disk (remade when the original is newer), else made now. An empty file means
        "the original is already small" – then None."""
        if Image is None:
            return None
        original = self._path(shortname)
        path = self.folder / "thumbs" / f"{original.stem}.{size}.webp"
        try:
            if path.stat().st_mtime >= original.stat().st_mtime:
                return path.read_bytes() or None
        except OSError:
            pass
        small = await asyncio.get_running_loop().run_in_executor(None, shrink, data, size)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(small or b"")
            tmp.replace(path)
        except OSError as exc:
            log.debug("Could not save the small picture of %s: %s", shortname, exc)
        return small

    async def _download_once(self, shortname: str) -> bytes | None:
        try:
            data = self.cached(shortname)
            return data if data is not None else await self._download(shortname)
        finally:
            self._jobs.pop(shortname, None)

    def _sources(self) -> list[str]:
        return [self.url_template, *(u for u in FALLBACK_URLS if u != self.url_template)]

    async def _fetch(self, url: str) -> bytes:
        """The picture, b"" if the source has none (404). Raises SourceTrouble if the source refused or failed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                                  headers={"User-Agent": "rce-discord-bot (item pictures)",
                                                           "Accept": "image/*"})
        host = urlparse(url).netloc
        stats = self.sources.setdefault(host, {"ok": 0, "not_found": 0, "errors": 0, "last_error": ""})
        try:
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    stats["ok"] += 1
                    return await resp.content.read(MAX_BYTES + 1)
                if resp.status in (404, 410):
                    stats["not_found"] += 1
                    return b""
                stats["errors"] += 1
                stats["last_error"] = f"HTTP {resp.status}"
                raise SourceTrouble(f"{host}: HTTP {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            stats["errors"] += 1
            stats["last_error"] = type(exc).__name__ if not str(exc) else str(exc)[:80]
            raise SourceTrouble(f"{host}: {stats['last_error']}") from exc

    async def _download(self, shortname: str) -> bytes | None:
        if self._downloads.hit("all") is not None:
            return None                     # too many new pictures right now – the next request tries again
        data, network_error = b"", False
        async with self._parallel:
            for template in self._sources():
                for variant in name_variants(shortname):
                    url = template.replace("{shortname}", quote(variant))
                    try:
                        data = await self._fetch(url)
                    except (SourceTrouble, ValueError) as exc:
                        log.debug("Item picture %s (%s): %s", shortname, url, exc)
                        network_error = True
                        data = b""
                        break               # this source refused or is down – try the next one
                    if data and len(data) <= MAX_BYTES and image_type(data) is not None:
                        break
                    data = b""
                if data:
                    break
        if not data and network_error:
            self._missing[shortname] = time.time() - MISSING_RETRY + SOURCE_TROUBLE_RETRY   # retry soon
            return None
        if not data or len(data) > MAX_BYTES or image_type(data) is None:
            self._missing[shortname] = time.time()
            return None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self._path(shortname).with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(self._path(shortname))
        except OSError as exc:
            log.warning("Could not save the picture of %s: %s", shortname, exc)
        return data

    async def scan(self, shortnames, *, retry: bool = True, progress=None) -> dict[str, list[str]]:
        """Fetches the pictures of all given items (4 at a time). Returns {"found": [...], "missing": [...]}.
        retry: also tries items that were marked "no picture" recently. progress(done, total) is called as it goes."""
        shortnames = list(shortnames)
        found: list[str] = []
        missing: list[str] = []
        limiter, self._downloads = self._downloads, RateLimiter(10 ** 9, 60)    # a scan is meant to fetch many
        try:
            async def one(short: str) -> None:
                if retry:
                    self._missing.pop(short, None)
                if self.cached(short) is not None or await self.get(short) is not None:
                    found.append(short)
                else:
                    missing.append(short)
                if progress is not None:
                    progress(len(found) + len(missing), len(shortnames))
            await asyncio.gather(*(one(s) for s in shortnames))
        finally:
            self._downloads = limiter
        return {"found": sorted(found), "missing": sorted(missing)}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
