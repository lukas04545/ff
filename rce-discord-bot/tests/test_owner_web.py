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


def test_customer_with_a_username_instead_of_an_email(cp):  # noqa: F811
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import provisioning as prov
    from control_plane.web import build_portal

    with pytest.raises(prov.ProvisionError):
        prov.create_customer(cp.db, "kryvonfriend", "a-long-password")               # the public sign-up: e-mail
    for bad in ("ab", "has space", "x" * 40):
        with pytest.raises(prov.ProvisionError):
            prov.create_customer(cp.db, bad, "a-long-password", allow_username=True)
    cid = prov.create_customer(cp.db, "KryvonFriend", "a-long-password", allow_username=True, actor="owner:boss")
    assert cp.db.one("SELECT email FROM customers WHERE id = ?", (cid,))["email"] == "kryvonfriend"

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            assert "E-mail or username" in await (await c.get("/login")).text()
            r = await c.post("/login", data={"email": "kryvonfriend", "password": "wrong-password"},
                             allow_redirects=False)
            assert "Wrong" in r.headers["Location"]
            r = await c.post("/login", data={"email": "KryvonFriend", "password": "a-long-password"},
                             allow_redirects=False)
            assert r.headers["Location"] == "/dashboard"
        finally:
            await c.close()
    asyncio.run(scenario())


def test_owner_invites_another_owner_and_removes_them(cp):  # noqa: F811
    import asyncio
    import re as _re

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import websettings
    from control_plane.web import build_owner
    from tests.test_control_plane import csrf_of

    async def changed():
        pass

    async def scenario():
        boss = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await boss.start_server()
        mate = TestClient(boss.server)
        try:
            token = websettings.new_login_link(cp.db)
            await boss.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await boss.get("/owners")).text()
            assert "boss" in page and "Add a person" in page
            t = csrf_of(page)
            assert (await boss.post("/owners/invite", data={"username": "mate"})).status == 403    # CSRF
            page = await (await boss.post("/owners/invite", data={"csrf": t, "username": "mate",
                                                                   "role": "owner"})).text()
            link = _re.search(r"(http://\S+/invite/[A-Za-z0-9_-]+)", page)[1]
            path = link.split(str(boss.server.port), 1)[1]
            assert "already invited" in await (await boss.post("/owners/invite", data={"csrf": t, "username": "Mate"})).text()
            assert "Welcome, mate" in await (await mate.get(path)).text()
            assert "16+" in await (await mate.post(path, data={"password": "short"})).text()
            page = await (await mate.post(path, data={"password": "mates-password-1234"})).text()
            assert "You're in, mate" in page and "2FA key" in page
            assert (await mate.get("/settings", allow_redirects=False)).status == 200             # signed in
            assert "doesn't work" in await (await mate.get(path)).text()                           # used up
            assert cp.db.one("SELECT 1 FROM owners WHERE username = 'mate'")
            # mate can't remove themselves; boss removes mate -> mate's session ends at once
            page = await (await mate.get("/owners")).text()
            assert "can't remove yourself" in await (await mate.post("/owners/mate/remove",
                                                                     data={"csrf": csrf_of(page)})).text()
            r = await boss.post("/owners/mate/remove", data={"csrf": t}, allow_redirects=False)
            assert r.status == 303 and cp.db.one("SELECT 1 FROM owners WHERE username = 'mate'") is None
            assert (await mate.get("/settings", allow_redirects=False)).status == 303
            assert "last owner" in await (await boss.post("/owners/boss/remove", data={"csrf": t})).text() or \
                "yourself" in await (await boss.post("/owners/boss/remove", data={"csrf": t})).text()
        finally:
            await mate.close()
            await boss.close()
    asyncio.run(scenario())


