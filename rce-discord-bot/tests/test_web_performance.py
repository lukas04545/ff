"""Website speed: small item pictures, ETag/304, gzip, panel files cached by content hash, small header logo."""
import asyncio
import io
import os
import re

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from tests.test_plugins import pb  # noqa: F401
from web.item_images import ItemImages, shrink


def png(size=180, colour=(200, 60, 20, 255)) -> bytes:
    im = Image.new("RGBA", (size, size), colour)
    for x in range(0, size, 3):                       # some detail so it isn't trivially small
        im.putpixel((x, x), (x % 255, 255 - x % 255, 90, 255))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def test_shrink_makes_a_small_webp():
    data = png(512)
    small = shrink(data, 96)
    assert small[:4] == b"RIFF" and small[8:12] == b"WEBP" and len(small) < len(data)
    assert Image.open(io.BytesIO(small)).size == (96, 96)
    assert shrink(b"not a picture", 96) is None


def test_thumbnails_are_kept_and_remade_when_the_picture_changes(tmp_path):
    images = ItemImages(tmp_path)
    (tmp_path / "zzfake.item.img").write_bytes(png(180))

    async def main():
        data, kind = await images.get("zzfake.item", 96)
        assert kind == "image/webp" and (tmp_path / "thumbs" / "zzfake.item.96.webp").read_bytes() == data
        assert (await images.get("zzfake.item"))[1] == "image/png"            # without a size: the original
        assert (await images.get("zzfake.item", 777))[1] == "image/png"       # only the allowed sizes
        (tmp_path / "zzfake.item.img").write_bytes(png(180, (10, 10, 240, 255)))
        thumb = tmp_path / "thumbs" / "zzfake.item.96.webp"
        os.utime(thumb, (1, 1))                                            # own picture put in later: newer
        new, _ = await images.get("zzfake.item", 96)
        assert new != data
    asyncio.run(main())


def test_pictures_pages_and_panel_files_are_fast(pb):  # noqa: F811
    folder = pb.bot.config.database_path.parent / "item_images"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rifle.ak.img").write_bytes(png(180))

    async def main():
        from web.server import WebInterface
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        web = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            r = await c.get("/img/items/rifle.ak.png?ph=1&s=96")
            assert r.content_type == "image/webp" and r.headers["ETag"]
            r2 = await c.get("/img/items/rifle.ak.png?ph=1&s=96", headers={"If-None-Match": r.headers["ETag"]})
            assert r2.status == 304 and await r2.read() == b""

            r = await c.get("/", headers={"Accept-Encoding": "gzip"})       # player website: gzip
            page = await r.text()
            assert r.headers.get("Content-Encoding") == "gzip" and "Accept-Encoding" in r.headers.get("Vary", "")
            assert 'width="42" height="42"' in page and "backdrop-filter" not in page
            version = re.search(r'/logo\?s=96&amp;v=(\w+)', page)[1]
            small = await c.get(f"/logo?s=96&v={version}")
            assert small.content_type == "image/webp" and "immutable" in small.headers["Cache-Control"]
            assert len(await small.read()) < len(await (await c.get("/logo")).read())
            assert "immutable" not in (await c.get("/logo?v=old")).headers["Cache-Control"]

            r = await c.get("/admin")                                        # panel: files linked by content hash
            html = await r.text()
            links = re.findall(r'/static/(app\.js|style\.css)\?v=(\w+)', html)
            assert len(links) == 2 and r.headers["Cache-Control"] == "no-cache"
            for name, v in links:
                assert len(v) == 12
                f = await c.get(f"/static/{name}?v={v}", headers={"Accept-Encoding": "gzip"})
                assert f.status == 200 and "immutable" in f.headers["Cache-Control"]
                assert f.headers.get("Content-Encoding") == "gzip"
            assert (await c.get("/static/app.js")).headers["Cache-Control"] == "no-cache"
            assert (await c.get("/static/..%2Fserver.py")).status == 404
            assert (await c.get("/static/nope.js")).status == 404
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())
