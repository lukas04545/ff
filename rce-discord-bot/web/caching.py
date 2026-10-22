"""HTTP helpers that make the websites faster: ETags (the browser re-checks a picture and gets "304 – unchanged"
instead of the whole file) and gzip for text responses."""
from __future__ import annotations

import hashlib

from aiohttp import web

YEAR = "public, max-age=31536000, immutable"      # versioned addresses (…?v=<hash>): never changes
COMPRESS_TYPES = ("text/", "application/json", "application/javascript", "image/svg+xml")
COMPRESS_MIN = 1024                                # smaller bodies aren't worth it


def etag_of(body: bytes) -> str:
    return '"' + hashlib.blake2s(body, digest_size=10).hexdigest() + '"'


def conditional(request: web.Request, body: bytes, content_type: str, cache: str, *,
                charset: str | None = None, headers: dict | None = None) -> web.Response:
    """A response with an ETag – or an empty 304 if the browser already has exactly this version."""
    etag = etag_of(body)
    out = {"Cache-Control": cache, "ETag": etag, "X-Content-Type-Options": "nosniff", **(headers or {})}
    if etag in request.headers.get("If-None-Match", ""):
        return web.Response(status=304, headers=out)
    return web.Response(body=body, content_type=content_type, charset=charset, headers=out)


def maybe_compress(request: web.Request, resp: web.StreamResponse) -> None:
    """gzip text responses (pages, JSON, CSS, JS) for browsers that accept it."""
    if type(resp) is not web.Response or resp.status == 304 or resp.headers.get("Content-Encoding"):
        return
    body = resp.body
    if not isinstance(body, (bytes, bytearray)) or len(body) < COMPRESS_MIN:
        return
    if not resp.content_type.startswith(COMPRESS_TYPES):
        return
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return
    resp.enable_compression(web.ContentCoding.gzip)
    resp.headers.add("Vary", "Accept-Encoding")
