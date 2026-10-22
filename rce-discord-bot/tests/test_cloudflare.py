"""Own domain via the Cloudflare API: zone lookup, tunnel create/reuse, route, DNS record, web endpoints."""
import asyncio

import pytest

from support import cloudflare as cf

TOKEN = "t" * 40


class FakeCF(cf.CloudflareAPI):
    """Records the API calls; answers like Cloudflare would."""

    def __init__(self, *, zones=("example.com",), tunnels=(), records=()):
        super().__init__(TOKEN)
        self.zones, self.tunnels, self.records, self.calls = set(zones), list(tunnels), list(records), []

    async def _call(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, json, params))
        if path == "/zones":
            name = params["name"]
            return [{"id": "Z1", "name": name, "account": {"id": "A1"}}] if name in self.zones else []
        if path == "/accounts/A1/cfd_tunnel" and method == "GET":
            return [t for t in self.tunnels if t["name"] == params["name"]]
        if path == "/accounts/A1/cfd_tunnel" and method == "POST":
            t = {"id": "T-NEW", "name": json["name"]}
            self.tunnels.append(t)
            return t
        if path.endswith("/configurations"):
            return {}
        if path.endswith("/token"):
            return "eyJ" + "x" * 60
        if path == "/zones/Z1/dns_records" and method == "GET":
            return [r for r in self.records if r["name"] == params["name"]]
        if path.startswith("/zones/Z1/dns_records"):
            return {"id": "R"}
        raise AssertionError((method, path))


def run(coro):
    return asyncio.run(coro)


def test_normalize_hostname():
    assert cf.normalize_hostname(" https://Panel.Example.com/login?x=1 ") == "panel.example.com"
    assert cf.normalize_hostname("example.co.uk.") == "example.co.uk"
    for bad in ("", "localhost", "http://", "a b.com", "-x.com"):
        with pytest.raises(cf.CloudflareError):
            cf.normalize_hostname(bad)
    with pytest.raises(cf.CloudflareError, match="API token"):
        cf.CloudflareAPI("short")


def test_setup_creates_tunnel_route_and_dns():
    api = FakeCF()
    r = run(api.setup("https://Panel.Example.com", 8080))
    assert (r.url, r.tunnel, r.dns, r.tunnel_id) == ("https://panel.example.com", "created", "created", "T-NEW")
    assert r.token.startswith("eyJ")
    # looked up panel.example.com first, then example.com
    assert [c[3]["name"] for c in api.calls if c[1] == "/zones"] == ["panel.example.com", "example.com"]
    route = next(c for c in api.calls if c[1].endswith("/configurations"))
    assert route[0] == "PUT" and route[2]["config"]["ingress"][0] == {
        "hostname": "panel.example.com", "service": "http://127.0.0.1:8080"}            # 127.0.0.1, not localhost
    assert route[2]["config"]["ingress"][-1] == {"service": "http_status:404"}
    dns = next(c for c in api.calls if c[0] == "POST" and c[1] == "/zones/Z1/dns_records")
    assert dns[2]["type"] == "CNAME" and dns[2]["content"] == "T-NEW.cfargotunnel.com" and dns[2]["proxied"] is True


def test_setup_again_reuses_everything():
    api = FakeCF(tunnels=[{"id": "T-OLD", "name": "rce-bot-panel.example.com"}],
                 records=[{"id": "R1", "name": "panel.example.com", "type": "CNAME",
                           "content": "T-OLD.cfargotunnel.com", "proxied": True}])
    r = run(api.setup("panel.example.com", 9000))
    assert (r.tunnel, r.dns, r.tunnel_id) == ("reused", "unchanged", "T-OLD")
    assert not any(c[0] == "POST" for c in api.calls)                                  # nothing new created
    # an older tunnel of ours on this name gets repointed
    api = FakeCF(tunnels=[{"id": "T-OLD", "name": "rce-bot-panel.example.com"}],
                 records=[{"id": "R1", "name": "panel.example.com", "type": "CNAME",
                           "content": "SOMEOTHER.cfargotunnel.com", "proxied": True}])
    assert run(api.setup("panel.example.com", 9000)).dns == "updated"


def test_setup_never_overwrites_foreign_records_or_unknown_domains():
    api = FakeCF(records=[{"id": "R1", "name": "example.com", "type": "A", "content": "1.2.3.4"}])
    with pytest.raises(cf.CloudflareError, match="already has a A record"):
        run(api.setup("example.com", 8080))
    assert not any(c[0] in ("PATCH", "DELETE") for c in api.calls)
    with pytest.raises(cf.CloudflareError, match="No domain"):
        run(FakeCF(zones=()).setup("panel.other.com", 8080))


def test_api_errors_explain_permissions(monkeypatch):
    class Resp:
        status = 403

        async def json(self, content_type=None):
            return {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(cf.aiohttp, "ClientSession", Session)
    with pytest.raises(cf.CloudflareError, match="Cloudflare Tunnel › Edit"):
        run(cf.CloudflareAPI(TOKEN).find_zone("panel.example.com"))


def test_web_endpoint_saves_token_and_switches_tunnel(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from tests.test_web import H, bot  # noqa: F401
    import envfile
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"),
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from config import load_config
    from web import server as srv
    b = RceBot(load_config("/nonexistent"))
    started = []

    async def fake_setup(self, hostname, port):
        assert self.token == TOKEN and port == 8080
        return cf.DomainSetup("panel.example.com", "https://panel.example.com", "T1", "rce-bot-panel.example.com",
                              "eyJ" + "y" * 60, "created", "created")
    monkeypatch.setattr(cf.CloudflareAPI, "setup", fake_setup)

    async def fake_check(url, **kw):
        return True, f"{url} works – it reaches this bot."
    monkeypatch.setattr(cf, "check_reachable", fake_check)

    async def main():
        web = srv.WebInterface(b, env_path=tmp_path / ".env", secret_path=tmp_path / "web_secret.key")
        monkeypatch.setattr(web, "_start_tunnel", lambda: started.append((web.ws.tunnel_token, web.ws.public_url)))
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            r = await c.post("/api/settings/cloudflare-domain", json={"hostname": "panel.example.com",
                                                                     "api_token": TOKEN}, headers=H)
            assert r.status == 401                                                     # owners only
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post("/api/settings/cloudflare-domain", json={"hostname": "x", "api_token": "short"}, headers=H)
            assert r.status == 400 and "API token" in (await r.json())["error"]
            r = await c.post("/api/settings/cloudflare-domain", json={"hostname": "panel.example.com",
                                                                     "api_token": TOKEN}, headers=H)
            data = await r.json()
            assert r.status == 200 and data["url"] == "https://panel.example.com" and data["dns"] == "created"
            env = envfile.read_env(tmp_path / ".env")
            assert env["WEB_TUNNEL_TOKEN"].startswith("eyJ") and env["WEB_PUBLIC_URL"] == "https://panel.example.com"
            assert env["WEB_TUNNEL"] == "true" and TOKEN not in (tmp_path / ".env").read_text()   # API token not kept
            assert started and started[-1] == (env["WEB_TUNNEL_TOKEN"], "https://panel.example.com")
            r = await c.post("/api/settings/cloudflare-domain/check", json={}, headers=H)
            assert (await r.json())["ok"] is True
        finally:
            await c.close()
            await b.rcon.close()
            b.db.close()
    run(main())
