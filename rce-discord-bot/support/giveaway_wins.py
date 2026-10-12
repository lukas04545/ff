"""Giveaway wins announced by another bot (e.g. Helios) in a Discord channel.

Expected message (Helios "Giveaway Winner"):
    content:  @Winner (mention)
    embed:    title "Giveaway Winner", "Winner: `username`", field "Prize" = "VIP Kit (Lifetime)"
The parser is tolerant: it reads the mention, the "Winner:" line and the "Prize" field/line from the
content, embed title/description/fields. The ticket AI uses this to find a ticket owner's wins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WINNER = re.compile(r"winners?\s*[:\-]\s*`?@?([^`\n,]+?)`?\s*(?:$|\n|,)", re.IGNORECASE)
_PRIZE_LINE = re.compile(r"prize\s*[:\-]\s*(.+)", re.IGNORECASE)
LIFETIME = re.compile(r"\b(life\s*-?\s*time|perm(?:anent)?|forever|unlimited)\b", re.IGNORECASE)


@dataclass(slots=True)
class GiveawayWin:
    message_id: int
    jump_url: str
    created_at: int                 # Unix time
    winner_ids: list[int]           # mentioned users
    winner_names: list[str]         # names from the "Winner:" line (lowercase)
    prize: str

    @property
    def lifetime(self) -> bool:
        return bool(LIFETIME.search(self.prize))

    def won_by(self, user_id: int, names: set[str]) -> bool:
        return user_id in self.winner_ids or bool(names & set(self.winner_names))


def _embed_text(embed) -> tuple[str, str | None]:
    """All text of an embed + the value of a field called "Prize" (if any)."""
    parts = [getattr(embed, "title", None) or "", getattr(embed, "description", None) or ""]
    prize = None
    for field in getattr(embed, "fields", []) or []:
        name, value = (field.name or "").strip(), (field.value or "").strip()
        parts.append(f"{name}: {value}")
        if name.lower().strip(" :🎁🏆") in ("prize", "prizes", "reward"):
            prize = value
    return "\n".join(p for p in parts if p), prize


def parse_win(message) -> GiveawayWin | None:
    """GiveawayWin from a discord.Message (or a compatible object), None if it is not a winner announcement."""
    texts, prize = [message.content or ""], None
    for embed in getattr(message, "embeds", []) or []:
        text, field_prize = _embed_text(embed)
        texts.append(text)
        prize = prize or field_prize
    full = "\n".join(texts)
    if "winner" not in full.lower():
        return None
    if prize is None:
        line = _PRIZE_LINE.search(full)
        # Helios style: "Prize" on its own line, the prize on the next line
        if line:
            prize = line.group(1).strip()
        else:
            lines = [ln.strip() for ln in full.splitlines()]
            for i, ln in enumerate(lines[:-1]):
                if ln.lower().strip(" :🎁🏆") == "prize" and lines[i + 1]:
                    prize = lines[i + 1]
                    break
    if not prize:
        return None
    names = [m.group(1).strip().lower() for m in _WINNER.finditer(full) if m.group(1).strip()]
    created = getattr(message, "created_at", None)
    return GiveawayWin(
        message_id=message.id, jump_url=getattr(message, "jump_url", ""),
        created_at=int(created.timestamp()) if created else 0,
        winner_ids=[u.id for u in getattr(message, "mentions", []) or []],
        winner_names=names, prize=prize.strip()[:200])


def match_kit(prize: str, kit_names: list[str]) -> str | None:
    """Kit named in the prize text ("VIP Kit (Lifetime)" -> "VIP"); the longest matching name wins."""
    prize_l = prize.lower()
    hits = [k for k in kit_names if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", prize_l)]
    return max(hits, key=len) if hits else None
