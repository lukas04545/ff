"""Stream overlay for TikTok LIVE Studio: the page, its live data, escaping and the visitor limit."""
import json

from tests.test_web import bot, run  # noqa: F401


def test_overlay_page_and_live_data(bot):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager

    async def scenario(c):
        ov = bot.plugins.running("overlay")
        assert ov is not None                                            # on by default
        await bot.plugins.save_settings("overlay", {"title": "420 <OFFICIAL>", "tags": "3X GATHER | 4.5K MAP",
                                                    "accent": "red;}body{x", "bar_colour": "#112233"})
        r = await c.get("/p/overlay/")
        page = await r.text()
        assert r.status == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
        assert "420 &lt;OFFICIAL&gt;" in page and "<OFFICIAL>" not in page
        assert "--accent:#e5341c" in page and "--bar:#112233" in page            # a bad colour is ignored
        assert 'src="overlay.js?v=' in page and "overlay.css?v=" in page
        init = json.loads(page.split('data-init="')[1].split('"')[0].replace("&quot;", '"').replace("&lt;", "<")
                          .replace("&gt;", ">").replace("&amp;", "&"))
        assert init["name"] == "420 <OFFICIAL>" and init["tags"] == "3X GATHER | 4.5K MAP" and init["online"] is False
        for name, kind in (("overlay.js", "javascript"), ("overlay.css", "css")):
            r = await c.get(f"/p/overlay/{name}")
            assert r.status == 200 and kind in r.headers["Content-Type"]
        bot.server_info = {"Players": 15, "MaxPlayers": 100, "Queued": 2}
        mp.setattr(type(bot.rcon), "connected", property(lambda self: True))       # restored by mp.undo()
        d = await (await c.get("/p/overlay/data.json")).json()
        assert d["online"] and d["players"] == 15 and d["max"] == 100 and d["queued"] == 2
        # the overlay asks every 15 s all stream long: never blocked by the website's visitor limit
        codes = [(await c.get("/p/overlay/data.json")).status for _ in range(80)]
        assert set(codes) == {200}
        assert (await c.get("/p/overlay/art")).status == 404                    # only the bar, no picture
        # the look: every setting reaches the page with each refresh; bad values fall back, never into the CSS
        await bot.plugins.save_settings("overlay", {
            "name_colour": "#00ff00", "tags_colour": "url(x)", "font": "mono", "shape": "pill", "icon": "logo",
            "edge_width": 12, "bar_opacity": 40, "label": "SURVIVORS", "messages": "discord.gg/abc\n\n  Search 420  \n",
            "rotate_seconds": 5, "show_progress": True, "show_dot": False, "uppercase": False})
        look = (await (await c.get("/p/overlay/data.json")).json())["look"]
        assert look["colours"]["--name"] == "#00ff00" and look["colours"]["--tags"] == "#c9cbbd"
        assert look["font"] == "mono" and look["shape"] == "pill" and look["edge"] == 12 and look["opacity"] == 40
        assert look["label"] == "SURVIVORS" and look["messages"] == ["discord.gg/abc", "Search 420"] and look["rotate"] == 5
        assert look["show"]["progress"] and not look["show"]["dot"] and look["upper"] is False
        assert look["icon"] == "star" and look["logo"] == ""          # no logo anywhere: the star instead
        assert (await c.get("/p/overlay/logo")).status == 404
        await bot.plugins.save_settings("overlay", {"font": "comic", "shape": "x", "icon": "nope"})
        look = (await (await c.get("/p/overlay/data.json")).json())["look"]
        assert (look["font"], look["shape"]) == ("mono", "pill")             # refused: the last good value stays
        await bot.plugins.set_enabled("website", True)                   # its logo can be used
        await bot.plugins.save_settings("overlay", {"icon": "logo"})
        look = (await (await c.get("/p/overlay/data.json")).json())["look"]
        r = await c.get("/p/overlay/" + look["logo"])
        assert look["icon"] == "logo" and r.status == 200 and r.headers["Content-Type"].startswith("image/")
        assert "/p/overlay/" in ov.status()["Overlay address"] or "WEB_PUBLIC_URL" in ov.status()["Overlay address"]

    async def setup():
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
    import asyncio
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(Plugin, "_loops", lambda self: [])
    asyncio.run(setup())
    try:
        run(bot, scenario)
    finally:
        mp.undo()
