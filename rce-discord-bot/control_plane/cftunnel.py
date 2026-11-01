"""Cloudflare for the whole platform, set up by itself: one tunnel, one wildcard DNS record, nothing to open.

`python3 -m control_plane cloudflare` (or owner panel → Settings → Cloudflare) takes your Cloudflare API token ONCE
and, through the Cloudflare API (support/cloudflare.py):
  1. finds your domain's zone and account,
  2. creates (or reuses) the tunnel "rce-saas-<domain>",
  3. routes *.<domain> through it to the portal listener – which passes each <slug>.<domain> on to that server
     (control_plane/hostproxy.py) – plus the old addresses of adopted servers, so their links keep working,
  4. creates the DNS record *.<domain> -> the tunnel (proxied) and moves old addresses that pointed at a tunnel,
  5. saves only the TUNNEL token (encrypted, like the other website settings). The API token is NOT stored.
`run` then starts cloudflared with that tunnel token. No Caddy, no certificates, no open ports needed.

API token permissions: Account › Cloudflare Tunnel › Edit, Zone › DNS › Edit, Zone › Zone › Read.
"""
from __future__ import annotations

import logging
from pathlib import Path

from control_plane import websettings
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

log = logging.getLogger("cp.cloudflare")


def old_addresses(db: DB, settings: Settings) -> dict[str, int]:
    """host -> server id: the public addresses adopted servers had before (their old WEB_PUBLIC_URL)."""
    from envfile import read_env
    out = {}
    for row in db.q("SELECT key, value FROM settings_kv WHERE key LIKE 'adopted:%'"):
        folder = Path(row["key"][len("adopted:"):])
        url = (read_env(folder / ".env").get("WEB_PUBLIC_URL") or "").strip()
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        if host and "trycloudflare.com" not in host:
            out[host] = int(row["value"])
    return out


async def setup(db: DB, box: SecretBox, settings: Settings, api_token: str, *, api=None) -> dict:
    from support.cloudflare import CloudflareAPI, CloudflareError
    domain = settings.base_domain.lower()
    if not domain or domain in ("example.com", "yourdomain.com"):
        raise CloudflareError("Set your domain first (Settings → Your domain, or init --domain).")
    cf = api or CloudflareAPI(api_token)
    zone = await cf.find_zone(domain)
    account = (zone.get("account") or {}).get("id")
    if not account:
        raise CloudflareError("Cloudflare didn't say which account the domain belongs to.")
    tunnel, how = await cf.tunnel(account, f"rce-saas-{domain}"[:60])
    service = f"http://127.0.0.1:{settings.portal_port}"
    aliases = {h: sid for h, sid in old_addresses(db, settings).items()
               if h == zone["name"] or h.endswith("." + zone["name"])}
    ingress = [{"hostname": h, "service": service} for h in sorted(aliases)]
    ingress += [{"hostname": f"*.{domain}", "service": service}, {"hostname": domain, "service": service},
                {"service": "http_status:404"}]
    await cf._call("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel['id']}/configurations",
                   json={"config": {"ingress": ingress}})
    dns = {f"*.{domain}": await cf.dns(zone["id"], f"*.{domain}", tunnel["id"])}
    for host, sid in aliases.items():
        try:
            dns[host] = await cf.dns(zone["id"], host, tunnel["id"])
            db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)", (f"host_alias:{host}", str(sid)))
        except CloudflareError as exc:
            dns[host] = f"skipped: {exc}"
    token = tunnel.get("token") or await cf._call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel['id']}/token")
    if not isinstance(token, str) or len(token) < 20:
        raise CloudflareError("Cloudflare didn't return a tunnel token.")
    websettings.save(db, box, {"CP_CF_TUNNEL_TOKEN": token}, actor="cloudflare-setup")
    websettings.refresh(settings, ["CP_CF_TUNNEL_TOKEN"])
    db.audit("owner", "cloudflare.setup", detail=f"tunnel {how}, dns " + ", ".join(f"{h}: {v}" for h, v in dns.items()))
    log.info("Cloudflare set up for %s (tunnel %s)", domain, how)
    return {"domain": domain, "tunnel": how, "dns": dns, "aliases": aliases}


def start_tunnel(settings: Settings):
    """cloudflared for the platform's tunnel (None if no tunnel is set up)."""
    if not settings.cf_tunnel_token:
        return None
    from web.tunnel import Tunnel
    t = Tunnel(settings.portal_port, token=settings.cf_tunnel_token, public_url=settings.portal_url,
               state_path=settings.data_dir / "cf_tunnel_state.json", log_path=settings.data_dir / "cloudflared.log")
    t.start()
    return t
