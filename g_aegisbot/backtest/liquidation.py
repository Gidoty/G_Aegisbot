"""Isolated-margin liquidation price with maintenance-margin brackets.

Derived from the isolated-margin equation: a position is liquidated when its equity falls to the maintenance margin,

    equity = WB + qty * (P - entry) * side
    maintenance margin = qty * P * MMR - cum              (MMR and cum come from the notional bracket)

    long : P = (qty*entry - WB - cum) / (qty * (1 - MMR))
    short: P = (WB + qty*entry + cum) / (qty * (1 + MMR))       with WB = qty*entry/leverage (the initial margin)

What this is NOT: an exact Binance liquidation simulation. It uses LAST-traded candle prices (Binance liquidates on the
MARK price), picks the bracket from the notional at ENTRY, ignores cross-margin effects, and on liquidation assumes the whole
isolated margin is forfeited (the remainder goes to the insurance fund as clearance fee), which is the conservative case.

The default is ONE flat 0.5% bracket, the simplification the backtester always used. Exact tiers differ per symbol and must
be supplied from Binance (`--liq-brackets file.json`); this code does not invent them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from g_aegisbot.backtest import safeio


@dataclass(frozen=True)
class MarginBracket:
    notional_cap: float          # this bracket applies to notional up to this value (inf for the last one)
    mmr: float                   # maintenance margin rate
    cum: float = 0.0             # Binance's "maintenance amount" that keeps the margin continuous between brackets


@dataclass(frozen=True)
class LiquidationModel:
    brackets: tuple[MarginBracket, ...] = (MarginBracket(math.inf, 0.005, 0.0),)
    source: str = "flat 0.5% maintenance margin (simplified; NOT Binance's tiered brackets)"

    def __post_init__(self):
        caps = [b.notional_cap for b in self.brackets]
        if not self.brackets or caps != sorted(caps) or not math.isinf(caps[-1]):
            raise ValueError("brackets must be ascending by notional_cap and end with an unbounded bracket")
        if any(not 0 < b.mmr < 1 for b in self.brackets):
            raise ValueError("every maintenance margin rate must be between 0 and 1")

    def bracket(self, notional: float) -> MarginBracket:
        return next(b for b in self.brackets if notional <= b.notional_cap)

    def liquidation_price(self, side: str, entry: float, qty: float, leverage: int) -> float:
        wb = qty * entry / leverage
        b = self.bracket(qty * entry)
        if side == "buy":
            return max(0.0, (qty * entry - wb - b.cum) / (qty * (1 - b.mmr)))
        return (wb + qty * entry + b.cum) / (qty * (1 + b.mmr))

    @classmethod
    def from_rows(cls, rows: list[dict], source: str) -> "LiquidationModel":
        """rows: [{"notional_cap": 50000, "mmr": 0.004, "cum": 0}, ..., {"notional_cap": null, "mmr": 0.5, "cum": 1234}]"""
        if not isinstance(rows, list) or not rows:
            raise ValueError("liquidation brackets must be a non-empty list of rows")
        try:
            brackets = tuple(MarginBracket(math.inf if r.get("notional_cap") in (None, "inf") else float(r["notional_cap"]),
                                           float(r["mmr"]), float(r.get("cum", 0.0))) for r in rows)
        except (TypeError, ValueError, KeyError, AttributeError):
            # fixed text on purpose: the offending value came from a file and is not echoed back
            raise ValueError("liquidation brackets: a row is malformed (each needs numeric mmr, and notional_cap and cum "
                             "as numbers; the last notional_cap may be null)") from None
        return cls(brackets, source)

    @classmethod
    def from_file(cls, path: str | Path) -> "LiquidationModel":
        return cls.from_rows(safeio.read_json(path), f"brackets from {Path(path).name}")


def load_liquidation_config(path: str | Path) -> tuple["LiquidationModel", dict[str, "LiquidationModel"]]:
    """A JSON file with either one list of brackets (used for every symbol) or an object mapping symbols to lists,
    with an optional "default" key. Symbols not listed and no default fall back to the flat simplified model."""
    raw = safeio.read_json(path)          # protected-path, link, size and strict-JSON checks happen before anything is parsed
    name = Path(path).name
    if isinstance(raw, list):
        return LiquidationModel.from_rows(raw, f"brackets from {name}"), {}
    if not isinstance(raw, dict):
        raise ValueError("the liquidation file must be a list of rows or an object of lists")
    default = LiquidationModel.from_rows(raw["default"], f"brackets from {name} (default)") if "default" in raw else LiquidationModel()
    return default, {s: LiquidationModel.from_rows(rows, f"brackets from {name} ({s})") for s, rows in raw.items() if s != "default"}
