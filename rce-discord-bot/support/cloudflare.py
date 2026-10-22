"""Use your own domain (bought on / moved to Cloudflare) for the web interface – set up automatically.

You give the bot a hostname (e.g. panel.yourdomain.com, or yourdomain.com itself) and a Cloudflare API token. The
bot then, through the Cloudflare API:
  1. finds the zone (your domain) and its account,
  2. creates a tunnel "rce-bot-<hostname>" (or reuses it if it already exists),
  3. points the tunnel at the web interface on this PC (http://127.0.0.1:<WEB_PORT>),
  4. creates the DNS record <hostname> -> <tunnel>.cfargotunnel.com (proxied), or updates its own old one,
  5. returns the tunnel token – the bot saves it as WEB_TUNNEL_TOKEN and the address as WEB_PUBLIC_URL.
The API token is only used for this setup and is not stored.

API token permissions (Cloudflare dashboard -> My Profile -> API Tokens -> Create Token -> Custom token):
  Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit, Zone > Zone > Read (zone: your domain).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("rce.cloudflare")

API = "https://api.cloudflare.com/client/v4"
HOSTNAME = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PERMISSIONS = "Account › Cloudflare Tunnel › Edit, Zone › DNS › Edit and Zone › Zone › Read"


class CloudflareError(Exception):
    """Message for the owner (what went wrong and what to do)."""


@dataclass
class DomainSetup:
    hostname: str
    url: str
    tunnel_id: str
    tunnel_name: str
    token: str
    dns: str            # "created" / "updated" / "unchanged"
    tunnel: str         # "created" / "reused"


def normalize_hostname(text: str) -> str:
    """'https://Panel.Example.com/x' -> 'panel.example.com'. Raises CloudflareError if it isn't a hostname."""
    host = str(text or "").strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].rstrip(".")
    if not HOSTNAME.match(host):
        raise CloudflareError("Enter the address like panel.yourdomain.com (or yourdomain.com).")
    return host


