"""Add-ons the OWNER switches on per customer (like slots: no customer route can change them).

  custom_bot  the customer's servers may run their OWN bot token (own name/avatar) instead of the shared bot
  ai          the AI features (tickets AI, AI chat, AI moderation, management AI) – with the platform's DeepSeek key

Switching an add-on off never deletes anything: servers on their own bot go back to "set up the bot" (shared bot,
the customer adds it to their Discord again); AI simply stops. Running servers restart with the new setup by
themselves (the supervisor notices the changed environment).
"""
from __future__ import annotations

import time

from control_plane.db import DB

FEATURES = {"custom_bot": "Own bot (custom token)", "ai": "AI features"}


class FeatureError(Exception):
    pass


def has(db: DB, customer_id: int, feature: str) -> bool:
    return db.one("SELECT 1 FROM features WHERE customer_id = ? AND feature = ? AND revoked_at IS NULL",
                  (customer_id, feature)) is not None


def active(db: DB, customer_id: int) -> set[str]:
    return {r["feature"] for r in db.q("SELECT feature FROM features WHERE customer_id = ? AND revoked_at IS NULL",
                                       (customer_id,))}


def grant(db: DB, customer_id: int, feature: str, *, owner: str) -> None:
    if feature not in FEATURES:
        raise FeatureError("Unknown add-on.")
    if has(db, customer_id, feature):
        return
    db.run("INSERT INTO features (customer_id, feature, granted_by, granted_at) VALUES (?, ?, ?, ?)",
           (customer_id, feature, owner, int(time.time())))
    db.audit(f"owner:{owner}", f"feature.{feature}.on", customer_id=customer_id)


def revoke(db: DB, customer_id: int, feature: str, *, owner: str) -> list[int]:
    """Switches the add-on off. Returns the servers that changed (own bot -> back to the shared bot)."""
    with db.transaction():
        db.run("UPDATE features SET revoked_at = ?, revoked_by = ? WHERE customer_id = ? AND feature = ? "
               "AND revoked_at IS NULL", (int(time.time()), owner, customer_id, feature))
        db.audit(f"owner:{owner}", f"feature.{feature}.off", customer_id=customer_id)
        changed = []
        if feature == "custom_bot":
            for s in db.q("SELECT id, status FROM servers WHERE customer_id = ? AND bot_mode = 'custom' "
                          "AND status != 'deleted'", (customer_id,)):
                db.run("DELETE FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (s["id"],))
                db.run("UPDATE servers SET bot_mode = 'shared', guild_id = NULL, status = CASE WHEN status = "
                       "'active' THEN 'awaiting_token' ELSE status END WHERE id = ?", (s["id"],))
                db.audit(f"owner:{owner}", "server.back_to_shared_bot", customer_id=customer_id, server_id=s["id"])
                changed.append(s["id"])
    return changed
