"""Built-in reverse proxy: lets ONE listener (the portal's) serve every website, so a single Cloudflare tunnel
(*.<domain> -> the portal port) is enough – no Caddy, no open ports, no certificates on the server.

  portal.<domain> (and <domain>)   -> the portal itself (and the owner panel under /management)
  <slug>.<domain>                  -> that server's own web port on 127.0.0.1 (running), or its "paused" page
  an old address of an adopted server (host_alias:<host> in settings_kv) -> that server

X-Forwarded-For is always set to the visitor (the tenant treats 127.0.0.1 WITHOUT it as "this PC" – trusted), and
the Host header is kept, so the tenant's own same-origin checks see its real address.
"""
from __future__ import annotations

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from control_plane.db import DB
from control_plane.settings import Settings

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
MAX_BODY = 2 * 1024 * 1024


def _host(request: web.Request) -> str:
    return (request.headers.get("Host") or "").split(":")[0].strip().lower().rstrip(".")


def portal_host(settings: Settings) -> str:
    return settings.portal_url.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()


def resolve(db: DB, settings: Settings, host: str) -> tuple[str, dict | None]:
    """("portal" | "tenant" | "paused" | "unknown", server row)."""
    domain = settings.base_domain.lower()
    if not host or host in (portal_host(settings), f"portal.{domain}", domain, "127.0.0.1", "localhost"):
        return "portal", None
    alias = db.one("SELECT value FROM settings_kv WHERE key = ?", (f"host_alias:{host}",))
    if alias:
        row = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (int(alias["value"]),))
    elif host.endswith("." + domain) and "." not in host[:-len(domain) - 1]:
        row = db.one("SELECT * FROM servers WHERE slug = ? AND status != 'deleted'", (host[:-len(domain) - 1],))
    else:
        return "portal", None                       # any other name: the portal answers as usual
    if row is None:
        return "unknown", None
    return ("tenant" if row["status"] == "active" else "paused"), dict(row)


def middleware(db: DB, settings: Settings):
    session: dict[str, aiohttp.ClientSession] = {}

    @web.middleware
    async def proxy(request: web.Request, handler):
        kind, row = resolve(db, settings, _host(request))
        if kind == "portal":
            return await handler(request)
        if kind == "unknown":
            raise web.HTTPNotFound(text="There's no server at this address.")
        if kind == "paused":
            raise web.HTTPSeeOther(f"{settings.portal_url}/paused/{row['slug']}")
        if request.content_length and request.content_length > MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(MAX_BODY, request.content_length)
        body = await request.read()
        if len(body) > MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(MAX_BODY, len(body))
        headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in HOP)
        visitor = (request.headers.get("CF-Connecting-IP") or request.remote or "").strip()
        headers["X-Forwarded-For"] = visitor or "0.0.0.0"
        headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto") or "https"
        headers["Host"] = request.headers.get("Host", "")
        if "s" not in session or session["s"].closed:
            session["s"] = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=120),
                                                 cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with session["s"].request(request.method, f"http://127.0.0.1:{row['web_port']}{request.path_qs}",
                                            headers=headers, data=body or None, allow_redirects=False) as resp:
                data = await resp.read()
                out = CIMultiDict((k, v) for k, v in resp.headers.items() if k.lower() not in HOP)
                return web.Response(body=data, status=resp.status, headers=out)
        except (aiohttp.ClientError, OSError, TimeoutError):
            return web.Response(status=502, text="This server's website is starting – try again in a moment.")

    return proxy
