"""Server slots: how many servers a customer may run.

limit = customers.base_slots (in the plan, default 1) + the active rows in slot_grants.
Only the owner writes slot_grants (owner panel / CLI) – no customer route, form field or request parameter can
change it; the limit is always recomputed from the database on the server side. Creating a server checks the limit
inside one write transaction (BEGIN IMMEDIATE), so parallel requests can't both take the last slot.
When the owner revokes slots and a customer has more servers than allowed, the newest servers are put "over_limit"
(stopped, data kept) – never deleted – until the owner grants again or the customer removes a server.
"""
from __future__ import annotations

import time

from control_plane.db import DB

COUNTED = ("provisioning", "awaiting_token", "active", "stopped", "suspended")   # over_limit/deleted don't count


class SlotError(Exception):
    pass


def slot_limit(db: DB, customer_id: int) -> int:
    row = db.one("SELECT base_slots FROM customers WHERE id = ?", (customer_id,))
    if row is None:
        return 0
    extra = db.one("SELECT COALESCE(SUM(slots), 0) AS n FROM slot_grants WHERE customer_id = ? AND revoked_at IS NULL",
                   (customer_id,))["n"]
    return int(row["base_slots"]) + int(extra)


def slots_used(db: DB, customer_id: int) -> int:
    marks = ",".join("?" * len(COUNTED))
    return int(db.one(f"SELECT COUNT(*) AS n FROM servers WHERE customer_id = ? AND status IN ({marks})",
                      (customer_id, *COUNTED))["n"])


def summary(db: DB, customer_id: int) -> dict:
    limit, used = slot_limit(db, customer_id), slots_used(db, customer_id)
    return {"limit": limit, "used": used, "free": max(0, limit - used)}


def reserve(db: DB, customer_id: int) -> None:
    """Raises SlotError if no slot is free. Call INSIDE db.transaction() together with the INSERT."""
    if slots_used(db, customer_id) >= slot_limit(db, customer_id):
        raise SlotError("All your server slots are in use. Extra slots are an add-on – contact us to unlock one.")


# ------------------------------------------------------------ Owner only

def grant(db: DB, customer_id: int, slots: int, *, owner: str, reason: str = "") -> int:
    if slots < 1 or slots > 100:
        raise SlotError("Grant between 1 and 100 slots.")
    if db.one("SELECT 1 FROM customers WHERE id = ? AND status != 'deleted'", (customer_id,)) is None:
        raise SlotError("No such customer.")
    with db.transaction():
        cur = db.run("INSERT INTO slot_grants (customer_id, slots, reason, granted_by, granted_at) VALUES (?, ?, ?, ?, ?)",
                     (customer_id, slots, reason[:200], owner, int(time.time())))
        db.audit(f"owner:{owner}", "slots.grant", customer_id=customer_id, detail=f"+{slots} {reason}")
    return int(cur.lastrowid)


def revoke(db: DB, grant_id: int, *, owner: str) -> list[int]:
    """Revokes a grant. Returns the servers that went over_limit because of it."""
    row = db.one("SELECT * FROM slot_grants WHERE id = ? AND revoked_at IS NULL", (grant_id,))
    if row is None:
        raise SlotError("No active grant with that id.")
    with db.transaction():
        db.run("UPDATE slot_grants SET revoked_at = ?, revoked_by = ? WHERE id = ?", (int(time.time()), owner, grant_id))
        db.audit(f"owner:{owner}", "slots.revoke", customer_id=row["customer_id"], detail=f"-{row['slots']} (#{grant_id})")
        over = enforce(db, row["customer_id"], actor=f"owner:{owner}")
    return over


def enforce(db: DB, customer_id: int, *, actor: str = "system") -> list[int]:
    """More servers than slots: the newest ones become over_limit (stopped, data kept)."""
    limit = slot_limit(db, customer_id)
    marks = ",".join("?" * len(COUNTED))
    rows = db.q(f"SELECT id FROM servers WHERE customer_id = ? AND status IN ({marks}) ORDER BY created_at, id",
                (customer_id, *COUNTED))
    over = [r["id"] for r in rows[limit:]]
    for sid in over:
        db.run("UPDATE servers SET status = 'over_limit', suspend_reason = 'More servers than slots' WHERE id = ?",
               (sid,))
        db.audit(actor, "server.over_limit", customer_id=customer_id, server_id=sid)
    return over


def release_over_limit(db: DB, customer_id: int, *, actor: str) -> list[int]:
    """After a grant: over_limit servers come back (oldest first) while slots are free."""
    back = []
    for r in db.q("SELECT id FROM servers WHERE customer_id = ? AND status = 'over_limit' ORDER BY created_at, id",
                  (customer_id,)):
        if slots_used(db, customer_id) >= slot_limit(db, customer_id):
            break
        has_token = db.one("SELECT 1 FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (r["id"],))
        db.run("UPDATE servers SET status = ?, suspend_reason = NULL WHERE id = ?",
               ("active" if has_token else "awaiting_token", r["id"]))
        db.audit(actor, "server.back_from_over_limit", customer_id=customer_id, server_id=r["id"])
        back.append(r["id"])
    return back
