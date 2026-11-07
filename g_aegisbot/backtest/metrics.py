"""Performance metrics from an equity curve and a list of closed trades.

Only quantities that can be computed reliably from the data are reported. Daily statistics use the equity curve
resampled to UTC days (crypto trades every day, so 365 periods a year, risk-free rate 0).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough fall as a negative fraction (e.g. -0.18 for an 18% drawdown)."""
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("1D").last().dropna().pct_change().dropna()


def sharpe_ratio(equity: pd.Series, periods_per_year: int = 365) -> float:
    """Annualised Sharpe from daily equity returns, risk-free rate 0."""
    rets = daily_returns(equity)
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * math.sqrt(periods_per_year))


def sortino_ratio(equity: pd.Series, periods_per_year: int = 365) -> float | None:
    """Annualised Sortino: mean daily return over the downside deviation (root mean square of the negative returns,
    taken over ALL days, target return 0). None when there are fewer than 2 days or no down days at all."""
    rets = daily_returns(equity)
    if len(rets) < 2:
        return None
    downside = np.minimum(rets.to_numpy(), 0.0)
    dd = math.sqrt(float(np.mean(downside ** 2)))
    if dd == 0:
        return None
    return float(rets.mean() / dd * math.sqrt(periods_per_year))


def exposure_fraction(trades: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Share of the measured period during which at least one position was open (overlapping trades counted once)."""
    span = (end - start).total_seconds()
    if span <= 0 or not trades:
        return 0.0
    spans = sorted((max(t["entry_time"], start), min(t["exit_time"], end)) for t in trades)
    covered, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                covered += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += (cur_e - cur_s).total_seconds()
    return covered / span


def longest_streaks(trades: list[dict]) -> tuple[int, int]:
    """(longest winning streak, longest losing streak) of net PnL, in exit order."""
    best_w = best_l = run_w = run_l = 0
    ordered = sorted(trades, key=lambda x: x["exit_time"]) if all("exit_time" in t for t in trades) else trades
    for t in ordered:
        if t["pnl"] > 0:
            run_w, run_l = run_w + 1, 0
        else:
            run_l, run_w = run_l + 1, 0
        best_w, best_l = max(best_w, run_w), max(best_l, run_l)
    return best_w, best_l


def _profit_factor(pnls: np.ndarray) -> float:
    wins, losses = pnls[pnls > 0].sum(), -pnls[pnls <= 0].sum()
    if losses > 0:
        return float(wins / losses)
    return math.inf if wins > 0 else 0.0


def breakdown(trades: list[dict], key: str) -> dict[str, dict]:
    """Per-group statistics (symbol, strategy, exit reason...). Winners and losers are split both after costs (net) and
    before costs (gross), so cost drag is visible separately from the strategy's own edge."""
    out: dict[str, dict] = {}
    for name in sorted({t[key] for t in trades}):
        ts = [t for t in trades if t[key] == name]
        net = np.array([t["pnl"] for t in ts], dtype=float)
        gross = np.array([t.get("gross_pnl", t["pnl"] + t["fees"] + t["funding"]) for t in ts], dtype=float)
        hold = np.array([t["holding_hours"] for t in ts], dtype=float)
        wins, losses = net[net > 0], net[net <= 0]
        out[name] = {
            "trades": len(ts), "net_pnl": float(net.sum()), "gross_pnl": float(gross.sum()),
            "fees": float(sum(t["fees"] for t in ts)), "funding": float(sum(t["funding"] for t in ts)),
            "win_rate": float(len(wins) / len(ts)), "profit_factor": _profit_factor(net),
            "wins": int(len(wins)), "losses": int(len(losses)),
            "avg_win": float(wins.mean()) if len(wins) else 0.0, "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "expectancy": float(net.mean()), "gross_expectancy": float(gross.mean()),
            "profitable_before_costs": int((gross > 0).sum()), "losing_before_costs": int((gross <= 0).sum()),
            "avg_holding_hours": float(hold.mean()), "median_holding_hours": float(np.median(hold)),
        }
    return out


def compute_metrics(equity: pd.Series, trades: list[dict], initial: float,
                    start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict:
    start = equity.index[0] if start is None and len(equity) else start
    end = equity.index[-1] if end is None and len(equity) else end
    final = float(equity.iloc[-1]) if len(equity) else initial
    years = (end - start).total_seconds() / (365.25 * 86400) if len(equity) > 1 else 0.0
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    hold = np.array([t["holding_hours"] for t in trades if "holding_hours" in t], dtype=float)
    win_streak, loss_streak = longest_streaks(trades) if trades else (0, 0)
    return {
        "start_equity": initial,
        "end_equity": final,
        "total_return": final / initial - 1.0,
        "cagr": (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else None,
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe_ratio(equity),
        "sortino": sortino_ratio(equity),
        "trades": len(trades),
        "win_rate": float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        "profit_factor": _profit_factor(pnls) if len(pnls) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnls.mean()) if len(pnls) else 0.0,
        "gross_expectancy": float(np.mean([t.get("gross_pnl", t["pnl"] + t["fees"] + t["funding"]) for t in trades])) if trades else 0.0,
        "total_fees": float(sum(t["fees"] for t in trades)),
        "total_funding": float(sum(t["funding"] for t in trades)),
        "exposure": exposure_fraction(trades, start, end)
        if len(equity) > 1 and all("entry_time" in t and "exit_time" in t for t in trades) else 0.0,
        "avg_holding_hours": float(hold.mean()) if len(hold) else 0.0,
        "median_holding_hours": float(np.median(hold)) if len(hold) else 0.0,
        "longest_win_streak": win_streak,
        "longest_loss_streak": loss_streak,
        "trades_per_month": len(trades) / (years * 12) if years > 0 else 0.0,
    }
