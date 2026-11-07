"""The suggestions AI in the management Discord (HQ): customers chat about the bot, the AI collects their ideas.

In the channel CP_HQ_IDEAS_CHANNEL (default "customer-chat") an AI assistant reads what customers write:

  * an idea or wish for the bot  → saved as a suggestion (#12) – or, if it's already on the list, counted as a vote
  * "what happened to my idea?"  → the status of the suggestions (🆕 New, 📌 Planned, 🛠️ In progress, ✅ Done, ❌ Not planned)
  * a question about the bot     → answered from the bot's README (search_docs)
  * people chatting with each other or with staff → it stays silent

A board message at the top of the channel (pinned) lists every suggestion by status and is kept up to date. When the
team changes a status (in the owner panel → Suggestions, or by asking the AI in that channel as HQ staff) the channel
gets a short notice and the person who suggested it is mentioned (only them – never roles or @everyone).

Safety: customers can only add suggestions and votes; changing a status needs HQ Support/Admin. Messages to the AI are
rate limited per user, new suggestions per day too.
"""
from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from pathlib import Path

import discord

from control_plane.db import DB
from web.ratelimit import RateLimiter

STATUSES = {"new": ("🆕", "New"), "planned": ("📌", "Planned"), "in_progress": ("🛠️", "In progress"),
            "done": ("✅", "Done"), "declined": ("❌", "Not planned")}
OPEN = ("new", "planned", "in_progress")
idea_turns = RateLimiter(20, 900)               # per Discord user: messages the AI reads
new_ideas = RateLimiter(10, 86400)              # per Discord user: new suggestions a day
README = Path(__file__).resolve().parents[1] / "README.md"
SILENT = "SILENT"


# ------------------------------------------------------------------ the list

def _row(r) -> dict:
    d = dict(r)
    d["votes"] = len(json.loads(d.pop("voters") or "[]")) + 1          # the author counts too
    return d


def get(db: DB, sid: int) -> dict | None:
    r = db.one("SELECT * FROM suggestions WHERE id = ?", (int(sid),))
    return _row(r) if r else None


