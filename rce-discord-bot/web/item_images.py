"""Pictures of in-game items for the kit builder and the player website.

The bot downloads an item's picture once (ITEM_IMAGE_URL, default RustLabs) and keeps it in data/item_images/.
Browsers then load it from the bot (/img/items/<shortname>.png) – no third-party requests from the panel, and it
keeps working if the source is down. Items without a picture are remembered for a day (no repeated downloads);
the web interface then shows the category emoji instead.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger("rce.items.images")

DEFAULT_URL = "https://rustlabs.com/img/items180/{shortname}.png"
SHORTNAME = re.compile(r"^[a-z0-9][a-z0-9._ -]{0,63}$")
MAX_BYTES = 512 * 1024
MISSING_RETRY = 86400           # seconds before an item without a picture is tried again
MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"RIFF", "image/webp"),
         (b"GIF8", "image/gif"))


def image_type(data: bytes) -> str | None:
    for magic, kind in MAGIC:
        if data.startswith(magic) and (kind != "image/webp" or data[8:12] == b"WEBP"):
            return kind
    return None


class ItemImages:
    def __init__(self, folder: Path, url_template: str = DEFAULT_URL) -> None:
        self.folder = Path(folder)
        self.url_template = url_template or DEFAULT_URL
        self._missing: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._parallel = asyncio.Semaphore(4)
        self._session: aiohttp.ClientSession | None = None

    def _path(self, shortname: str) -> Path:
        return self.folder / (shortname.replace(" ", "_") + ".img")

    def cached(self, shortname: str) -> bytes | None:
        try:
            return self._path(shortname).read_bytes()
        except OSError:
            return None

    async def get(self, shortname: str) -> tuple[bytes, str] | None:
        """(image bytes, content type) or None if there is no picture for this item."""
        shortname = (shortname or "").strip().lower()
        if not SHORTNAME.match(shortname):
            return None
        data = self.cached(shortname)
        if data is None:
            if time.time() - self._missing.get(shortname, 0) < MISSING_RETRY:
                return None
            lock = self._locks.setdefault(shortname, asyncio.Lock())
            async with lock:
                data = self.cached(shortname)
                if data is None:
                    data = await self._download(shortname)
            self._locks.pop(shortname, None)
        kind = image_type(data) if data else None
        return (data, kind) if kind else None

    async def _download(self, shortname: str) -> bytes | None:
        url = self.url_template.replace("{shortname}", quote(shortname))
        try:
            async with self._parallel:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                                          headers={"User-Agent": "rce-discord-bot"})
                async with self._session.get(url) as resp:
                    data = await resp.content.read(MAX_BYTES + 1) if resp.status == 200 else b""
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("Item picture %s: %s", shortname, exc)
            self._missing[shortname] = time.time() - MISSING_RETRY + 300     # network problem: retry in 5 min
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

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
