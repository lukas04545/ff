"""The control panel installs as an app (PWA): manifest, icons and service worker under the panel's secret address."""
import io

from PIL import Image

from tests.test_web import H, bot, run  # noqa: F401


def test_panel_is_an_installable_app(bot):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        base = (await (await c.get("/api/me")).json())["panel_path"]
        page = await (await c.get(base)).text()
        assert f'<link rel="manifest" href="{base}/manifest.webmanifest">' in page
        r = await c.get(f"{base}/manifest.webmanifest")
        m = await r.json(content_type=None)
        assert r.headers["Content-Type"].startswith("application/manifest+json")
        assert m["start_url"] == f"{base}/" and m["scope"] == f"{base}/" and m["display"] == "standalone"
        assert [i["sizes"] for i in m["icons"]] == ["192x192", "512x512"]
        icon = await (await c.get(f"{base}/app-icon-512.png")).read()
        assert Image.open(io.BytesIO(icon)).size == (512, 512)
        sw = await (await c.get(f"{base}/sw.js")).text()
        assert "No connection" in sw and "caches" not in sw                    # nothing of the panel is cached
        assert (await c.get(f"{base}/secret.txt")).status == 404
        c.session.cookie_jar.clear()
        assert (await c.get("/manifest.webmanifest")).status == 404            # only under the secret address
    run(bot, scenario)


def test_android_app_download(bot):  # noqa: F811
    from web.server import ANDROID_APK
    assert ANDROID_APK.is_file() and ANDROID_APK.read_bytes()[:2] == b"PK"               # a real APK (zip)

    async def scenario(c):
        r = await c.get("/app/rce-panel.apk")
        assert r.status == 200 and r.headers["Content-Type"] == "application/vnd.android.package-archive"
        assert "rce-panel.apk" in r.headers["Content-Disposition"]
        assert (await r.read())[:2] == b"PK"
    run(bot, scenario)
