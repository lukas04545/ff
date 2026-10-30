"""Kit pictures like the in-game inventory screen (support/kit_image.py): layout, rendering, cache, the panel's
downloads (kit builder, custom and in-game kits) and the picture on the web store's kit cards."""
import asyncio
import io
from dataclasses import replace

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from support import kit_image
from tests.test_kit_requests import env  # noqa: F401
from tests.test_webshop import shop  # noqa: F401

H = {"X-Requested-With": "rce-web"}


def _png(colour=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (64, 64), colour + (255,)).save(out, "PNG")
    return out.getvalue()


class FakeImages:
    def __init__(self, missing=()):
        self.asked, self.missing = [], set(missing)

    async def get(self, shortname, size=None, *, wait=None):
        self.asked.append(shortname)
        return None if shortname in self.missing else (_png(), "image/png")


def test_layout_keeps_containers_and_places_the_rest_like_the_kit_builder():
    slots = kit_image.layout([("rifle.ak", 1), ("hoodie", 1), ("wood", 1000), ("ammo.rifle", 128, "belt"),
                              ("metal.facemask", 1, "Wear"), ("syringe.medical", 5)])
    assert slots["Wear"] == [("metal.facemask", 1), ("hoodie", 1)]           # given first, then clothing
    assert slots["Belt"] == [("ammo.rifle", 128), ("rifle.ak", 1), ("syringe.medical", 5)]
    assert slots["Main"] == [("wood", 1000)]
    many = kit_image.layout([("wood", 1)] * 30 + [("hoodie", 1)] * 9)
    assert len(many["Wear"]) == 7 and len(many["Main"]) == 32                # overflow -> inventory grows
    full_belt = kit_image.layout([("rifle.ak", 1)] * 8)
    assert len(full_belt["Belt"]) == 6 and len(full_belt["Main"]) == 2


def test_render_makes_a_png_and_the_inventory_grows_by_rows():
    pics = {"rifle.ak": _png(), "wood": _png((90, 60, 30))}
    small = Image.open(io.BytesIO(kit_image.render([("rifle.ak", 1), ("wood", 5)], pics, title="Starter")))
    big = Image.open(io.BytesIO(kit_image.render([("wood", 1)] * 19, pics, title="Starter")))
    assert small.format == "PNG" and small.width == kit_image.W and big.height > small.height   # 1 vs 4 rows
    no_pic = kit_image.render([("no.such.item", 3)], {}, title="")                              # name instead
    assert Image.open(io.BytesIO(no_pic)).format == "PNG"


def test_pictures_are_cached_only_when_complete(tmp_path):
    images = FakeImages(missing={"wood"})
    items = [("rifle.ak", 1), ("wood", 10)]
    first = asyncio.run(kit_image.kit_picture(items, images, tmp_path, title="A"))
    assert first.startswith(b"\x89PNG") and list(tmp_path.glob("*.png")) == []      # a picture was missing
    images.missing.clear()
    asyncio.run(kit_image.kit_picture(items, images, tmp_path, title="A"))
    assert len(list(tmp_path.glob("*.png"))) == 1
    asked = len(images.asked)
    asyncio.run(kit_image.kit_picture(items, images, tmp_path, title="A"))
    assert len(images.asked) == asked                                             # from the cache


def test_panel_downloads_and_store_picture(shop, monkeypatch):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    bot, db = shop.env.bot, shop.db
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])
    bot.config = replace(bot.config, web_password="secret123")

    async def main():
        from web.server import WebInterface
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        await bot.plugins.set_enabled("website", True)
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        monkeypatch.setattr(web.item_images, "get", FakeImages().get)
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            body = {"name": "Raid", "slots": [{"item": "rifle.ak", "amount": 2, "container": "Belt"},
                                              {"item": "metal.facemask", "amount": 1, "container": "Wear"}]}
            assert (await c.post("/api/kit-image", json=body, headers=H)).status == 401          # login first
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post("/api/kit-image", json=body, headers=H)
            assert r.status == 200 and r.content_type == "image/png"
            assert 'filename="Raid.png"' in r.headers["Content-Disposition"]
            assert (await r.read()).startswith(b"\x89PNG")
            r = await c.post("/api/kit-image", json={"name": "x", "slots": []}, headers=H)
            assert r.status == 400 and "empty" in (await r.json())["error"]
            r = await c.get("/api/custom-kits/Ak Kit/image.png")
            assert r.status == 200 and r.content_type == "image/png"
            assert (await c.get("/api/custom-kits/Nope/image.png")).status == 404
            # the web store: a picture on the kit's card, public, only for kits that are for sale
            page = await (await c.get("/store")).text()
            assert f'/img/kits/{shop.kit_id}.png?v=' in page and "Download picture" in page
            c.session.cookie_jar.clear()
            r = await c.get(f"/img/kits/{shop.kit_id}.png", headers={"CF-Connecting-IP": "203.0.113.9"})
            assert r.status == 200 and r.content_type == "image/png" and "max-age" in r.headers["Cache-Control"]
            assert "Content-Disposition" not in r.headers
            r = await c.get(f"/img/kits/{shop.kit_id}.png?dl=1", headers={"CF-Connecting-IP": "203.0.113.9"})
            assert "attachment" in r.headers["Content-Disposition"]
            db.update_store_kit(shop.kit_id, enabled=0)
            assert (await c.get(f"/img/kits/{shop.kit_id}.png")).status == 404
            assert (await c.get("/img/kits/99999.png")).status == 404
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_every_catalogue_item_has_a_bundled_picture():
    from web.item_images import BUNDLED_DIR
    from rce import items as itemlist
    missing = [s for s in itemlist.COMMON_ITEMS
               if not (BUNDLED_DIR / (s.replace(" ", "_") + ".webp")).exists()]
    assert missing == [], f"{len(missing)} items without a bundled picture: {missing[:10]}"
    assert len(list(BUNDLED_DIR.glob("*.webp"))) >= len(itemlist.COMMON_ITEMS)


def test_bundled_pictures_are_used_without_downloading(tmp_path, monkeypatch):
    from web.item_images import ItemImages
    images = ItemImages(tmp_path)                       # empty data dir
    monkeypatch.setattr(images, "_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded!")))
    got = asyncio.run(images.get("rifle.ak"))
    assert got and got[0][:4] == b"RIFF" and got[1] == "image/webp"        # the bundled WebP, no download


def test_logo_changes_the_picture_and_the_cache_key():
    from rce import items as itemlist
    logo = io.BytesIO()
    Image.new("RGBA", (200, 200), (247, 90, 30, 255)).save(logo, "PNG")
    logo = logo.getvalue()
    items = [("rifle.ak", 1)]
    pics = {"rifle.ak": _png()}
    with_logo = kit_image.render(items, pics, title="K", logo=logo)
    without = kit_image.render(items, pics, title="K")
    assert with_logo != without and Image.open(io.BytesIO(with_logo)).format == "PNG"
    assert kit_image.cache_key(items, "K", "", "v1") != kit_image.cache_key(items, "K", "", "v2")
