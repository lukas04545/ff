"""Customers pay for the bot with PayPal: months in advance, extended from the current end, servers stop after."""
import asyncio
import re
import time

from aiohttp.test_utils import TestClient, TestServer

from control_plane import billing, paypal_billing, plans
from tests.test_control_plane import cp, csrf_of  # noqa: F401
from tests.test_webshop import FakePayPal


def _setup(cp, monkeypatch):  # noqa: F811
    cp.settings.paypal_client_id, cp.settings.paypal_secret = "id", "secret"
    fake = FakePayPal()
    monkeypatch.setattr(paypal_billing, "client", lambda settings: fake)
    plans.seed(cp.db)
    return fake


def test_pay_three_months_with_paypal_and_extend(cp, monkeypatch):  # noqa: F811
    from control_plane import web as cpweb
    fake = _setup(cp, monkeypatch)
    changed = []

    async def on_change():
        changed.append(1)

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, on_change)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            page = await (await a.get("/dashboard")).text()
            assert "Pay with PayPal" in page and "3 months" in page
            plan = plans.listed(cp.db)[0]
            r = await a.post("/billing/paypal", data={"csrf": csrf_of(page), "plan": str(plan["id"]), "months": "3"},
                             allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("https://www.sandbox.paypal.com/")
            assert fake.orders["PP1"]["amount"] == round(plan["price_cents"] * 3 / 100, 2)
            assert not billing.subscription_ok(cp.db, cid)                         # nothing before paying
            token = re.search(r"o=([^&]+)", fake.orders["PP1"]["return"])[1]
            a.session.cookie_jar.clear()                                           # back from paypal.com: no cookie
            r = await a.get(f"/billing/paypal/return?o={token}")
            assert r.status == 200 and "/dashboard?m=Thanks" in await r.text()     # same-site hop to the dashboard
            assert billing.subscription_ok(cp.db, cid) and changed
            until = paypal_billing.paid_until(cp.db, cid)
            assert abs(until - (time.time() + 90 * 86400)) < 60
            await a.get(f"/billing/paypal/return?o={token}")                       # twice: counted once
            assert paypal_billing.paid_until(cp.db, cid) == until
            # paying again early extends from the end (nothing lost)
            c = dict(cp.db.one("SELECT * FROM customers WHERE id = ?", (cid,)))
            await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 1)
            token2 = re.search(r"o=([^&]+)", fake.orders["PP2"]["return"])[1]
            await paypal_billing.complete(cp.db, cp.settings, token2)
            assert paypal_billing.paid_until(cp.db, cid) == until + 30 * 86400
            # after the paid time the servers stop like after a trial
            assert not billing.subscription_ok(cp.db, cid, now=until + 31 * 86400)
        finally:
            await a.close()
    asyncio.run(scenario())


def test_wrong_amount_cancel_and_forgotten_returns(cp, monkeypatch):  # noqa: F811
    from tests.test_control_plane import customer
    fake = _setup(cp, monkeypatch)
    cid = customer(cp, "b@x.io")
    c = dict(cp.db.one("SELECT * FROM customers WHERE id = ?", (cid,)))
    plan = plans.listed(cp.db)[0]

    async def go():
        await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 1)
        fake.paid["PP1"] = 0.01                                                    # PayPal reports less
        t1 = re.search(r"o=([^&]+)", fake.orders["PP1"]["return"])[1]
        assert (await paypal_billing.complete(cp.db, cp.settings, t1))["status"] == "review"
        await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 1)
        t2 = re.search(r"o=([^&]+)", fake.orders["PP2"]["return"])[1]
        paypal_billing.cancel(cp.db, t2)
        await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 6)
        fake.state["PP3"] = "APPROVED"                                             # paid, closed the page
        assert await paypal_billing.check_pending(cp.db, cp.settings, now=time.time() + 120) == 1
        assert paypal_billing.paid_until(cp.db, cid)
        try:
            await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 2)
        except paypal_billing.PayPalBillingError as exc:
            assert "1, 3, 6 or 12" in str(exc)
        cp.db.run("UPDATE subscriptions SET stripe_subscription_id = 'sub_1', status = 'active' WHERE customer_id = ?",
                  (cid,))
        try:
            await paypal_billing.start(cp.db, cp.settings, c, plan["id"], 1)
            raise AssertionError("double billing")
        except paypal_billing.PayPalBillingError as exc:
            assert "Stripe" in str(exc)
    asyncio.run(go())
    states = [r["status"] for r in cp.db.q("SELECT status FROM paypal_payments ORDER BY id")]
    assert states == ["review", "cancelled", "paid"]


