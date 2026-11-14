"""The owner panel's management AI: a request for one tenant server is carried out by that server's own bot
(POST /saas/manage on 127.0.0.1, Bearer token unique per server) – with every tool it has there (kits, emote binds,
teleports, ZORP, custom kits, the server's admin website …). The control plane only relays the text and the answer.
"""
from __future__ import annotations

import time

import aiohttp

from control_plane import billing, provisioning
from control_plane.crypto import SecretBox
from control_plane.db import DB


def account_note(db: DB, server_id: int) -> str:
    """What the control plane knows about the customer's account – the server's own AI can't see it, so questions
    like "when does this account expire?" get it along with the request."""
    row = db.one("SELECT customer_id FROM servers WHERE id = ?", (server_id,))
    if row is None:
        return ""
    info = billing.summary(db, row["customer_id"])
    return ("[Hosting account of this server's customer, from the control plane – use it for questions about the "
            f"subscription, expiry or payment and don't mention it otherwise. Package: {info['plan'] or 'none'}. "
            f"State: {info['label']}. Paid: {info['via']}. {info['text']} "
            f"Today is {time.strftime('%Y-%m-%d', time.gmtime())} (UTC).]\n\n")


async def ask(db: DB, box: SecretBox, server_id: int, message: str, history: list[dict], *,
              allow_risky: bool) -> dict:
    s = db.one("SELECT web_port FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        return {"ok": False, "error": "No such server."}
    token = provisioning.manage_token(provisioning.get_secret(db, box, server_id, "health_token") or "")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(f"http://127.0.0.1:{s['web_port']}/saas/manage",
                                    headers={"Authorization": f"Bearer {token}"},
                                    json={"message": account_note(db, server_id) + message, "history": history[-12:],
                                          "allow_risky": allow_risky}) as resp:
                if resp.status == 404:
                    return {"ok": False, "error": "The server didn't accept the request (not running the new code?)."}
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError) as exc:
        return {"ok": False, "error": f"The server isn't reachable – is it running? ({type(exc).__name__})"}
