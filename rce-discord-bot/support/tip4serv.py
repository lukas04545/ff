"""Tip4Serv store connection: the bot acts as a Tip4Serv "server" and receives its purchase commands.

Same protocol as the official Tip4Serv Rust plugin: every minute GET /v1/store/server/<id>/commands lists the
payments with commands for this server; afterwards POST the same route marks the commands as executed.
Authentication: "Bearer SERVER_KEY:<signature>" – HMAC-SHA256 over the key parts and the Unix time.

Setup on tip4serv.com: add a server for the bot, copy its key (three parts separated by dots) into the
setting TIP4SERV_KEY, and give the custom kit product this command (on that server):

    rcebot {discord_id} {total_paid} {currency} {PlayerName}

Markers Tip4Serv can't fill (e.g. no Discord login on the store) are simply ignored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger("rce.tip4serv")

API_BASE = "https://api.tip4serv.com/v1/store/server/"
COMMAND_PREFIX = "rcebot"
EXECUTED = "Executed"
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}


class Tip4ServError(Exception):
    pass


KEY_HELP = ("On tip4serv.com open your store → Servers → add (or open) a server for the bot with the PLUGIN "
            "connection (game Rust / Oxide, not \"Connect with RCON\"), and copy that server's key – three parts "
            "separated by dots, the first one is the server's number.")


def split_key(key: str) -> list[str]:
    parts = key.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise Tip4ServError("The Tip4Serv key must have three parts separated by dots. " + KEY_HELP)
    if parts[0].startswith("eyJ"):          # a JWT: the store API key from Dashboard -> API keys
        raise Tip4ServError("TIP4SERV_KEY is the store API key (Dashboard → API keys) – the bot needs a SERVER key "
                            "instead. " + KEY_HELP)
    return parts


def signature(key: str, timestamp: str) -> str:
    """HMAC like the official plugin: key = 2nd part, message = 1st + 3rd part + time; hex -> base64."""
    first, secret, third = split_key(key)
    digest = hmac.new(secret.encode("ascii"), (first + third + timestamp).encode("ascii"), hashlib.sha256).hexdigest()
    return base64.b64encode(digest.encode("ascii")).decode("ascii")


def parse_command(text: str) -> dict | None:
    """'rcebot {discord_id} {total_paid} {currency} {PlayerName}' (any order, missing markers are fine).
    -> {"discord_id", "amount", "currency", "player"} or None if it's not a command for the bot."""
    text = (text or "").strip()
    if not text.lower().startswith(COMMAND_PREFIX):
        return None
    discord_id = amount = currency = None
    name: list[str] = []
    for tok in text[len(COMMAND_PREFIX):].split():
        if tok.startswith("{") and tok.endswith("}"):
            continue                                      # marker Tip4Serv could not fill
        if discord_id is None and re.fullmatch(r"\d{17,20}", tok):
            discord_id = int(tok)
        elif amount is None and re.fullmatch(r"[€$£]?\d+(?:[.,]\d{1,2})?[€$£]?", tok):
            symbol = next((s for s in tok if s in _CURRENCY_SYMBOLS), None)
            amount = float(tok.strip("€$£").replace(",", "."))
            if symbol and currency is None:
                currency = _CURRENCY_SYMBOLS[symbol]
        elif currency is None and re.fullmatch(r"[A-Za-z]{3}", tok) and tok.upper() == tok:
            currency = tok.upper()
        else:
            name.append(tok)
    return {"discord_id": discord_id, "amount": amount, "currency": currency, "player": " ".join(name) or None}


class Tip4ServClient:
    def __init__(self, key: str, session: aiohttp.ClientSession | None = None, base: str = API_BASE) -> None:
        self.key = key.strip()
        self.server_id = split_key(self.key)[0]
        self._session = session
        self.base = base

    async def _request(self, method: str, body: dict | None = None):
        ts = str(int(time.time()))
        url = f"{self.base}{self.server_id}/commands?time={ts}"
        headers = {"Authorization": f"Bearer SERVER_KEY:{signature(self.key, ts)}", "Content-Type": "application/json"}
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.request(method, url, headers=headers, data=json.dumps(body) if body else None,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    hint = {401: " – the key is wrong or belongs to another server",
                            403: " – the key is wrong or belongs to another server",
                            404: " – Tip4Serv doesn't know this server number",
                            500: " – Tip4Serv couldn't handle the request; usually the key is not a plugin "
                                 "server key (e.g. the server was added with \"Connect with RCON\")"}.get(resp.status, "")
                    raise Tip4ServError(f"HTTP {resp.status}{hint} (server #{self.server_id})"
                                        + (f": {text[:200]}" if text.strip() else "") + ". " + KEY_HELP)
                return json.loads(text) if text.strip() else None
        except aiohttp.ClientError as exc:
            raise Tip4ServError(f"connection failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise Tip4ServError(f"unexpected answer: {text[:200]}") from exc
        finally:
            if self._session is None:
                await session.close()

    async def pending(self) -> list[dict]:
        """Payments with commands for this server (list of {id, action, player, username, steam_id, cmds})."""
        data = await self._request("GET")
        if data is None:
            return []
        if isinstance(data, dict):        # tolerate a wrapped list
            data = data.get("payments") or data.get("data") or []
        return [p for p in data if isinstance(p, dict) and p.get("id")]

    async def mark_executed(self, done: dict[str, tuple[str | None, list]]) -> None:
        """done: payment id -> (action, [command ids]) – reported as executed so Tip4Serv stops sending them."""
        if not done:
            return
        body = {pid: {"action": action, "cmds": {str(c): EXECUTED for c in cmds}} for pid, (action, cmds) in done.items()}
        await self._request("POST", body)