def test_customers_without_a_server_show_and_get_one(cp):  # noqa: F811
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import billing, websettings
    from control_plane import provisioning as prov
    from control_plane.web import build_owner
    from tests.test_control_plane import csrf_of

    cid = prov.create_customer(cp.db, "newbie", "a-long-password", allow_username=True)
    billing.start_trial(cp.db, cid, 7, actor="test")

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            for path in ("/", "/servers"):
                page = await (await c.get(path)).text()
                assert "without a server yet (1)" in page and "newbie" in page, path
            r = await c.post(f"/customers/{cid}/server", data={"csrf": csrf_of(page), "name": "Newbie EU"})
            page = await r.text()
            assert "is created" in page and "Panel password" in page
            assert cp.db.one("SELECT name FROM servers WHERE customer_id = ?", (cid,))["name"] == "Newbie EU"
            assert "without a server yet" not in await (await c.get("/servers")).text()
        finally:
            await c.close()
    asyncio.run(scenario())


def test_staff_accounts_can_only_look_after_customers(cp):  # noqa: F811
    import asyncio
    import re as _re

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import websettings
    from control_plane.web import build_owner
    from tests.test_control_plane import csrf_of

    async def changed():
        pass

    async def scenario():
        boss = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await boss.start_server()
        mate = TestClient(boss.server)
        try:
            token = websettings.new_login_link(cp.db)
            await boss.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await boss.get("/owners")).text()
            assert "Staff – customers only" in page
            t = csrf_of(page)
            page = await (await boss.post("/owners/invite", data={"csrf": t, "username": "mate",
                                                                   "role": "staff"})).text()
            path = "/invite/" + _re.search(r"/invite/([A-Za-z0-9_-]+)", page)[1]
            await mate.post(path, data={"password": "mates-password-1234"})
            assert cp.db.one("SELECT role FROM owners WHERE username = 'mate'")["role"] == "staff"
            # allowed: customers
            page = await (await mate.get("/customers")).text()
            assert "New customer" in page and "Packages" not in page and "Owners" not in page   # menu trimmed
            mt = csrf_of(page)
            r = await mate.post("/customers/new", data={"csrf": mt, "email": "trialguy", "access": "trial",
                                                        "days": "7", "plan": "0"})
            assert "Customer #" in await r.text()
            assert "Passkeys" in await (await mate.get("/settings")).text()
            assert "Your domain" not in await (await mate.get("/settings")).text()        # no platform settings
            # refused: everything else
            for method, url in (("GET", "/owners"), ("GET", "/plans"), ("GET", "/domains"), ("GET", "/audit"),
                                ("POST", "/settings"), ("POST", "/owners/invite"), ("POST", "/update/install"),
                                ("POST", "/customers/1/remove"), ("POST", "/servers/1/purge")):
                r = await (mate.get(url) if method == "GET" else mate.post(url, data={"csrf": mt}))
                assert r.status == 403, url
            # an owner still can; the last full owner can't be removed, staff can
            assert (await boss.get("/plans")).status == 200
            r = await boss.post("/owners/mate/remove", data={"csrf": t}, allow_redirects=False)
            assert r.status == 303 and cp.db.one("SELECT 1 FROM owners WHERE username = 'mate'") is None
        finally:
            await mate.close()
            await boss.close()
    asyncio.run(scenario())


def test_installing_an_update_restarts_the_control_plane(cp, monkeypatch):  # noqa: F811
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import updates, websettings
    from control_plane.web import build_owner
    from tests.test_control_plane import csrf_of
    restarts, seen = [], []

    async def fake_install(db, settings, supervisor, *, owner, updater=None):
        seen.append(supervisor)
        status = "updated" if len(seen) == 1 else "current"
        return {"last_install": {"status": status, "message": "v2 installed" if status == "updated" else "up to date",
                                 "restarted": 0}}
    monkeypatch.setattr(updates, "install", fake_install)

    async def changed():
        pass

    async def scenario():
        from types import SimpleNamespace as _NS
        sup = _NS(procs={})
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed, supervisor=sup,
                                              restart=lambda: restarts.append(1))))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            t = csrf_of(await (await c.get("/")).text())
            page = await (await c.post("/update/install", data={"csrf": t})).text()
            assert "Update installed" in page and "v2 installed" in page
            assert seen == [None]                                   # no extra rolling restart before it
            await asyncio.sleep(1.3)
            assert restarts == [1]                                  # the control plane restarts by itself
            r = await c.post("/update/install", data={"csrf": t}, allow_redirects=False)
            assert r.status == 303 and restarts == [1]              # nothing new: no restart
        finally:
            await c.close()
    asyncio.run(scenario())
