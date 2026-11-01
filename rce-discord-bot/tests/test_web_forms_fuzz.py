"""Every form and link of the public website gets hostile input (and hostile names in the database): nothing may come
back as live HTML (XSS), nothing may crash, and forms from other sites are refused (CSRF)."""
import asyncio
import re
from dataclasses import replace
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlparse

from aiohttp.test_utils import TestClient, TestServer

from tests.test_webshop import shop  # noqa: F401
from tests.test_kit_requests import env  # noqa: F401

PROBE = "\"'><xss-probe x=1>`${7*7}</textarea></script>"
RAW = "<xss-probe"                                    # must never come back unescaped
OUT = {"CF-Connecting-IP": "203.0.113.50"}


async def _text(r) -> str:
    ctype = r.headers.get("Content-Type", "")
    return (await r.read()).decode("utf-8", "replace") if ("text" in ctype or "json" in ctype) else ""


def _links(page: str, base: str) -> set[str]:
    out = set()
    for href in re.findall(r'href="([^"]+)"', page):
        url = urljoin(base, unescape(href))
        p = urlparse(url)
        if not p.netloc and p.path.startswith("/") and not p.path.startswith(("/static/", "/img/", "/api/")):
            out.add(p.path + (f"?{p.query}" if p.query else ""))
    return out


def _forms(page: str, base: str) -> list[tuple[str, str, list[str]]]:
    forms = []
    for attrs, body in re.findall(r"<form([^>]*)>(.*?)</form>", page, re.S | re.I):
        action = re.search(r'action="([^"]*)"', attrs)
        method = re.search(r'method="([^"]*)"', attrs, re.I)
        names = re.findall(r'name="([^"]+)"', body)
        forms.append((urljoin(base, unescape(action[1])) if action else base,
                      (method[1] if method else "get").lower(), names))
    return forms


def test_public_forms_and_pages_resist_xss_and_csrf(shop, monkeypatch):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    bot, db = shop.env.bot, shop.db
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])
    # hostile names everywhere a visitor can get text into: in-game names, kits, shop items
    for name in (PROBE, "Normal Player"):
        db.record_kill(name, "Victim" + PROBE)
        db.add_playtime([name], 3600)
    db.link_player(99, PROBE)
    db.add_store_kit("Kit" + PROBE, "kit" + PROBE, 4.99, 0, "desc" + PROBE)
    db.team_event("create", PROBE, 77)

    import time
    from cogs.player_accounts import PlayerAccounts, _hash
    monkeypatch.setattr(PlayerAccounts, "cog_load", lambda self: asyncio.sleep(0))
    bot.config = replace(bot.config, player_accounts=True, economy_enabled=True)
    db.add_player_session(_hash("probe-session"), 99, PROBE, int(time.time()) + 3600)    # logged in as that name
    db.add_points(99, 5000, "test")

    async def main():
        from web.server import WebInterface
        await bot.add_cog(PlayerAccounts(bot))
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        for pid in ("website", "casino", "battlepass", "streamers"):
            if pid in bot.plugins.infos:
                await bot.plugins.set_enabled(pid, True)
        tmp = bot.config.database_path.parent
        w = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        w._public_pages.limit = w._public_total.limit = 10 ** 6         # the crawl isn't a flood
        w.shield_on = False                                                # nor for the flood shield
        c = TestClient(TestServer(w.app))
        await c.start_server()
        seen, queue, forms, problems = set(), ["/", "/store", "/leaderboard", "/admin", "/account"], [], []

        def redirect_ok(r, where):
            loc = r.headers.get("Location")
            if loc and (loc.startswith(("//", "/\\")) or (urlparse(loc).netloc and not urlparse(loc).netloc.endswith(
                    ("paypal.com",)))):
                problems.append(f"open redirect {where} -> {loc}")

        def check(where, status, text):
            if RAW in text:
                i = text.index(RAW)
                problems.append(f"XSS {where}: …{text[max(0, i - 80):i + 40]!r}")
            if status >= 500:
                problems.append(f"{status} {where}")
        async def crawl():
            while queue and len(seen) < 300:
                path = queue.pop()
                if path in seen:
                    continue
                seen.add(path)
                r = await c.get(path, headers=OUT, allow_redirects=False)
                text = await _text(r)
                check(f"GET {path}", r.status, text)
                redirect_ok(r, path)
                queue.extend(_links(text, path) - seen)
                forms.extend((a, m, n, path) for a, m, n in _forms(text, path))
                # the same page with the probe in every query parameter and in the path
                q = parse_qsl(urlparse(path).query)
                if q:
                    probe_url = urlparse(path).path + "?" + "&".join(f"{k}={PROBE}" for k, _ in q)
                    r = await c.get(probe_url, headers=OUT, allow_redirects=False)
                    check(f"GET {probe_url}", r.status, await _text(r))
                    redirect_ok(r, probe_url)
                r = await c.get(path.split("?")[0].rstrip("/") + "/" + PROBE, headers=OUT, allow_redirects=False)
                check(f"GET {path}/<probe>", r.status, await _text(r))

        try:
            await crawl()                                               # logged out: login forms
            c.session.cookie_jar.update_cookies({"rce_player": "probe-session"})
            first = set(seen)
            seen.clear()
            queue.extend(["/", "/account", "/casino", "/battlepass", "/streams"])
            await crawl()                                               # logged in as the hostile name
            done = set()
            for action, method, names, page in forms:
                key = (urlparse(action).path, method, tuple(sorted(names)))
                if key in done:
                    continue
                done.add(key)
                data = {n: PROBE for n in names} or {"x": PROBE}
                if method == "post" and urlparse(action).path.rstrip("/") == "/admin":
                    r = await c.post("/admin", data=data, headers=OUT)          # the honeypot: logs from anywhere
                elif method == "post":
                    r = await c.post(urlparse(action).path, data=data, headers={**OUT, "Origin": "http://evil.example"},
                                     allow_redirects=False)
                    if r.status != 403:
                        problems.append(f"CSRF: POST {action} from another site answered {r.status}")
                    r = await c.post(urlparse(action).path, data=data, headers={**OUT, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
                                     allow_redirects=False)
                    if r.status != 403:
                        problems.append(f"CSRF: POST {action} with Origin null answered {r.status}")
                    r = await c.post(urlparse(action).path, data=data,
                                     headers={**OUT, "Sec-Fetch-Site": "cross-site"}, allow_redirects=False)
                    if r.status != 403:
                        problems.append(f"CSRF: POST {action} with Sec-Fetch-Site cross-site answered {r.status}")
                    r = await c.post(urlparse(action).path, data=data, headers=OUT, allow_redirects=False)
                    redirect_ok(r, action)
                else:
                    r = await c.get(urlparse(action).path, params=data, headers=OUT)
                check(f"{method.upper()} {action} (from {page})", r.status, await _text(r))
            seen |= first
            for path in sorted(seen):                                   # stored: anything the forms saved
                r = await c.get(path, headers=OUT)
                check(f"GET {path} (after forms)", r.status, await _text(r))
            assert len(seen) > 5 and forms, (seen, forms)
            assert {"/account", "/casino", "/battlepass", "/store"} <= seen
            assert {"/account/login", "/casino/slots", "/store/checkout"} <= {k[0] for k in done}
            assert not problems, "\n".join(problems[:20])
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())
