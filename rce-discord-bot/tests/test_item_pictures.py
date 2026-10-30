"""Item pictures: a refusing source (403) isn't "no picture" for a day, slow downloads go on in the background,
placeholders are never cached, and the per-source report shows what happened."""
import asyncio
import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from tests.test_plugins import pb  # noqa: F401
from web import item_images as ii


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (120, 120), (200, 80, 20, 255)).save(out, "PNG")
    return out.getvalue()


async def source(slow: float = 0.0):
    app = web.Application()

    async def ok(request):
        await asyncio.sleep(slow)
        if request.match_info["name"] == "nopic.png":
            raise web.HTTPNotFound()
        return web.Response(body=png(), content_type="image/png")

    async def refuse(request):
        raise web.HTTPForbidden()

    async def gone(request):
        raise web.HTTPNotFound()
    app.router.add_get("/ok/{name}", ok)
    app.router.add_get("/refuse/{name}", refuse)
    app.router.add_get("/gone/{name}", gone)
    srv = TestServer(app)
    await srv.start_server()
    return srv


def test_refusing_source_is_not_no_picture(tmp_path, monkeypatch):
    async def main():
        srv = await source()
        base = str(srv.make_url("/"))
        monkeypatch.setattr(ii, "FALLBACK_URLS", (base + "gone/{shortname}.png",))
        images = ii.ItemImages(tmp_path, base + "refuse/{shortname}.png")
        try:
            assert await images.get("zzfake.item") is None
            host = f"{srv.host}:{srv.port}"
            assert images.sources[host]["errors"] >= 1 and images.sources[host]["last_error"] == "HTTP 403"
            # refused -> retried after a few minutes, not after a day
            assert images.known_missing("zzfake.item")
            assert images._missing["zzfake.item"] < ii.time.time() - ii.MISSING_RETRY + ii.SOURCE_TROUBLE_RETRY + 5
            images.url_template = base + "ok/{shortname}.png"
            images._missing.clear()
            data, kind = await images.get("zzfake.item")
            assert kind == "image/png" and images.cached("zzfake.item") == data
            assert await images.get("nopic") is None and images.known_missing("nopic")     # really no picture
        finally:
            await images.close()
            await srv.close()
    asyncio.run(main())


def test_slow_download_continues_in_background(tmp_path):
    async def main():
        srv = await source(slow=0.5)
        images = ii.ItemImages(tmp_path, str(srv.make_url("/")) + "ok/{shortname}.png")
        try:
            assert await images.get("zzfake.slow", wait=0.05) is None                  # the page doesn't wait long …
            assert "zzfake.slow" in images._jobs
            await asyncio.sleep(0.8)
            assert images.cached("zzfake.slow") is not None                            # … the next request has it
            assert (await images.get("zzfake.slow", wait=0.05))[1] == "image/png"
        finally:
            await images.close()
            await srv.close()
    asyncio.run(main())


def test_placeholder_is_never_cached(pb):  # noqa: F811
    from aiohttp.test_utils import TestClient
    from web.server import WebInterface

    async def main():
        web_iface = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "k")
        web_iface.item_images._missing["definitely.unknown"] = ii.time.time()            # known: no picture
        c = TestClient(TestServer(web_iface.app))
        await c.start_server()
        try:
            r = await c.get("/img/items/definitely.unknown.png?ph=1")
            assert r.status == 200 and r.content_type == "image/svg+xml" and r.headers["Cache-Control"] == "no-store"
            r = await c.get("/img/items/definitely.unknown.png")
            assert r.status == 404 and r.headers["Cache-Control"] == "no-store"
        finally:
            await c.close()
            await pb.bot.rcon.close()
    asyncio.run(main())