def test_paypal_test_names_keys_of_the_wrong_mode(monkeypatch):
    from support import paypal

    async def token(self, session):
        if self.mode != "sandbox":                                # these keys are sandbox keys
            raise paypal.PayPalError("rejected")
        return "t"
    monkeypatch.setattr(paypal.PayPalClient, "_access_token", token)
    out = asyncio.run(paypal.diagnose("id", "sec", "Live"))
    assert not out["ok"] and "SANDBOX keys" in out["text"] and out["mode"] == "live"
    assert asyncio.run(paypal.diagnose("id", "sec", "sandbox"))["ok"]
    assert not asyncio.run(paypal.diagnose("", "", "live"))["ok"]
    assert paypal.normalize_mode(" Production ") == "live" and paypal.normalize_mode("x") == "sandbox"


def test_subscription_summary_and_paid_time_by_hand(cp):  # noqa: F811
    plans.seed(cp.db)
    cid = cp.db.run("INSERT INTO customers (email, name, password_hash, created_at) VALUES ('s@x.io', '', 'x', ?)",
                    (int(time.time()),)).lastrowid
    assert billing.summary(cp.db, cid)["status"] == "none" and not billing.summary(cp.db, cid)["running"]
    billing.start_trial(cp.db, cid, 2, actor="t")
    info = billing.summary(cp.db, cid)
    assert info["running"] and info["label"] == "Free trial" and "left" in info["text"] and info["left"] > 86400
    until = billing.add_days(cp.db, cid, 30, actor="owner:t")              # added to the running trial
    info = billing.summary(cp.db, cid)
    assert info["status"] == "paid" and info["ends"] == until and until > time.time() + 31 * 86400
    assert "1 month" not in info["text"] and "32 days left" in info["text"] or "31 days left" in info["text"]
    later = billing.summary(cp.db, cid, now=until + 3 * 86400)
    assert not later["running"] and later["label"] == "Paid time ended" and "3 days ago" in later["text"]
    cp.db.run("UPDATE subscriptions SET status = 'active', stripe_subscription_id = 'sub_1' WHERE customer_id = ?", (cid,))
    try:
        billing.add_days(cp.db, cid, 30, actor="owner:t")
        raise AssertionError("card customers are managed by Stripe")
    except ValueError as exc:
        assert "Stripe" in str(exc)
    assert billing.span(65 * 86400) == "2 months 5 days" and billing.span(7200) == "2 hours"


def test_paypal_reasons_in_plain_words_and_the_account_probe(monkeypatch):
    from support import paypal

    async def token(self, session):
        return "t"

    async def call(self, method, path, body=None, *, request_id=None):
        return 422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYEE_ACCOUNT_RESTRICTED"}]}
    monkeypatch.setattr(paypal.PayPalClient, "_access_token", token)
    monkeypatch.setattr(paypal.PayPalClient, "_call", call)
    out = asyncio.run(paypal.diagnose("id", "sec", "live", probe_currency="EUR"))
    assert not out["ok"] and "PAYEE_ACCOUNT_RESTRICTED" in out["text"] and "restricted" in out["text"]
    cap = asyncio.run(paypal.PayPalClient("id", "sec").capture("O1", amount=5, currency="EUR"))
    assert not cap.ok and cap.status == "PAYEE_ACCOUNT_RESTRICTED" and "Resolution" in cap.problem
    assert "compliance" in paypal.explain("COMPLIANCE_VIOLATION") and paypal.explain("X_Y") == "X_Y"
