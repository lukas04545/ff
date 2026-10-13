"""PayPal checkout (REST API, Orders v2) for the web store.

Flow: the bot creates an order for the exact price -> the buyer is sent to PayPal and approves it -> PayPal sends
them back to the website -> the bot captures the payment and only delivers when PayPal reports the capture as
COMPLETED for exactly the expected amount and currency. Nothing is charged until the bot captures.

Credentials: PayPal Developer Dashboard -> Apps & Credentials -> your app (Client ID + Secret). PAYPAL_MODE
"sandbox" uses PayPal's test system (test accounts, no real money), "live" takes real payments.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import aiohttp

log = logging.getLogger("rce.paypal")

API = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
# Currencies PayPal accepts without decimals
NO_DECIMALS = {"HUF", "JPY", "TWD"}


class PayPalError(Exception):
    pass


def money(amount: float | Decimal, currency: str) -> str:
    """Amount as PayPal expects it ("12.50", or "1250" for currencies without decimals)."""
    places = Decimal("1") if currency.upper() in NO_DECIMALS else Decimal("0.01")
    return str(Decimal(str(amount)).quantize(places, rounding=ROUND_HALF_UP))


@dataclass
class Capture:
    ok: bool                  # completed and the amount/currency match
    status: str               # COMPLETED / PENDING / DECLINED / … (or the order status)
    capture_id: str | None
    amount: str | None
    currency: str | None
    payer_email: str | None
    payer_name: str | None
    problem: str | None = None


class PayPalClient:
    def __init__(self, client_id: str, secret: str, mode: str = "sandbox", *, session_factory=None) -> None:
        self.client_id = client_id.strip()
        self.secret = secret.strip()
        self.mode = mode if mode in API else "sandbox"
        self.base = API[self.mode]
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        self._session_factory = session_factory or (lambda: aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)))

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        async with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            async with session.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                    auth=aiohttp.BasicAuth(self.client_id, self.secret),
                                    headers={"Accept": "application/json"}) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or "access_token" not in data:
                    raise PayPalError("PayPal rejected the Client ID / Secret"
                                      + (" (sandbox and live credentials are different – check PAYPAL_MODE)"
                                         if resp.status == 401 else f" (HTTP {resp.status})"))
            self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3000)))
            return self._token[0]

    async def _call(self, method: str, path: str, body: dict | None = None, *,
                    request_id: str | None = None) -> tuple[int, dict]:
        if not self.configured:
            raise PayPalError("PayPal is not set up (PAYPAL_CLIENT_ID / PAYPAL_SECRET).")
        async with self._session_factory() as session:
            token = await self._access_token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                       "Accept": "application/json"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id      # PayPal ignores repeats of the same request
            try:
                async with session.request(method, f"{self.base}{path}", json=body, headers=headers) as resp:
                    data = await resp.json(content_type=None) if resp.content_length != 0 else {}
                    return resp.status, data or {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise PayPalError(f"PayPal is not reachable ({exc.__class__.__name__}).") from None

    async def create_order(self, *, amount: float, currency: str, description: str, reference: str,
                           return_url: str, cancel_url: str, brand: str = "") -> tuple[str, str]:
        """Creates an order. Returns (PayPal order ID, URL the buyer has to open to pay)."""
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference[:256], "custom_id": reference[:127], "description": description[:127],
                "amount": {"currency_code": currency.upper(), "value": money(amount, currency)},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": (brand or "Web store")[:127], "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING", "return_url": return_url, "cancel_url": cancel_url,
            }}},
        }
        status, data = await self._call("POST", "/v2/checkout/orders", body, request_id=f"create-{reference}")
        if status not in (200, 201) or not data.get("id"):
            raise PayPalError(f"PayPal could not create the order (HTTP {status}: "
                              f"{data.get('message') or data.get('name') or 'unknown error'}).")
        link = next((link["href"] for link in data.get("links", []) if link.get("rel") in ("payer-action", "approve")),
                    None)
        if not link:
            raise PayPalError("PayPal sent no payment link.")
        return data["id"], link

    async def get_order(self, order_id: str) -> dict:
        status, data = await self._call("GET", f"/v2/checkout/orders/{order_id}")
        if status != 200:
            raise PayPalError(f"PayPal order {order_id} not found (HTTP {status}).")
        return data

    async def capture(self, order_id: str, *, amount: float, currency: str) -> Capture:
        """Takes the money for an approved order and checks that exactly `amount` `currency` arrived."""
        status, data = await self._call("POST", f"/v2/checkout/orders/{order_id}/capture", {},
                                        request_id=f"capture-{order_id}")
        if status == 422 and "ORDER_ALREADY_CAPTURED" in str(data):
            data, status = await self.get_order(order_id), 200
        if status not in (200, 201):
            issue = next((d.get("issue") for d in data.get("details", []) if isinstance(d, dict)), None)
            return Capture(False, issue or data.get("name") or f"HTTP {status}", None, None, None, None, None,
                           problem=data.get("message") or "PayPal refused the capture.")
        return self.check(data, amount=amount, currency=currency)

    @staticmethod
    def check(data: dict, *, amount: float, currency: str) -> Capture:
        units = data.get("purchase_units") or [{}]
        captures = ((units[0].get("payments") or {}).get("captures") or [])
        cap = captures[0] if captures else {}
        payer = data.get("payer") or {}
        name = " ".join(filter(None, [(payer.get("name") or {}).get("given_name"),
                                      (payer.get("name") or {}).get("surname")])) or None
        got_amount = (cap.get("amount") or {}).get("value")
        got_currency = (cap.get("amount") or {}).get("currency_code")
        status = cap.get("status") or data.get("status") or "UNKNOWN"
        result = Capture(False, status, cap.get("id"), got_amount, got_currency, payer.get("email_address"), name)
        if status != "COMPLETED":
            result.problem = f"Payment status is {status}."
        elif got_currency != currency.upper() or got_amount is None or \
                Decimal(got_amount) != Decimal(money(amount, currency)):
            result.problem = (f"PayPal reports {got_amount} {got_currency}, expected "
                              f"{money(amount, currency)} {currency.upper()}.")
        else:
            result.ok = True
        return result