def add(db: DB, author_id: int, author_name: str, text: str) -> dict:
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:400]
    if len(text) < 5:
        raise ValueError("Describe the idea in a few words.")
    now = int(time.time())
    cur = db.run("INSERT INTO suggestions (author_id, author_name, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                 (int(author_id), str(author_name or "")[:64], text, now, now))
    db.audit(f"discord:{author_id}", "hq.suggestion", detail=f"#{cur.lastrowid} {text[:200]}")
    return get(db, cur.lastrowid)


def vote(db: DB, sid: int, user_id: int) -> dict:
    s = db.one("SELECT * FROM suggestions WHERE id = ?", (int(sid),))
    if s is None:
        raise ValueError(f"There's no suggestion #{sid}.")
    voters = json.loads(s["voters"] or "[]")
    if int(user_id) != s["author_id"] and int(user_id) not in voters:
        voters.append(int(user_id))
        db.run("UPDATE suggestions SET voters = ? WHERE id = ?", (json.dumps(voters), s["id"]))
    return get(db, s["id"])


def set_status(db: DB, sid: int, status: str, note: str | None, actor: str) -> dict:
    if status not in STATUSES:
        raise ValueError("Status must be one of: " + ", ".join(STATUSES))
    s = get(db, sid)
    if s is None:
        raise ValueError(f"There's no suggestion #{sid}.")
    db.run("UPDATE suggestions SET status = ?, note = ?, updated_at = ? WHERE id = ?",
           (status, s["note"] if note is None else str(note).strip()[:300], int(time.time()), s["id"]))
    db.audit(actor, "hq.suggestion_status", detail=f"#{s['id']} → {status}" + (f" ({note[:150]})" if note else ""))
    return get(db, s["id"])


def remove(db: DB, sid: int, actor: str) -> None:
    db.run("DELETE FROM suggestions WHERE id = ?", (int(sid),))
    db.audit(actor, "hq.suggestion_remove", detail=f"#{sid}")


def listing(db: DB, status: str | None = None, query: str | None = None, limit: int = 25) -> list[dict]:
    rows = [_row(r) for r in db.q("SELECT * FROM suggestions" + (" WHERE status = ?" if status else "")
                                  + " ORDER BY id DESC", (status,) if status else ())]
    if query:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        scored = [(sum(w in (r["text"] + " " + r["note"]).lower() for w in words), r) for r in rows]
        rows = [r for n, r in sorted(scored, key=lambda x: -x[0]) if n] if words else rows
    return rows[:limit]


def label(status: str) -> str:
    icon, text = STATUSES.get(status, ("•", status))
    return f"{icon} {text}"


def _line(s: dict, width: int = 90) -> str:
    text = s["text"] if len(s["text"]) <= width else s["text"][:width - 1] + "…"
    extra = f" · 👍 {s['votes']}" if s["votes"] > 1 else ""
    note = f"\n  ↳ *{s['note'][:100]}*" if s["note"] else ""
    return f"**#{s['id']}** {discord.utils.escape_mentions(text)}{extra}{note}"


def board(db: DB) -> tuple[discord.Embed, discord.ui.View]:
    """The pinned board: every open suggestion by status, the latest finished ones."""
    rows = [_row(r) for r in db.q("SELECT * FROM suggestions ORDER BY id")]
    e = discord.Embed(title="💡 Suggestions & development status", color=0xF7931E, description=(
        "Got an idea for the bot? Just write it here – the assistant adds it to this list (or counts your vote if it's "
        "on it already). Ask *\"what's the status of #12?\"* any time."))
    for status in ("in_progress", "planned", "new", "done", "declined"):
        items = [r for r in rows if r["status"] == status]
        if status in ("done", "declined"):
            items = sorted(items, key=lambda r: -r["updated_at"])[:8]
        else:
            items = sorted(items, key=lambda r: (-r["votes"], r["id"]))
        if not items:
            continue
        lines, used = [], 0
        for r in items:
            line = _line(r)
            if used + len(line) + 1 > 1000:
                lines.append(f"… and {len(items) - len(lines)} more")
                break
            lines.append(line)
            used += len(line) + 1
        e.add_field(name=f"{label(status)} ({sum(1 for r in rows if r['status'] == status)})", value="\n".join(lines),
                    inline=False)
    if not rows:
        e.add_field(name="Nothing yet", value="Be the first – write your idea below.", inline=False)
    e.set_footer(text="Updated")
    e.timestamp = discord.utils.utcnow().replace(second=0, microsecond=0)
    return e, discord.ui.View(timeout=None)


def unannounced(db: DB) -> list[dict]:
    return [_row(r) for r in db.q("SELECT * FROM suggestions WHERE status != announced ORDER BY id")]


def mark_announced(db: DB, sid: int, status: str) -> None:
    db.run("UPDATE suggestions SET announced = ? WHERE id = ?", (status, int(sid)))


# ------------------------------------------------------------------ what the bot can do (the README)

@lru_cache(maxsize=1)
def _sections() -> tuple[tuple[str, str], ...]:
    try:
        text = README.read_text(encoding="utf-8")
    except OSError:
        return ()
    out, head, buf, code = [], "Overview", [], False
    for line in text.splitlines():
        if line.startswith("```"):
            code = not code
        if not code and re.match(r"^#{1,4} ", line):
            if buf:
                out.append((head, "\n".join(buf).strip()))
            head, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    if buf:
        out.append((head, "\n".join(buf).strip()))
    return tuple(out)


def search_docs(query: str, n: int = 3) -> list[dict]:
    words = [w for w in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(w) > 2]
    if not words:
        return []
    scored = []
    for head, body in _sections():
        h, b = head.lower(), body.lower()
        score = sum(3 * (w in h) + min(b.count(w), 5) for w in words)
        if score:
            scored.append((score, head, body))
    scored.sort(key=lambda x: -x[0])
    return [{"section": head, "text": body[:1800]} for _, head, body in scored[:n]]


def features_summary() -> str:
    for head, body in _sections():
        if head.startswith("✅ Implemented"):
            return body[:4000]
    return ""


# ------------------------------------------------------------------ the AI

def _fn(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required or []}}}


TOOLS = [
    _fn("list_suggestions", "The saved suggestions (newest first) – filter by status and/or words. Use it to answer "
        "status questions and to find duplicates before adding.",
        {"status": {"type": "string", "enum": list(STATUSES)}, "query": {"type": "string"}}),
    _fn("add_suggestion", "Save a NEW idea/wish for the bot, written as one clear English sentence. Only after "
        "list_suggestions found nothing that means the same.", {"text": {"type": "string"}}, ["text"]),
    _fn("support_suggestion", "The person wants an idea that is already on the list: count their vote.",
        {"id": {"type": "integer"}}, ["id"]),
    _fn("search_docs", "Search the bot's documentation (README) to answer a question about what the bot can do and how.",
        {"query": {"type": "string"}}, ["query"]),
]
STAFF_TOOLS = [
    _fn("set_status", "HQ staff only: change a suggestion's development status (and optionally the team's note).",
        {"id": {"type": "integer"}, "status": {"type": "string", "enum": list(STATUSES)}, "note": {"type": "string"}},
        ["id", "status"]),
]