class CloudflareAPI:
    def __init__(self, token: str, *, timeout: float = 20.0) -> None:
        self.token = str(token or "").strip()
        if len(self.token) < 20:
            raise CloudflareError("Paste your Cloudflare API token (Cloudflare → My Profile → API Tokens).")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None):
        """One API request. Returns `result`; raises CloudflareError with Cloudflare's own message."""
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.request(method, API + path, json=json, params=params, headers=headers) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = {}
                    status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CloudflareError(f"Can't reach the Cloudflare API: {exc}") from exc
        if not isinstance(data, dict) or not data.get("success"):
            errors = (data.get("errors") if isinstance(data, dict) else None) or []
            text = "; ".join(str(e.get("message", e)) for e in errors if e) or f"HTTP {status}"
            if status in (401, 403) or any(str(e.get("code")) in ("10000", "9109") for e in errors if isinstance(e, dict)):
                raise CloudflareError(f"Cloudflare refused the API token ({text}). It needs these permissions: "
                                      f"{PERMISSIONS}.")
            raise CloudflareError(f"Cloudflare: {text}")
        return data.get("result")

    # ------------------------------------------------------------ Steps

    async def find_zone(self, hostname: str) -> dict:
        """The zone (domain) the hostname belongs to: tries panel.example.co.uk, example.co.uk, co.uk …"""
        labels = hostname.split(".")
        for i in range(len(labels) - 1):
            name = ".".join(labels[i:])
            zones = await self._call("GET", "/zones", params={"name": name})
            if zones:
                return zones[0]
        raise CloudflareError(f"No domain for {hostname} in this Cloudflare account (or the API token can't see it "
                              f"– it needs Zone › Zone › Read for your domain).")

    async def tunnel(self, account_id: str, name: str) -> tuple[dict, str]:
        """Existing tunnel with this name, else a new one. (tunnel, "created" / "reused")"""
        found = await self._call("GET", f"/accounts/{account_id}/cfd_tunnel", params={"name": name, "is_deleted": "false"})
        if found:
            return found[0], "reused"
        created = await self._call("POST", f"/accounts/{account_id}/cfd_tunnel",
                                   json={"name": name, "config_src": "cloudflare"})
        return created, "created"

    async def route(self, account_id: str, tunnel_id: str, hostname: str, port: int) -> None:
        """The tunnel sends <hostname> to the web interface on this PC (127.0.0.1 – not "localhost", which can be
        IPv6 while the panel listens on IPv4)."""
        await self._call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", json={"config": {
            "ingress": [{"hostname": hostname, "service": f"http://127.0.0.1:{int(port)}"},
                        {"service": "http_status:404"}]}})

    async def dns(self, zone_id: str, hostname: str, tunnel_id: str) -> str:
        """CNAME <hostname> -> <tunnel>.cfargotunnel.com (proxied). Never deletes or overwrites records it didn't
        make (an A/AAAA record or a CNAME elsewhere): asks the owner to remove it instead."""
        target = f"{tunnel_id}.cfargotunnel.com"
        records = await self._call("GET", f"/zones/{zone_id}/dns_records", params={"name": hostname})
        for rec in records or []:
            if rec.get("type") == "CNAME" and rec.get("content") == target:
                if rec.get("proxied"):
                    return "unchanged"
                await self._call("PATCH", f"/zones/{zone_id}/dns_records/{rec['id']}", json={"proxied": True})
                return "updated"
            if rec.get("type") == "CNAME" and str(rec.get("content", "")).endswith(".cfargotunnel.com"):
                await self._call("PATCH", f"/zones/{zone_id}/dns_records/{rec['id']}",
                                 json={"content": target, "proxied": True})     # an older tunnel of ours
                return "updated"
            if rec.get("type") in ("A", "AAAA", "CNAME"):
                raise CloudflareError(f"{hostname} already has a {rec['type']} record ({rec.get('content')}). Use "
                                      "another name (e.g. panel.yourdomain.com) or delete that record in the "
                                      "Cloudflare dashboard (DNS → Records) first.")
        await self._call("POST", f"/zones/{zone_id}/dns_records", json={
            "type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1,
            "comment": "RCE bot web interface (Cloudflare tunnel)"})
        return "created"

    async def setup(self, hostname: str, port: int) -> DomainSetup:
        hostname = normalize_hostname(hostname)
        zone = await self.find_zone(hostname)
        account_id = (zone.get("account") or {}).get("id")
        if not account_id:
            raise CloudflareError("Cloudflare didn't say which account the domain belongs to.")
        name = f"rce-bot-{hostname}"[:60]
        tun, how = await self.tunnel(account_id, name)
        await self.route(account_id, tun["id"], hostname, port)
        dns = await self.dns(zone["id"], hostname, tun["id"])
        token = tun.get("token") or await self._call("GET", f"/accounts/{account_id}/cfd_tunnel/{tun['id']}/token")
        if not isinstance(token, str) or len(token) < 20:
            raise CloudflareError("Cloudflare didn't return a tunnel token.")
        log.info("Own domain %s set up: tunnel %s (%s), DNS %s", hostname, name, how, dns)
        return DomainSetup(hostname, f"https://{hostname}", tun["id"], name, token, dns, how)


async def check_reachable(url: str, *, timeout: float = 12.0) -> tuple[bool, str]:
    """Opens the public address like a visitor would. (works, explanation)"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url.rstrip("/") + "/", allow_redirects=True) as resp:
                body = (await resp.content.read(200_000)).decode("utf-8", "replace")
                status = resp.status
    except aiohttp.ClientConnectorError as exc:
        return False, (f"The address doesn't resolve yet ({exc.os_error or exc}). New DNS records can take a "
                       "minute or two – try again shortly.")
    except (aiohttp.ClientError, TimeoutError) as exc:
        return False, f"No answer from {url} ({exc or type(exc).__name__})."
    if status == 200 and "RCE Bot Panel" in body:
        return True, f"{url} works – it reaches this bot."
    if status == 530 or "1033" in body:
        return False, ("Cloudflare can't reach the tunnel yet (error 1033/530). The bot is connecting it – try again "
                       "in a few seconds. If it stays like this, check that 'Reachable from the internet' is on.")
    if status == 502:
        return False, ("Cloudflare reached the tunnel but not the web interface (502). The tunnel points at the "
                       "wrong port – run the setup again after changing the port.")
    return False, f"{url} answered with HTTP {status}, but not with this bot's web interface."
