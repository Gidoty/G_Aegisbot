"""Performance metrics from an equity curve and a list of closed trades."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough fall as a negative fraction (e.g. -0.18 for an 18% drawdown)."""
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def sharpe_ratio(equity: pd.Series, periods_per_year: int = 365) -> float:
    """Annualised Sharpe from daily equity returns, risk-free rate 0. Crypto trades every day, so 365."""
    daily = equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * math.sqrt(periods_per_year))


def compute_metrics(equity: pd.Series, trades: list[dict], initial: float) -> dict:
    end = float(equity.iloc[-1]) if len(equity) else initial
    years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400) if len(equity) > 1 else 0.0
    total_return = end / initial - 1.0
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = math.inf if gross_win > 0 else 0.0
    return {
        "start_equity": initial,
        "end_equity": end,
        "total_return": total_return,
        "cagr": (end / initial) ** (1 / years) - 1 if years > 0 and end > 0 else None,
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe_ratio(equity),
        "trades": len(trades),
        "win_rate": float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        "profit_factor": profit_factor,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnls.mean()) if len(pnls) else 0.0,
        "total_fees": float(sum(t["fees"] for t in trades)),
        "total_funding": float(sum(t["funding"] for t in trades)),
    }
