"""Development screen for the slow-timeframe study (docs/STRATEGY_ACCEPTANCE.md, section 6.5).

Applies the pre-declared promotion rules to the output of ONE backtest run, mechanically, so no judgement enters the
decision. It refuses anything that is not exactly the registered candidate run on exactly the development period:

    python -m g_aegisbot.backtest.devscreen --candidate S-B4 --run DIR [--stress-run DIR]

DIR is the folder that holds summary.json, trades.csv, equity.csv and manifest.json for the run.

Rules (point estimates, development period 2021-11-06 to 2024-05-07, completed trades only):
  P1 feasibility  at least 600 completed trades (the confirmation period needs 300 and has roughly half the pair-days)
  P2              net expectancy per trade above 0                                   (C2, point estimate)
  P3              profit factor of at least 1.30                                     (C3, point estimate)
  P4              maximum drawdown of the daily marked-to-market equity at most 25%  (C4, point estimate)
  P5              stress run (slippage x2, taker fee x1.2): net expectancy above 0 and profit factor above 1.0 (C6)
A candidate that fails P1 is INFEASIBLE_UNDER_C1: no evidence about the edge either way, and it cannot be promoted. This
module is not part of the backtest import closure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from g_aegisbot.backtest import safeio
from g_aegisbot.backtest.acceptance_methods import daily_returns, net_pnl, split_by_period
from g_aegisbot.backtest.manifest import digest
from g_aegisbot.costs import TAKER_FEE
from g_aegisbot.engine.strategy import StrategyParams

DEV_START = pd.Timestamp("2021-11-06", tz="UTC")
DEV_END = pd.Timestamp("2024-05-07", tz="UTC")
MIN_TRADES = 600
MIN_PROFIT_FACTOR = 1.30
MAX_DRAWDOWN = 0.25
STRESS_SLIPPAGE_SCALE = 2.0
STRESS_FEE_SCALE = 1.2

# candidate id -> (strategies, timeframe). S-C* (combined) run only if both components of that timeframe are promoted.
CANDIDATES = {
    "S-B4": (("breakout",), "4h"), "S-T4": (("trend",), "4h"),
    "S-B1D": (("breakout",), "1d"), "S-T1D": (("trend",), "1d"),
    "S-C4": (("breakout", "trend"), "4h"), "S-C1D": (("breakout", "trend"), "1d"),
}


class ScreenError(ValueError):
    """The run is not an acceptable input for this screen. A refusal, never a verdict."""


def registered_params_sha256(candidate: str) -> str:
    strategies, timeframe = CANDIDATES[candidate]
    return digest(StrategyParams(timeframe=timeframe, strategies=strategies))


def _json(path: Path) -> dict:
    return json.loads(safeio.read_bytes(path, 64 << 20).decode("utf-8"))


def load_run(run_dir: str | Path) -> dict:
    d = Path(run_dir)
    out = {"summary": _json(d / "summary.json"), "manifest": _json(d / "manifest.json")}
    t = pd.read_csv(safeio.guard_read(d / "trades.csv"), parse_dates=["entry_time", "exit_time"])
    for c in ("entry_time", "exit_time"):
        t[c] = pd.to_datetime(t[c], utc=True)
    out["trades"] = t.to_dict("records")
    e = pd.read_csv(safeio.guard_read(d / "equity.csv"), index_col=0, parse_dates=True)
    out["equity"] = e["equity"]
    return out


def check_identity(run: dict, candidate: str, *, stress: bool) -> dict:
    """Refuse a run that is not exactly the registered candidate on exactly the development period. Returns a record of what
    was checked (commit, code hash) for the report."""
    if candidate not in CANDIDATES:
        raise ScreenError(f"unknown candidate {candidate!r}; registered: {', '.join(CANDIDATES)}")
    strategies, timeframe = CANDIDATES[candidate]
    m, s = run["manifest"], run["summary"]
    if m.get("synthetic"):
        raise ScreenError("a synthetic run cannot be screened")
    if m["strategy"]["enabled"] != list(strategies):
        raise ScreenError(f"the run enabled {m['strategy']['enabled']}, the candidate is {list(strategies)}")
    if m["strategy"]["params_sha256"] != registered_params_sha256(candidate):
        raise ScreenError("the run's strategy parameters differ from the registered candidate (parameter hash mismatch)")
    cfg = m["config"]
    if cfg["funding_mode"] != "historical":
        raise ScreenError("the screen needs historical funding")
    want_slip, want_fee = (STRESS_SLIPPAGE_SCALE, TAKER_FEE * STRESS_FEE_SCALE) if stress else (1.0, TAKER_FEE)
    if cfg["slippage_scale"] != want_slip or abs(cfg["taker_fee"] - want_fee) > 1e-12:
        raise ScreenError(f"{'stress' if stress else 'base'} run needs slippage scale {want_slip} and taker fee {want_fee}, "
                          f"got {cfg['slippage_scale']} and {cfg['taker_fee']}")
    if cfg["use_breaker"] is not True:
        raise ScreenError("the daily drawdown breaker must be on (production risk setting)")
    period = s["notes"]["measurement_period"]
    start, end = pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
    if start != DEV_START or end != DEV_END:
        raise ScreenError(f"the measured period is {start} to {end}; the development period is {DEV_START} to {DEV_END}. "
                          "Nothing else may be screened here.")
    if not m["eligibility"]["data_integrity_passed"]:
        raise ScreenError("the run's data integrity checks did not pass")
    if not m["code"].get("committed", {}).get("matches"):
        raise ScreenError("the code that ran is not exactly a committed version (run from a clean checkout at the tag)")
    return {"commit": m["code"]["git"].get("commit"), "code_sha256": m["code"]["sha256"], "experiment_id": m["experiment_id"],
            "timeframe": timeframe}


def run_statistics(run: dict) -> dict:
    parts = split_by_period(run["trades"], DEV_START, DEV_END)
    done = parts["completed"]
    pnl = np.array([net_pnl(t) for t in done], dtype=float)
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl <= 0].sum()
    pf = float(wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)
    r = daily_returns(run["equity"], float(run["summary"]["metrics"]["start_equity"]))
    path = np.concatenate([[1.0], np.cumprod(1.0 + r)])
    dd = float(-(path / np.maximum.accumulate(path) - 1.0).min())
    return {"completed_trades": int(len(done)), "truncated_trades": int(len(parts["truncated"])),
            "net_expectancy": float(pnl.mean()) if len(pnl) else 0.0, "profit_factor": pf, "max_drawdown_daily": dd}


def _rule(name, value, threshold, ok) -> dict:
    return {"rule": name, "value": value, "threshold": threshold, "pass": bool(ok)}


def screen(base_dir: str | Path, candidate: str, stress_dir: str | Path | None = None) -> dict:
    base = load_run(base_dir)
    ident = check_identity(base, candidate, stress=False)
    st = run_statistics(base)
    rules = [
        _rule("P1 completed trades", st["completed_trades"], f">= {MIN_TRADES}", st["completed_trades"] >= MIN_TRADES),
        _rule("P2 net expectancy per trade", st["net_expectancy"], "> 0", st["net_expectancy"] > 0),
        _rule("P3 profit factor", st["profit_factor"], f">= {MIN_PROFIT_FACTOR}", st["profit_factor"] >= MIN_PROFIT_FACTOR),
        _rule("P4 max drawdown (daily equity)", st["max_drawdown_daily"], f"<= {MAX_DRAWDOWN}", st["max_drawdown_daily"] <= MAX_DRAWDOWN),
    ]
    out = {"candidate": candidate, "identity": ident, "statistics": st, "rules": rules}
    if not rules[0]["pass"]:
        out["verdict"] = "INFEASIBLE_UNDER_C1"
    elif not all(r["pass"] for r in rules):
        out["verdict"] = "REJECT"
    elif stress_dir is None:
        out["verdict"] = "STRESS_RUN_NEEDED"
    else:
        stress = load_run(stress_dir)
        check_identity(stress, candidate, stress=True)
        ss = run_statistics(stress)
        p5 = _rule("P5 stress: net expectancy > 0 and profit factor > 1.0", {"net_expectancy": ss["net_expectancy"], "profit_factor": ss["profit_factor"]},
                   "> 0 and > 1.0", ss["net_expectancy"] > 0 and ss["profit_factor"] > 1.0)
        out["rules"].append(p5)
        out["verdict"] = "PROMOTE_TO_VALIDATION" if p5["pass"] else "REJECT"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Development screen for the slow-timeframe candidates")
    ap.add_argument("--candidate", required=True, choices=sorted(CANDIDATES))
    ap.add_argument("--run", required=True)
    ap.add_argument("--stress-run")
    a = ap.parse_args(argv)
    try:
        res = screen(a.run, a.candidate, a.stress_run)
    except (ScreenError, safeio.UnsafePathError, OSError, KeyError, ValueError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
