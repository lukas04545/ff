"""Price list for custom kits (prices.txt): quotes a requested item list, checks the caps.

Used by the ticket AI (tell the player the price) and by the Tip4Serv auto-approve (does the payment cover it?).
The file format is described at the top of prices.txt.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PriceRule:
    items: frozenset[str]
    price: float
    per: int
    extras: frozenset[str] = frozenset()   # included for free, up to the amount of the rule's items


@dataclass
class Cap:
    limit: int
    items: frozenset[str]
    each: bool


@dataclass
class PriceList:
    currency: str = "EUR"
    rules: list[PriceRule] = field(default_factory=list)
    caps: list[Cap] = field(default_factory=list)
    timers: dict[int, float] = field(default_factory=dict)   # claim timer hours -> price
    warnings: list[str] = field(default_factory=list)        # lines that could not be read

    def rule_for(self, short: str) -> PriceRule | None:
        return next((r for r in self.rules if short in r.items), None)

    @property
    def default_timer(self) -> int | None:
        free = [h for h, p in self.timers.items() if p == 0]
        return max(free) if free else None


@dataclass
class Quote:
    total: float
    currency: str
    lines: list[dict]                 # {"item", "amount", "price"} (price None = no price)
    unpriced: list[str]               # shortnames without a price -> staff has to decide
    over_cap: list[str]               # human readable cap violations
    timer_hours: int | None
    timer_price: float

    @property
    def complete(self) -> bool:
        return not self.unpriced and not self.over_cap


_NUM = r"(\d+(?:[.,]\d+)?)"


def _items(text: str) -> frozenset[str]:
    return frozenset(i.strip().lower() for i in text.split(",") if i.strip())


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse(text: str) -> PriceList:
    pl = PriceList()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if m := re.fullmatch(r"currency\s+([a-z]{3})", low):
            pl.currency = m.group(1).upper()
        elif m := re.fullmatch(r"timer\s+(\d+)\s*h?\s*=\s*" + _NUM, low):
            pl.timers[int(m.group(1))] = _num(m.group(2))
        elif m := re.fullmatch(r"cap\s+([\d_.,]+)\s*(each)?\s*:\s*(.+)", low):
            limit = int(re.sub(r"[_.,]", "", m.group(1)))
            pl.caps.append(Cap(limit, _items(m.group(3)), bool(m.group(2))))
        elif m := re.fullmatch(r"(.+?)=\s*" + _NUM + r"\s*/\s*([\d_.,]+)(?:\s+with\s+(.+))?", low):
            per = int(re.sub(r"[_.,]", "", m.group(3)))
            if per < 1:
                pl.warnings.append(f"line {n}: amount must be at least 1")
                continue
            pl.rules.append(PriceRule(_items(m.group(1)), _num(m.group(2)), per, _items(m.group(4) or "")))
        else:
            pl.warnings.append(f"line {n}: not understood: {raw.strip()[:80]}")
    return pl


def load(path: Path) -> PriceList | None:
    """None if there is no price list (file missing or no prices in it)."""
    try:
        pl = parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return pl if pl.rules else None


def quote(pl: PriceList, items: list[dict], timer_hours: int | None = None) -> Quote:
    """items: [{"short", "amount"}]. Prices per rule are rounded up to full bundles."""
    amounts: dict[str, int] = {}
    for i in items:
        amounts[i["short"]] = amounts.get(i["short"], 0) + int(i["amount"])

    over_cap = []
    for cap in pl.caps:
        if cap.each:
            for short in sorted(cap.items):
                if amounts.get(short, 0) > cap.limit:
                    over_cap.append(f"{short}: max {cap.limit:,} (requested {amounts[short]:,})")
        else:
            total = sum(amounts.get(s, 0) for s in cap.items)
            if total > cap.limit:
                names = ", ".join(sorted(s for s in cap.items if s in amounts))
                over_cap.append(f"{names}: max {cap.limit:,} together (requested {total:,})")

    # Free extras ("with"): covered up to the amount of the rule's main items
    remaining = dict(amounts)
    free: dict[str, int] = {}
    for rule in pl.rules:
        if not rule.extras:
            continue
        budget = sum(amounts.get(s, 0) for s in rule.items)
        for extra in sorted(rule.extras):
            take = min(budget, remaining.get(extra, 0))
            if take:
                free[extra] = free.get(extra, 0) + take
                remaining[extra] -= take

    total, lines, unpriced = 0.0, [], []
    per_rule: dict[int, int] = {}
    for short, amount in amounts.items():
        if free.get(short):
            lines.append({"item": short, "amount": free[short], "price": 0.0, "note": "included"})
        left = remaining.get(short, 0)
        if not left:
            continue
        rule = pl.rule_for(short)
        if rule is None:
            unpriced.append(short)
            lines.append({"item": short, "amount": left, "price": None})
            continue
        per_rule[id(rule)] = per_rule.get(id(rule), 0) + left
        lines.append({"item": short, "amount": left, "price": round(left / rule.per * rule.price, 2)})
    for rule in pl.rules:                        # bundles per rule (items of one rule share bundles)
        if id(rule) in per_rule:
            total += math.ceil(per_rule[id(rule)] / rule.per) * rule.price

    timer_price = 0.0
    if pl.timers:
        timer_hours = timer_hours or pl.default_timer
        if timer_hours not in pl.timers:
            over_cap.append(f"claim timer {timer_hours}h is not available – options: "
                            + ", ".join(f"{h}h" for h in sorted(pl.timers, reverse=True)))
        else:
            timer_price = pl.timers[timer_hours]
    else:
        timer_hours = None
    return Quote(round(total + timer_price, 2), pl.currency, lines, unpriced, over_cap, timer_hours, timer_price)


def format_money(amount: float, currency: str) -> str:
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency)
    text = f"{amount:,.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}"
