"""Pure checks that decide whether an open position is really protected by its exchange-side stop.

A stop only counts as protection when ALL of these hold:
  * it closes the position: the order side is the opposite of the position side;
  * it is reduce-only (or close-position), so it can never open or flip a position;
  * it is a plain STOP_MARKET (not a stop-limit, which may not fill, not a trailing stop, which moves on its own, and not a
    take-profit, which never limits a loss), in one-way mode (position side BOTH);
  * it triggers on the mark price when the exchange reports the trigger type (the bot places MARK_PRICE stops);
  * its trigger price is a positive finite number;
  * its quantity covers the WHOLE open position (or it is a close-position stop). A smaller stop is partial coverage,
    never protection. A missing quantity cannot be verified and is treated as not protected.

Several valid stops can coexist (a stale duplicate from an earlier trail, for example). The tightest one is kept as the
protection and the rest are reported as duplicates so the caller can remove them AFTER the protection is confirmed.
Orders that fail a check are reported with the reason and are never counted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

QTY_TOLERANCE = 1e-6          # relative; absorbs exchange rounding of the stop quantity


class ProtectionError(Exception):
    """One or more symbols could not be verified or protected. `failures` maps symbol to the reason."""

    def __init__(self, failures: dict[str, str]):
        self.failures = dict(failures)
        super().__init__("; ".join(f"{s}: {r}" for s, r in self.failures.items()))


@dataclass
class StopAssessment:
    valid: list[dict] = field(default_factory=list)                     # full-coverage stops, tightest first
    invalid: list[tuple[dict, str]] = field(default_factory=list)       # (order, reason it does not protect)

    @property
    def best(self) -> dict | None:
        return self.valid[0] if self.valid else None

    @property
    def duplicates(self) -> list[dict]:
        return self.valid[1:]


def _truthy(value) -> bool:
    return value is True or str(value).lower() == "true"


def assess_stops(stops: list[dict], *, position_side: str, position_qty: float) -> StopAssessment:
    """`position_side` is 'long' or 'short'. `stops` are the dicts returned by the adapters' fetch_stop_orders."""
    exit_side = "sell" if position_side == "long" else "buy"
    result = StopAssessment()
    for order in stops:
        reason = _problem(order, exit_side, position_qty)
        if reason:
            result.invalid.append((order, reason))
        else:
            result.valid.append(order)
    # tightest protection first: highest trigger for a long, lowest for a short
    result.valid.sort(key=lambda o: float(o["stopPrice"]), reverse=(position_side == "long"))
    return result


def _problem(order: dict, exit_side: str, position_qty: float) -> str | None:
    side = str(order.get("side") or "").lower()
    if side != exit_side:
        return f"wrong side ({side or 'unknown'}, a closing order must be {exit_side})"
    close_all = _truthy(order.get("closePosition"))
    if not (_truthy(order.get("reduceOnly")) or close_all):
        return "not reduce-only"
    kind = str(order.get("type") or "").lower().replace("_", "").replace("-", "")
    if "takeprofit" in kind:
        return "take-profit order (it never limits a loss)"
    position_side = str(order.get("positionSide") or "").upper()
    if position_side and position_side != "BOTH":
        return f"position side {position_side} (the bot only supports one-way mode)"
    if "trailing" in kind:
        return "trailing stop (its trigger moves by itself)"
    if kind and "market" not in kind:
        return f"not a stop-market order ({order.get('type')})"
    working = str(order.get("workingType") or "").upper()
    if working and working != "MARK_PRICE":
        return f"triggers on {working}, the bot's stops use MARK_PRICE"
    try:
        price = float(order.get("stopPrice"))
    except (TypeError, ValueError):
        return "no trigger price"
    if not math.isfinite(price) or price <= 0:
        return "invalid trigger price"
    if close_all:
        return None
    try:
        qty = float(order.get("amount"))
    except (TypeError, ValueError):
        return "quantity unknown, cannot confirm it covers the position"
    if not math.isfinite(qty) or qty <= 0:
        return "quantity unknown, cannot confirm it covers the position"
    if qty < position_qty * (1 - QTY_TOLERANCE):
        return f"insufficient quantity ({qty:g} of {position_qty:g})"
    return None
