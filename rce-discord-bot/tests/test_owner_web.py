"""The owner panel as a website on the portal (/management): same login (password + 2FA), own cookie path,
customers' sessions are worthless there, links and redirects stay under the prefix, optional IP allowlist."""
import asyncio
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import provisioning
from control_plane.settings import _owner_path
from tests.test_control_plane import cp, csrf_of, customer, paid  # noqa: F401


def _apps(cp, allow=frozenset()):  # noqa: F811
    from control_plane.web import build_owner, build_portal, mount_owner

    async def changed():
        pass
    portal = build_portal(cp.db, cp.box, cp.settings, changed)
    mount_owner(portal, build_owner(cp.db, cp.box, cp.settings, changed, prefix="/management", allow_ips=allow),
                "/management")
    return portal


def _owner(cp):  # noqa: F811
    from web.totp import new_secret
    from web.users import hash_password
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))
    return secret


def test_owner_panel_under_management_on_the_portal(cp):  # noqa: F811
    from web.totp import code_at
    secret = _owner(cp)
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")

    async def scenario():
        c = TestClient(TestServer(_apps(cp)))
        await c.start_server()
        try:
            r = await c.get("/management", allow_redirects=False)
            assert r.headers["Location"] == "/management/"
            r = await c.get("/management/", allow_redirects=False)
            assert r.headers["Location"] == "/management/login"
            page = await (await c.get("/management/login")).text()
            assert "action=/management/login" in page
            # a customer signed in on the portal gets nothing in the owner panel
            await c.post("/signup", data={"email": "z@x.io", "password": "password1234"})
            assert (await c.get("/management/", allow_redirects=False)).headers["Location"] == "/management/login"
            r = await c.post("/management/login", data={"username": "boss", "password": "wrong-password",
                                                         "code": "000000"}, allow_redirects=False)
            assert r.headers["Location"].startswith("/management/login?e=")
            r = await c.post("/management/login", data={"username": "boss", "password": "owner-password-123",
                                                        "code": code_at(secret, int(time.time() // 30))},
                             allow_redirects=False)
            assert r.headers["Location"] == "/management/"
            assert "Path=/management" in r.headers["Set-Cookie"]
            r = await c.get("/management/")
            page = await r.text()
            assert "Owner panel" in page and r.headers["X-Robots-Tag"].startswith("noindex")
            # every link and form stays under /management
            for target in re.findall(r"(?:href|action)='?(/[^ '>]*)", page):
                assert target.startswith("/management/"), target
            t = csrf_of(page)
            r = await c.post(f"/management/servers/{s['id']}/suspend", data={"csrf": t}, allow_redirects=False)
            assert r.headers["Location"] == "/management/"
            assert cp.db.one("SELECT status FROM servers WHERE id = ?", (s["id"],))["status"] == "suspended"
            assert (await c.post(f"/management/servers/{s['id']}/reactivate", data={})).status == 403   # CSRF
            r = await c.post("/management/logout", data={"csrf": t}, allow_redirects=False)
            assert r.headers["Location"] == "/management/login"
        finally:
            await c.close()
    asyncio.run(scenario())


def test_management_allowlist_and_username_lockout(cp):  # noqa: F811
    _owner(cp)

    async def scenario():
        c = TestClient(TestServer(_apps(cp, frozenset({"203.0.113.7"}))))
        await c.start_server()
        try:
            assert (await c.get("/management/login")).status == 404
            assert (await c.get("/management/login", headers={"CF-Connecting-IP": "203.0.113.7"})).status == 200
            assert (await c.get("/")).status == 200                                   # the portal itself is unaffected
        finally:
            await c.close()
        c = TestClient(TestServer(_apps(cp)))
        await c.start_server()
        try:
            for i in range(10):                                                     # different "IPs", same username
                await c.post("/management/login", data={"username": "boss", "password": "x", "code": "1"},
                             headers={"CF-Connecting-IP": f"198.51.100.{i}"})
            r = await c.post("/management/login", data={"username": "boss", "password": "x", "code": "1"},
                             headers={"CF-Connecting-IP": "198.51.100.99"}, allow_redirects=False)
            assert "Too+many" in r.headers["Location"]
        finally:
            await c.close()
    asyncio.run(scenario())


def test_owner_path_setting():
    assert _owner_path("/management") == "/management" and _owner_path("admin-x/") == "/admin-x"
    assert _owner_path("off") == "" and _owner_path("") == ""
    with pytest.raises(ValueError):
        _owner_path("/a/b")


def test_admin_is_reserved_for_the_honeypot():
    for bad in ("/admin", "admin", "/Admin/", "/login", "/dashboard"):
        with pytest.raises(ValueError):
            _owner_path(bad)


def test_customers_get_the_secret_panel_link_not_the_honeypot(cp):  # noqa: F811
    cid = customer(cp)
    paid(cp, cid)
    out = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    assert "/admin" not in out["panel_url"] and re.search(r"/panel-[A-Za-z0-9]{16}$", out["panel_url"])
    row = cp.db.one("SELECT * FROM servers WHERE id = ?", (out["id"],))
    assert provisioning.panel_url(cp.settings, row) == out["panel_url"]
    provisioning.finish_provisioning(cp.db, cp.box, cp.settings, out["id"])        # a re-run keeps the address
    assert provisioning.panel_url(cp.settings, row) == out["panel_url"]


def test_links_command_shows_the_owner_panel_and_secret_panels(cp, capsys, monkeypatch):  # noqa: F811
    from control_plane import cli
    cid = customer(cp)
    paid(cp, cid)
    out = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    assert cli.main(["links"]) == 0
    text = capsys.readouterr().out
    assert f"{cp.settings.portal_url}/management" in text and out["panel_url"] in text
    assert "/admin" not in text
    assert cli.main(["links", "--email", "nobody@x.io"]) == 0
    assert "Servers: none" in capsys.readouterr().out


def test_restart_control_plane_button(cp):  # noqa: F811
    from control_plane import updates
    from control_plane.web import build_owner
    from web.totp import code_at
    secret = _owner(cp)
    restarted = []
    updates._save(cp.db, {"control_plane_restart_needed": True})

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed,
                                              restart=lambda: restarted.append(1))))
        await c.start_server()
        try:
            await c.post("/login", data={"username": "boss", "password": "owner-password-123",
                                         "code": code_at(secret, int(time.time() // 30))})
            page = await (await c.get("/")).text()
            assert "Restart control plane" in page and "still runs the old code" in page
            assert (await c.post("/update/restart", data={})).status == 403                 # CSRF
            r = await c.post("/update/restart", data={"csrf": csrf_of(page)})
            assert "Restarting" in await r.text() and restarted == []                  # only after the page is sent
            await asyncio.sleep(1.2)
            assert restarted == [1]
            assert any(row["action"] == "control_plane.restart" for row in cp.db.q("SELECT action FROM audit_log"))
        finally:
            await c.close()
    asyncio.run(scenario())


def test_owner_creates_a_customer_with_a_free_trial(cp, monkeypatch):  # noqa: F811
    import asyncio
    import re as _re
    import time as _time

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import billing, websettings
    from control_plane.web import build_owner
    from tests.test_control_plane import csrf_of

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await c.get("/customers")).text()
            assert "New customer" in page
            t = csrf_of(page)
            plan = cp.db.one("SELECT id FROM plans ORDER BY id LIMIT 1")["id"]
            r = await c.post("/customers/new", data={"csrf": t, "email": "Trial@Example.com", "name": "Trial clan",
                                                     "password": "", "plan": str(plan), "access": "trial",
                                                     "days": "7"})
            page = await r.text()
            assert "Customer #" in page and "trial@example.com" in page
            password = _re.search(r"Password: ([^<\s]+)", page)[1]
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'trial@example.com'")["id"]
            from web.users import verify_password
            assert verify_password(password, cp.db.one("SELECT password_hash FROM customers WHERE id = ?",
                                                       (cid,))["password_hash"])
            sub = cp.db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (cid,))
            assert sub["status"] == "trial" and sub["plan_id"] == plan
            assert abs(sub["current_period_end"] - (_time.time() + 7 * 86400)) < 60
            assert billing.subscription_ok(cp.db, cid)                               # servers may run …
            assert not billing.subscription_ok(cp.db, cid, now=_time.time() + 8 * 86400)   # … until it ends
            assert "until" in await (await c.get("/customers")).text()
            # extend it from the customer page; a bad e-mail / duplicate is refused
            r = await c.post(f"/customers/{cid}/trial", data={"csrf": t, "days": "30"}, allow_redirects=False)
            assert r.status == 303
            assert cp.db.one("SELECT current_period_end FROM subscriptions WHERE customer_id = ?",
                             (cid,))["current_period_end"] > _time.time() + 29 * 86400
            r = await c.post("/customers/new", data={"csrf": t, "email": "trial@example.com", "access": "comped"})
            assert "exists already" in await r.text()
            r = await c.post("/customers/new", data={"csrf": t, "email": "free@example.com", "password": "a-long-password",
                                                     "plan": "0", "access": "comped"})
            assert "a-long-password" in await r.text()
            assert cp.db.one("SELECT s.status FROM subscriptions s JOIN customers c ON c.id = s.customer_id "
                             "WHERE c.email = 'free@example.com'")["status"] == "comped"
            assert (await c.post("/customers/new", data={"email": "x@example.com"})).status == 403   # CSRF
        finally:
            await c.close()
    asyncio.run(scenario())
