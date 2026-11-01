"""SaaS control plane: secrets, slots (server-side, race-free, owner-only), provisioning/isolation, billing,
supervisor, reverse proxy, customer portal, owner panel, migration import – and the bot's hosted-mode lockdown."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import billing, provisioning, proxy, slots
from control_plane.crypto import CryptoError, SecretBox, parse_keys
from control_plane.db import DB
from control_plane.settings import Settings

TOKEN = "MTAx" + "A" * 22 + ".Gabcde." + "z" * 30
TOKEN2 = "MTAy" + "B" * 22 + ".Hfghij." + "y" * 30


def key(n: int = 1) -> str:
    return f"{n}:" + base64.b64encode(bytes([n]) * 32).decode()


@pytest.fixture
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_MASTER_KEYS", key(1))
    settings = Settings(data_dir=tmp_path / "cp", tenants_dir=tmp_path / "tenants", base_domain="rust.test",
                        port_min=21000, port_max=21010, session_secret="s" * 40, caddy_file=tmp_path / "Caddyfile",
                        caddy_reload="")
    db = DB(settings.db_path)
    yield NS(db=db, box=SecretBox.from_env(), settings=settings, tmp=tmp_path)
    db.close()


def customer(cp, email="a@x.io", *, own_bot: bool = True) -> int:
    from control_plane import features
    cid = provisioning.create_customer(cp.db, email, "password1234")
    if own_bot:
        features.grant(cp.db, cid, "custom_bot", owner="test")
    return cid


def paid(cp, cid) -> None:
    cp.db.run("UPDATE subscriptions SET status = 'active' WHERE customer_id = ?", (cid,))


# ------------------------------------------------------------ Secrets

def test_secrets_are_bound_to_their_row_and_rotate(monkeypatch):
    box = SecretBox(parse_keys(key(1)))
    blob = box.encrypt(TOKEN, "server:1:discord_token")
    assert TOKEN not in blob and box.decrypt(blob, "server:1:discord_token") == TOKEN
    with pytest.raises(CryptoError):
        box.decrypt(blob, "server:2:discord_token")              # copied into another tenant's row: useless
    with pytest.raises(CryptoError):
        box.decrypt(blob[:-4] + "AAAA", "server:1:discord_token")   # tampered
    box2 = SecretBox(parse_keys(f"{key(1)},{key(2)}"))
    assert box2.decrypt(blob, "server:1:discord_token") == TOKEN and box2.needs_rotation(blob)
    assert box.fingerprint(TOKEN) == box2.fingerprint(TOKEN)       # stable across rotation


# ------------------------------------------------------------ Slots

def test_slot_limit_is_enforced_server_side_and_only_the_owner_grants(cp):
    cid = customer(cp)
    a = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="customer")
    assert slots.summary(cp.db, cid) == {"limit": 1, "used": 1, "free": 0}
    with pytest.raises(slots.SlotError):
        provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Second", actor="customer")
    gid = slots.grant(cp.db, cid, 1, owner="me", reason="add-on")
    b = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Second", actor="customer")
    assert slots.summary(cp.db, cid)["used"] == 2
    over = slots.revoke(cp.db, gid, owner="me")                     # the NEWEST server goes over_limit, data kept
    assert over == [b["id"]] and cp.db.one("SELECT status FROM servers WHERE id = ?", (b["id"],))["status"] == "over_limit"
    assert os.path.isdir(cp.db.one("SELECT home_dir FROM servers WHERE id = ?", (b["id"],))["home_dir"])
    with pytest.raises(provisioning.ProvisionError):
        provisioning.set_server_status(cp.db, b["id"], "active", actor="x")
    slots.grant(cp.db, cid, 1, owner="me")
    assert slots.release_over_limit(cp.db, cid, actor="owner:me") == [b["id"]]
    provisioning.remove_server(cp.db, a["id"], actor="customer")    # removing frees the slot (and the port)
    assert slots.summary(cp.db, cid)["used"] == 1


def test_two_parallel_requests_cannot_both_take_the_last_slot(cp):
    cid = customer(cp)
    results = []

    def worker(n):
        db = DB(cp.settings.db_path)                                # separate connections, like two requests
        try:
            provisioning.provision_server(db, cp.box, cp.settings, cid, f"Race {n}", actor="customer")
            results.append("ok")
        except slots.SlotError:
            results.append("full")
        finally:
            db.close()
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("full") == 5


# ------------------------------------------------------------ Provisioning & isolation

def test_each_server_gets_its_own_folder_port_secrets_and_no_leaks(cp, monkeypatch):
    c1, c2 = customer(cp, "a@x.io"), customer(cp, "b@x.io")
    s1 = provisioning.provision_server(cp.db, cp.box, cp.settings, c1, "Alpha", actor="t")
    s2 = provisioning.provision_server(cp.db, cp.box, cp.settings, c2, "Alpha", actor="t")
    r1, r2 = (cp.db.one("SELECT * FROM servers WHERE id = ?", (s["id"],)) for s in (s1, s2))
    assert r1["slug"] != r2["slug"] and r1["web_port"] != r2["web_port"] and r1["home_dir"] != r2["home_dir"]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(r1["home_dir"]).st_mode) == 0o700
    provisioning.set_bot_token(cp.db, cp.box, s1["id"], TOKEN, actor="t")
    with pytest.raises(provisioning.ProvisionError, match="already used"):
        provisioning.set_bot_token(cp.db, cp.box, s2["id"], TOKEN, actor="t")       # one token = one server
    with pytest.raises(provisioning.ProvisionError):
        provisioning.set_bot_token(cp.db, cp.box, s2["id"], "not-a-token", actor="t")
    env_file = open(os.path.join(r1["home_dir"], ".env")).read()
    assert "DISCORD_TOKEN" not in env_file and "DB_KEY" not in env_file and "RCE_SAAS" not in env_file
    assert "WEB_PORT=21000" in env_file and "pbkdf2" in env_file                     # hashed panel password
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_secret")
    env = provisioning.environment(cp.db, cp.box, cp.settings, s1["id"])
    assert env["DISCORD_TOKEN"] == TOKEN and env["RCE_SAAS"] == "1" and len(env["DB_KEY"]) == 64
    assert env["DB_KEY"] != provisioning.environment(cp.db, cp.box, cp.settings, s2["id"])["DB_KEY"]
    assert "CP_MASTER_KEYS" not in env and "STRIPE_SECRET_KEY" not in env           # control plane secrets stay
    provisioning.remove_server(cp.db, s1["id"], actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s2["id"], TOKEN, actor="t")          # token free again after removal
    provisioning.purge_server(cp.db, cp.settings, s1["id"], actor="t")
    assert not os.path.exists(r1["home_dir"]) and cp.db.q("SELECT * FROM secrets WHERE server_id = ?", (s1["id"],)) == []


# ------------------------------------------------------------ Billing

def sign(payload: bytes, secret: str, ts: int) -> str:
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


def test_stripe_webhooks_drive_running_and_suspension(cp):
    cid = customer(cp)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")
    assert provisioning.runnable(cp.db, s["id"]) is False                           # not paid yet
    body = json.dumps({"id": "evt_1"}).encode()
    now = int(time.time())
    assert billing.verify_signature(body, sign(body, "whsec", now), "whsec")["id"] == "evt_1"
    for bad in (sign(body, "other", now), sign(body, "whsec", now - 1000), "garbage"):
        with pytest.raises(billing.BillingError):
            billing.verify_signature(body, bad, "whsec")
    ev = lambda i, kind, obj: {"id": i, "type": kind, "data": {"object": obj}}   # noqa: E731
    assert billing.handle_event(cp.db, ev("e1", "checkout.session.completed",
                                          {"client_reference_id": str(cid), "customer": "cus_1",
                                           "subscription": "sub_1"})) == "linked"
    assert provisioning.runnable(cp.db, s["id"]) is True
    assert billing.handle_event(cp.db, ev("e1", "checkout.session.completed", {})) == "duplicate"
    assert billing.handle_event(cp.db, ev("e2", "invoice.payment_failed", {"customer": "cus_1"})) == "past_due"
    assert provisioning.runnable(cp.db, s["id"]) is True                            # grace period
    assert billing.subscription_ok(cp.db, cid, now=time.time() + 4 * 86400) is False
    assert billing.handle_event(cp.db, ev("e3", "customer.subscription.deleted", {"customer": "cus_1", "id": "sub_1"}))
    assert provisioning.runnable(cp.db, s["id"]) is False                           # suspended, data kept
    billing.handle_event(cp.db, ev("e4", "invoice.paid", {"customer": "cus_1"}))
    assert provisioning.runnable(cp.db, s["id"]) is True                            # reactivated


# ------------------------------------------------------------ Supervisor

class FakeProcess:
    def __init__(self):
        self.pid, self.returncode = 4242, None
        self.stdout = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, code):
        self.returncode = code
        self._done.set()

    def terminate(self):
        self.finish(0)

    kill = terminate


class FakeLauncher:
    def __init__(self):
        self.started = []

    async def start(self, env, home):
        self.started.append(env["DISCORD_TOKEN"])
        return FakeProcess()


def test_supervisor_runs_only_paid_active_servers_and_detects_crash_loops(cp, monkeypatch):
    from control_plane import supervisor as sup_mod
    from control_plane.supervisor import Supervisor
    monkeypatch.setattr(sup_mod.os, "killpg", lambda pid, sig: None)
    cid = customer(cp)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")

    async def main():
        sup = Supervisor(cp.db, cp.box, cp.settings, FakeLauncher())
        assert (await sup.reconcile())["started"] == []                             # unpaid
        paid(cp, cid)
        assert (await sup.reconcile())["started"] == [s["id"]] and sup.launcher.started == [TOKEN]
        proc = sup.procs[s["id"]]
        for _ in range(5):                                                          # keeps crashing
            proc.process.finish(1)
            await asyncio.sleep(0.01)
            proc.next_start = 0
            await sup.reconcile()
        assert proc.state == "crashloop"
        assert cp.db.one("SELECT process FROM health WHERE server_id = ?", (s["id"],))["process"] == "crashloop"
        await sup.restart(s["id"])                                                  # the owner's restart clears it
        assert proc.state == "running"
        provisioning.set_customer_status(cp.db, cid, "suspended", actor="owner:me")
        assert (await sup.reconcile())["stopped"] == [s["id"]] and proc.state == "stopped"
    asyncio.run(main())


def test_reverse_proxy_config(cp):
    cid = customer(cp)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Kryvon EU", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")
    text = proxy.render(cp.db, cp.settings)
    assert "kryvon-eu.rust.test {" in text and "reverse_proxy 127.0.0.1:21000" in text
    assert "header_up X-Forwarded-For {remote_host}" in text
    provisioning.set_server_status(cp.db, s["id"], "suspended", actor="t")
    assert "rewrite * /paused/kryvon-eu" in proxy.render(cp.db, cp.settings)
    assert asyncio.run(proxy.apply(cp.db, cp.settings)) and not asyncio.run(proxy.apply(cp.db, cp.settings))


# ------------------------------------------------------------ Portal & owner panel

def csrf_of(html: str) -> str:
    return re.search(r"name=csrf value=([0-9a-f]+)", html)[1]


def test_portal_customers_cannot_unlock_slots_or_touch_others(cp):
    from control_plane.web import build_owner, build_portal
    calls = []

    async def changed():
        calls.append(1)

    async def scenario():
        portal = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        other = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        owner = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        for c in (portal, other, owner):
            await c.start_server()
        try:
            r = await portal.post("/signup", data={"email": "a@x.io", "password": "password1234"},
                                  allow_redirects=False)
            assert r.status == 303
            page = await (await portal.get("/dashboard")).text()
            token = csrf_of(page)
            assert "0 of 1 slots" in page
            assert (await portal.post("/servers", data={"name": "One"})).status == 403          # no CSRF token
            assert "&lt;b&gt;" in await (await portal.get("/dashboard?m=<b>x</b>")).text()     # no HTML injection
            r = await other.post("/login", data={"email": "a@x.io", "password": "password1234"},
                                 headers={"Origin": "https://evil.example"}, allow_redirects=False)
            assert r.status == 403                                                           # no login CSRF
            r = await portal.post("/servers", data={"name": "One", "csrf": token})
            assert "Panel password" in await r.text()
            # tampering: extra fields are ignored, the limit comes from the database
            r = await portal.post("/servers", data={"name": "Two", "csrf": token, "slots": "99", "base_slots": "9"})
            assert "All your server slots are in use" in await r.text()
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            assert slots.slot_limit(cp.db, cid) == 1
            sid = cp.db.one("SELECT id FROM servers WHERE customer_id = ?", (cid,))["id"]
            # another customer can't see or change this server (IDOR)
            await other.post("/signup", data={"email": "b@x.io", "password": "password1234"})
            t2 = csrf_of(await (await other.get("/dashboard")).text())
            assert (await other.post(f"/servers/{sid}/token", data={"token": TOKEN, "csrf": t2})).status == 404
            assert (await other.post(f"/servers/{sid}/remove", data={"csrf": t2})).status == 404
            # the owner panel: a customer session is worthless there; owner routes don't exist on the portal
            assert (await portal.get("/customers/1", allow_redirects=False)).status == 404
            owner.session.cookie_jar.update_cookies({"cp_c": portal.session.cookie_jar.filter_cookies(
                portal.make_url("/"))["cp_c"].value})
            assert (await owner.get("/", allow_redirects=False)).headers["Location"] == "/login"
        finally:
            for c in (portal, other, owner):
                await c.close()
    asyncio.run(scenario())


def test_owner_panel_login_with_totp_grant_and_suspend(cp):
    from web.totp import code_at, new_secret
    from web.users import hash_password
    from control_plane.web import build_owner
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")

    async def changed():
        pass

    async def scenario():
        owner = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await owner.start_server()
        try:
            r = await owner.post("/login", data={"username": "boss", "password": "owner-password-123", "code": "000000"},
                                 allow_redirects=False)
            assert "Wrong" in r.headers["Location"]                                    # 2FA is required
            code = code_at(secret, int(time.time() // 30))
            await owner.post("/login", data={"username": "boss", "password": "owner-password-123", "code": code})
            page = await (await owner.get("/")).text()
            assert "a@x.io" in page and "main.rust.test" in page and "1 / 1" in page
            detail = await (await owner.get(f"/customers/{cid}")).text()
            token = csrf_of(detail)
            await owner.post(f"/customers/{cid}/grant", data={"slots": "2", "reason": "add-on", "csrf": token})
            assert slots.slot_limit(cp.db, cid) == 3
            await owner.post(f"/customers/{cid}/suspend", data={"csrf": token})
            assert provisioning.runnable(cp.db, s["id"]) is False
            await owner.post(f"/customers/{cid}/reactivate", data={"csrf": token})
            assert provisioning.runnable(cp.db, s["id"]) is True
            audit = await (await owner.get("/audit")).text()
            assert "slots.grant" in audit and "customer.suspended" in audit and "owner.login" in audit
        finally:
            await owner.close()
    asyncio.run(scenario())


# ------------------------------------------------------------ Migration

def test_import_an_existing_bot_folder(cp, tmp_path):
    from control_plane.cli import import_server
    src = tmp_path / "oldbot"
    (src / "data").mkdir(parents=True)
    con = sqlite3.connect(src / "data" / "bot.db")
    con.execute("CREATE TABLE players (name TEXT)")
    con.execute("INSERT INTO players VALUES ('Alice')")
    con.commit()
    con.close()
    (src / "rules.md").write_text("Be nice")
    (src / ".env").write_text(f"DISCORD_TOKEN={TOKEN}\nRCON_HOST=1.2.3.4\nWEB_PORT=8080\nSERVER_DISPLAY_NAME=Old\n"
                              "SUPPORT_KNOWLEDGE_FILE=/etc/passwd\n")
    out = import_server(cp.db, cp.box, cp.settings, src, "me@owner.io", "Kryvon")
    home = out["home"]
    assert sqlite3.connect(os.path.join(home, "data", "bot.db")).execute("SELECT name FROM players").fetchall() == [("Alice",)]
    env = open(os.path.join(home, ".env")).read()
    assert "RCON_HOST=1.2.3.4" in env and "DISCORD_TOKEN" not in env and "/etc/passwd" not in env
    assert "WEB_PORT=21000" in env                                                  # the control plane's port
    assert provisioning.get_secret(cp.db, cp.box, out["id"], "discord_token") == TOKEN
    assert provisioning.runnable(cp.db, out["id"])                                 # comped: your own server


# ------------------------------------------------------------ The bot in hosted mode

def test_bot_hosted_mode_lockdown(monkeypatch):
    import saas_mode
    from web.server import locked_settings
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_HEALTH_TOKEN", "h" * 64)
    assert {"DISCORD_TOKEN", "DATABASE_PATH", "SUPPORT_KNOWLEDGE_FILE", "ITEM_IMAGE_URL"} <= locked_settings()
    for method, path in (("POST", "/api/plugins/upload"), ("POST", "/api/plugins/generate"), ("POST", "/api/servers"),
                         ("POST", "/api/update"), ("GET", "/api/backups/full"), ("POST", "/api/allplugins/copy")):
        assert saas_mode.blocked(method, path), path
    assert not saas_mode.blocked("GET", "/api/kits") and not saas_mode.blocked("GET", "/api/servers")
    assert saas_mode.health_ok("Bearer " + "h" * 64) and not saas_mode.health_ok("Bearer nope")
    import dbcrypt
    monkeypatch.delenv("DB_KEY", raising=False)
    with pytest.raises(dbcrypt.KeyError_):
        dbcrypt.adopt_env_key()                                                     # never falls back to a shared file
    monkeypatch.setenv("DB_KEY", "ab" * 32)
    assert dbcrypt.adopt_env_key() == "ab" * 32
    monkeypatch.delenv("RCE_SAAS")
    assert locked_settings() == frozenset() and not saas_mode.blocked("POST", "/api/plugins/upload")
