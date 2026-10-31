"""Shop builder offers every staff-kit-builder item (default price), and item pictures: fallbacks, variants, scan."""
import asyncio
from dataclasses import replace

import pytest

from rce import items as itemlist
from tests.test_kit_requests import env  # noqa: F401
from tests.test_webshop import shop  # noqa: F401  (fixture: WebShop cog with a price list)
from web import item_images


def run(coro):
    return asyncio.run(coro)


def test_every_catalogue_item_is_buyable_with_a_default_price(shop):
    from cogs.webshop import CheckoutError
    s = shop.s
    unpriced = "ammo.grenadelauncher.he"                       # in the catalogue, no rule in the test price list
    assert unpriced in itemlist.COMMON_ITEMS and unpriced in s.unpriced_items()
    with pytest.raises(CheckoutError, match="can't be bought"):
        s.price_custom({unpriced: "10"})
    shop.env.bot.config = replace(shop.env.bot.config, store_default_item_price=0.05)
    assert s.unpriced_items() == []                            # everything has a price now
    q, picked = s.price_custom({unpriced: "10", "rifle.ak": "1"})
    assert picked == [{"short": unpriced, "amount": 10}, {"short": "rifle.ak", "amount": 1}]
    assert q.total == 5.5                                      # 10 × 0.05 + the AK from the price list (5)
    # Items that are free extras of a rule keep being free and don't get a default price
    assert "metal.facemask" not in {r for rule in s.effective_price_list().rules[28:] for r in rule.items}


def test_picker_shows_all_items(shop):
    from web.store_pages import picker_groups
    shop.env.bot.config = replace(shop.env.bot.config, store_default_item_price=0)
    html = picker_groups(shop.s, {})
    assert "No price yet" in html and 'disabled placeholder="–"' in html        # greyed out, not hidden
    assert "ammo.grenadelauncher.he" in html and 'name="q.rifle.ak"' in html
    shop.env.bot.config = replace(shop.env.bot.config, store_default_item_price=0.1)
    html = picker_groups(shop.s, {"ammo.grenadelauncher.he": "7"})
    assert "No price yet" not in html and "per item" in html
    assert 'name="q.ammo.grenadelauncher.he"' in html and 'value="7"' in html
    # every catalogue item has an input somewhere
    assert all(f'name="q.{short}"' in html for short in itemlist.COMMON_ITEMS)


def test_name_variants():
    assert item_images.name_variants("rifle.ak") == ["rifle.ak", "rifle_ak"]
    v = item_images.name_variants("lumberjack hoodie")
    assert v[0] == "lumberjack hoodie" and "lumberjack.hoodie" in v and "lumberjack_hoodie" in v
    assert len(item_images.name_variants("a b_c.d")) <= 4



def _real_png(colour=(200, 40, 40, 255)) -> bytes:
    import io as _io
    from PIL import Image as _Image
    out = _io.BytesIO()
    _Image.new("RGBA", (8, 8), colour).save(out, "PNG")
    return out.getvalue()

PNG = _real_png()


def test_pictures_use_fallback_sources_and_variants(tmp_path, monkeypatch):
    imgs = item_images.ItemImages(tmp_path, "https://primary.example/{shortname}.png")
    tried = []

    async def fake_fetch(self, url):
        tried.append(url)
        if url == "https://cdn.rusthelp.com/images/public/hv.rifle.png":      # only a fallback + variant has it
            return PNG
        return b""
    monkeypatch.setattr(item_images.ItemImages, "_fetch", fake_fetch)
    data, kind = run(imgs.get("hv rifle"))
    assert data == PNG and kind == "image/png"
    assert tried[0] == "https://primary.example/hv%20rifle.png"                # configured source first
    assert any("rusthelp" in u for u in tried)
    assert imgs.cached("hv rifle") == PNG                                      # saved
    before = len(tried)
    run(imgs.get("hv rifle"))
    assert len(tried) == before                                                # cached: no more requests


def test_scan_reports_found_and_missing(tmp_path, monkeypatch):
    imgs = item_images.ItemImages(tmp_path, "https://primary.example/{shortname}.png")

    async def fake_fetch(self, url):
        return PNG if "rifle.ak" in url else b""
    monkeypatch.setattr(item_images.ItemImages, "_fetch", fake_fetch)
    progress = []
    result = run(imgs.scan(["rifle.ak", "nothing.here"], progress=lambda d, t: progress.append((d, t))))
    assert result == {"found": ["rifle.ak"], "missing": ["nothing.here"]}
    assert progress[-1] == (2, 2)
    # A scan retries items that were marked "no picture"; the rate limit is restored afterwards
    assert imgs._downloads.hit("all") is None


def test_scan_api_is_owner_only(shop, monkeypatch, tmp_path):
    """POST starts a background scan (owner), GET reports progress."""
    from aiohttp.test_utils import TestClient, TestServer

    from web.server import WebInterface
    shop.env.bot.config = replace(shop.env.bot.config, item_image_url="https://primary.example/{shortname}.png",
                                  web_password="secret123")

    async def fake_scan(self, shortnames, *, retry=True, progress=None):
        shortnames = list(shortnames)
        if progress:
            progress(len(shortnames), len(shortnames))
        return {"found": shortnames[:-1], "missing": shortnames[-1:]}
    monkeypatch.setattr(item_images.ItemImages, "scan", fake_scan)
    H = {"X-Requested-With": "rce-web"}

    async def main():
        web = WebInterface(shop.env.bot, env_path=tmp_path / ".env", secret_path=tmp_path / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.post("/api/item-images/scan", json={}, headers=H)).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            first = await (await c.get("/api/item-images/scan")).json()
            assert first["running"] is False and first["never"] is True
            r = await c.post("/api/item-images/scan", json={}, headers=H)
            assert r.status == 200
            await web._scan_task
            state = await (await c.get("/api/item-images/scan")).json()
            assert state["running"] is False and len(state["missing"]) == 1
            assert state["found"] == len(itemlist.COMMON_ITEMS) - 1
        finally:
            await c.close()
    run(main())
