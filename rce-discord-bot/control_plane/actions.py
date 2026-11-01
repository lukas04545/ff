"""Owner actions on customers and servers – one place for the owner panel (website) and the management Discord (HQ).

Every function writes the audit log with the given actor ("owner:<name>" or "discord:<user id>") and raises
slots.SlotError / provisioning.ProvisionError / features.FeatureError / ValueError with a message for the owner.
The caller then runs `changed()` so the supervisor and the proxy pick the change up.
"""
from __future__ import annotations

import time

from control_plane import features, provisioning, slots
from control_plane.db import DB
from control_plane.settings import Settings

SERVER_ACTIONS = ("suspend", "reactivate", "restart", "remove", "purge")
CUSTOMER_ACTIONS = ("grant", "suspend", "reactivate", "comp", "remove")


async def server(db: DB, settings: Settings, supervisor, sid: int, action: str, *, actor: str) -> str:
    """Returns a short text of what happened."""
    if db.one("SELECT 1 FROM servers WHERE id = ?", (sid,)) is None:
        raise ValueError(f"There's no server #{sid}.")
    if action == "suspend":
        provisioning.set_server_status(db, sid, "suspended", actor=actor, reason="by the owner")
    elif action == "reactivate":
        provisioning.set_server_status(db, sid, "active", actor=actor)
    elif action == "restart":
        if supervisor is None:
            raise ValueError("The supervisor isn't running here.")
        await supervisor.restart(sid)
    elif action == "remove":
        provisioning.remove_server(db, sid, actor=actor)
    elif action == "purge":
        provisioning.purge_server(db, settings, sid, actor=actor)
    else:
        raise ValueError(f"Unknown action {action!r}.")
    return {"suspend": "suspended", "reactivate": "reactivated", "restart": "restarting", "remove": "removed",
            "purge": "purged"}[action]


def customer(db: DB, cid: int, action: str, *, actor: str, owner: str, slots_n: int = 1, reason: str = "") -> str:
    if db.one("SELECT 1 FROM customers WHERE id = ?", (cid,)) is None:
        raise ValueError(f"There's no customer #{cid}.")
    if action == "grant":
        slots.grant(db, cid, int(slots_n or 1), owner=owner, reason=reason)
        slots.release_over_limit(db, cid, actor=actor)
        return f"+{int(slots_n or 1)} slot(s)"
    if action in ("suspend", "reactivate"):
        provisioning.set_customer_status(db, cid, "suspended" if action == "suspend" else "active",
                                         actor=actor, reason="by the owner" if action == "suspend" else None)
        return "suspended" if action == "suspend" else "reactivated"
    if action == "comp":
        row = db.one("SELECT status FROM subscriptions WHERE customer_id = ?", (cid,))
        new = "none" if row and row["status"] == "comped" else "comped"
        db.run("UPDATE subscriptions SET status = ?, updated_at = ? WHERE customer_id = ?", (new, int(time.time()), cid))
        db.audit(actor, f"subscription.{new}", customer_id=cid)
        return "comped (free)" if new == "comped" else "comp removed"
    if action == "remove":
        for s in db.q("SELECT id FROM servers WHERE customer_id = ? AND status != 'deleted'", (cid,)):
            provisioning.remove_server(db, s["id"], actor=actor)
        db.run("UPDATE customers SET status = 'deleted' WHERE id = ?", (cid,))
        db.audit(actor, "customer.remove", customer_id=cid)
        return "removed"
    raise ValueError(f"Unknown action {action!r}.")


def feature(db: DB, cid: int, name: str, on: bool, *, owner: str) -> str:
    if on:
        features.grant(db, cid, name, owner=owner)
    else:
        features.revoke(db, cid, name, owner=owner)
    return f"{features.FEATURES.get(name, name)} {'on' if on else 'off'}"


def manager_ai(db: DB, sid: int, on: bool, *, actor: str) -> str:
    provisioning.set_manager_ai(db, sid, on, actor=actor)
    return f"management AI in Discord {'on' if on else 'off'}"