def system_prompt(staff: bool) -> str:
    return (
        "You are the assistant in the customers' chat of a hosted Discord bot for Rust Console Edition servers. "
        "Customers talk about the bot here. Your jobs:\n"
        "1) Collect suggestions: when someone proposes a feature, change or improvement (or reports something they'd "
        "like to work differently), call list_suggestions with a few key words first. If the same idea is there, "
        "call support_suggestion; otherwise add_suggestion with one short, clear English sentence. Then confirm in "
        "one line with its number and status, e.g. \"📝 Saved as #12 (🆕 New) – thanks!\". One message can hold "
        "several ideas – save each.\n"
        "2) Status: when asked what happened to an idea or what's being worked on, use list_suggestions and answer "
        "with numbers and statuses (🆕 New, 📌 Planned, 🛠️ In progress, ✅ Done, ❌ Not planned) and the team's note.\n"
        "3) Questions about the bot: answer briefly from search_docs – never invent features. If the docs don't "
        "say, tell them the team will answer here.\n"
        "4) If the message is people chatting with each other or with staff, a greeting, thanks, or anything that "
        f"needs nothing from you, reply with exactly {SILENT} and nothing else.\n"
        "Never promise a release date or that something will be built – only the team decides. Never mention "
        "@everyone, @here or roles. Never ask for passwords, tokens or payment details; account or setup problems "
        "belong in their customer ticket (#get-started). Reply in the person's language, short (at most a few lines)."
        + ("\nThe person writing is HQ STAFF: they may also change statuses with set_status." if staff else
           "\nThe person writing is a customer: they can't change statuses – the team does that.")
        + "\n\nWhat the bot already does (summary):\n" + features_summary()
    )


class IdeaTools:
    def __init__(self, db: DB, user_id: int, user_name: str, *, staff: bool = False) -> None:
        self.db, self.user_id, self.user_name, self.staff = db, int(user_id), user_name, staff
        self.changed = False                       # the board needs an update

    @property
    def tools(self) -> list[dict]:
        return TOOLS + (STAFF_TOOLS if self.staff else [])

    @staticmethod
    def _short(s: dict) -> dict:
        return {"id": s["id"], "text": s["text"], "status": label(s["status"]), "votes": s["votes"],
                "note": s["note"], "by": s["author_name"]}

    def run(self, name: str, args: dict) -> dict:
        try:
            if name == "list_suggestions":
                rows = listing(self.db, args.get("status") or None, args.get("query") or None)
                return {"ok": True, "suggestions": [self._short(s) for s in rows]}
            if name == "add_suggestion":
                if new_ideas.hit(f"idea:{self.user_id}") is not None:
                    return {"ok": False, "error": "They've added a lot today – ask them to wait until tomorrow."}
                s = add(self.db, self.user_id, self.user_name, str(args.get("text") or ""))
                self.changed = True
                return {"ok": True, **self._short(s)}
            if name == "support_suggestion":
                s = vote(self.db, int(args.get("id") or 0), self.user_id)
                self.changed = True
                return {"ok": True, **self._short(s)}
            if name == "search_docs":
                return {"ok": True, "results": search_docs(str(args.get("query") or ""))}
            if name == "set_status" and self.staff:
                note = args.get("note")
                s = set_status(self.db, int(args.get("id") or 0), str(args.get("status") or ""),
                               None if note is None else str(note), f"discord:{self.user_id}")
                self.changed = True
                return {"ok": True, **self._short(s), "note": "The channel gets a notice by itself."}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"{name} isn't available."}


async def answer(client, tools: IdeaTools, history: list[dict], text: str, *, max_rounds: int = 5) -> str:
    """One chat message → the reply ("" = stay silent)."""
    msgs = [{"role": "system", "content": system_prompt(tools.staff)}, *history[-12:],
            {"role": "user", "content": f"{tools.user_name or 'someone'}: {text[:1500]}"}]
    for _ in range(max_rounds):
        out = await client.chat(msgs, tools=tools.tools, json_mode=False, max_tokens=600)
        calls = out.get("tool_calls") or []
        if not calls:
            reply = (out.get("content") or "").strip()
            return "" if not reply or reply.strip(" .`*") == SILENT else reply
        msgs.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": calls})
        for call in calls[:6]:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = tools.run(str(fn.get("name") or ""), args if isinstance(args, dict) else {})
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result)[:3500]})
    return ""
