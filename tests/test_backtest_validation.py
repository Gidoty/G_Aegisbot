"""Backtester accounting, exit categories, liquidation model, historical funding and the extra metrics."""
import math

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest.funding import FundingSchedule
from g_aegisbot.backtest.liquidation import LiquidationModel, MarginBracket
from g_aegisbot.backtest.metrics import (
    breakdown, compute_metrics, daily_returns, exposure_fraction, longest_streaks, sortino_ratio,
)
from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.engine.strategy import StrategyParams
from tests.test_backtest import BASE, SYM, breakout_frame
from tests.trendfx import candles


def run(frames, **kw):
    return Backtester(frames, BacktestConfig(**kw)).run()


def assert_books_balance(res, initial=10_000.0):
    assert res.metrics["end_equity"] == pytest.approx(initial + sum(t["pnl"] for t in res.trades), rel=1e-9)
    for t in res.trades:
        gross = t.get("gross_pnl", t["pnl"] + t["fees"] + t["funding"])
        assert t["pnl"] == pytest.approx(gross - t["fees"] - t["funding"])


# ---- exit categories
def test_crash_on_entry_bar_is_an_initial_stop():
    frame = breakout_frame([(121.0, 121.0, 90.0, 95.0), (95, 96, 94, 95)])
    res = run({SYM: frame})
    assert [t["reason"] for t in res.trades] == ["initial-stop"]
    assert_books_balance(res)


def test_ratcheted_stop_is_a_trailing_stop_and_data_end_is_labelled():
    rise = [(121 + j, 122 + j, 120.8 + j, 121.5 + j) for j in range(12)]
    res = run({SYM: breakout_frame(rise + [(133, 133, 100, 110)])})
    assert res.trades[0]["reason"] == "trailing-stop"
    res2 = run({SYM: breakout_frame([(121, 121.3, 120.8, 121.0)] * 2)})
    assert res2.trades[0]["reason"] == "end-of-data"
    assert set(res.by_exit) == {"trailing-stop"}


def test_trades_carry_strategy_and_breakdowns_split_net_and_gross():
    res = run({SYM: breakout_frame([(121.0, 121.0, 90.0, 95.0), (95, 96, 94, 95)])})
    t = res.trades[0]
    assert t["strategy"] == "breakout"
    row = res.by_strategy["breakout"]
    assert row["trades"] == 1 and row["net_pnl"] == pytest.approx(t["pnl"])
    assert row["gross_pnl"] == pytest.approx(t["pnl"] + t["fees"] + t["funding"])


# ---- liquidation
def test_flat_bracket_liquidation_prices():
    m = LiquidationModel()
    long_p = m.liquidation_price("buy", 100.0, 1.0, 3)
    short_p = m.liquidation_price("sell", 100.0, 1.0, 3)
    assert long_p == pytest.approx((100 - 100 / 3) / (1 - 0.005))
    assert short_p == pytest.approx((100 / 3 + 100) / (1 + 0.005))
    assert long_p < 100 < short_p


def test_higher_mmr_and_cum_bracket_is_used_for_bigger_notional():
    m = LiquidationModel((MarginBracket(1_000, 0.004, 0.0), MarginBracket(math.inf, 0.01, 2.0)))
    assert m.bracket(500).mmr == 0.004 and m.bracket(5_000).mmr == 0.01
    small = m.liquidation_price("buy", 100.0, 5.0, 3)       # notional 500
    big = m.liquidation_price("buy", 100.0, 50.0, 3)        # notional 5000
    assert big > small                                       # closer to entry, so liquidated sooner
    assert big == pytest.approx((5000 - 5000 / 3 - 2.0) / (50 * 0.99))


def test_invalid_brackets_are_rejected():
    with pytest.raises(ValueError):
        LiquidationModel((MarginBracket(1_000, 0.004),))
    with pytest.raises(ValueError):
        LiquidationModel((MarginBracket(math.inf, 1.5),))


def test_liquidation_forfeits_the_whole_margin_when_a_gap_jumps_past_the_stop():
    # 1.5% risk at 3x leverage can still be gapped through: a bar opening at a tiny fraction of entry
    frame = breakout_frame([(121, 121.3, 120.8, 121.0), (5.0, 5.2, 4.9, 5.0), (5, 5, 5, 5)])
    res = run({SYM: frame})
    t = res.trades[0]
    assert t["reason"] == "liquidation"
    margin = t["amount"] * t["entry_price"] / 3
    assert t["exit_price"] == pytest.approx(LiquidationModel().liquidation_price("buy", t["entry_price"], t["amount"], 3))
    assert t["pnl"] == pytest.approx(-(margin + t["fees"] + t["funding"]))      # margin gone, plus entry fee and funding
    assert_books_balance(res)


# ---- funding
def sched(rates, start="2026-01-01", step_h=8):
    ts = [int((pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=step_h * i)).timestamp() * 1000) for i in range(len(rates))]
    return FundingSchedule(ts, list(rates))


def test_funding_schedule_lookup_and_validation():
    s = sched([0.0001, -0.0002, 0.0003])
    assert [r for _, r in s.events_in(s.ts[0], s.ts[2])] == [0.0001, -0.0002]
    assert s.problems(s.ts[0], s.ts[2]) == []
    assert FundingSchedule([], []).problems(0, 1) == ["no funding events at all"]
    bad = FundingSchedule([s.ts[0], s.ts[0]], [0.0, 0.0])
    assert any("ascending" in p for p in bad.problems(s.ts[0], s.ts[0] + 1))
    huge = FundingSchedule(s.ts, [0.5, 0.0, 0.0])
    assert any("implausibly" in p for p in huge.problems(s.ts[0], s.ts[2]))
    gap = FundingSchedule([s.ts[0], s.ts[0] + 30 * 3_600_000], [0.0, 0.0])
    assert any("gap" in p for p in gap.problems(gap.ts[0], gap.ts[1]))


def test_historical_mode_without_history_refuses_to_run():
    with pytest.raises(ValueError, match="no funding history"):
        Backtester({SYM: breakout_frame([(121, 121.3, 120.8, 121.0)] * 3)}, BacktestConfig(funding_mode="historical"))


def test_historical_mode_with_incomplete_history_refuses_unless_allowed():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0)] * 3)
    short = FundingSchedule([int(frame.index[0].timestamp() * 1000)], [0.0001])
    with pytest.raises(ValueError, match="incomplete"):
        Backtester({SYM: frame}, BacktestConfig(funding_mode="historical", funding_history={SYM: short}))
    bt = Backtester({SYM: frame}, BacktestConfig(funding_mode="historical", funding_history={SYM: short}, funding_allow_gaps=True))
    assert SYM in bt.funding_gaps


def full_schedule(frame, rate):
    start = int(frame.index[0].timestamp() * 1000) - 8 * 3_600_000
    n = int((frame.index[-1] - frame.index[0]).total_seconds() // (8 * 3600)) + 4
    return FundingSchedule([start + i * 8 * 3_600_000 for i in range(n)], [rate] * n)


def test_positive_funding_costs_longs_and_negative_pays_them():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0)] * 40)
    base = run({SYM: frame}, funding_mode="none").trades[0]["funding"]
    pos = run({SYM: frame}, funding_mode="historical", funding_history={SYM: full_schedule(frame, 0.0005)}).trades[0]["funding"]
    neg = run({SYM: frame}, funding_mode="historical", funding_history={SYM: full_schedule(frame, -0.0005)}).trades[0]["funding"]
    assert base == 0.0 and pos > 0 and neg < 0
    assert pos == pytest.approx(-neg)


# ---- metrics
def test_streaks_and_exposure_overlap_counted_once():
    t = lambda pnl, h: {"pnl": pnl, "exit_time": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=h)}
    assert longest_streaks([t(1, 1), t(2, 2), t(3, 3), t(-1, 4), t(-1, 5)]) == (3, 2)
    s, e = pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC")
    h = lambda a, b: {"entry_time": s + pd.Timedelta(hours=a), "exit_time": s + pd.Timedelta(hours=b)}
    assert exposure_fraction([h(0, 6), h(3, 9)], s, e) == pytest.approx(9 / 24)
    assert exposure_fraction([], s, e) == 0.0


def test_sortino_needs_downside_and_matches_manual():
    idx = pd.date_range("2026-01-01", periods=6, freq="1D", tz="UTC")
    up = pd.Series([100, 101, 102, 103, 104, 105.0], index=idx)
    assert sortino_ratio(up) is None
    eq = pd.Series([100, 102, 99, 103, 101, 104.0], index=idx)
    r = daily_returns(eq).to_numpy()
    dd = math.sqrt(np.mean(np.minimum(r, 0) ** 2))
    assert sortino_ratio(eq) == pytest.approx(r.mean() / dd * math.sqrt(365))


def test_breakdown_groups_and_profit_factor():
    tr = [
        {"symbol": "A", "pnl": 10.0, "fees": 1.0, "funding": 0.0, "holding_hours": 2},
        {"symbol": "A", "pnl": -4.0, "fees": 1.0, "funding": 0.0, "holding_hours": 4},
        {"symbol": "B", "pnl": -2.0, "fees": 0.5, "funding": 0.5, "holding_hours": 6},
    ]
    b = breakdown(tr, "symbol")
    assert b["A"]["trades"] == 2 and b["A"]["profit_factor"] == pytest.approx(10 / 4)
    assert b["A"]["gross_pnl"] == pytest.approx(11 + -3)   # (10+1)+(-4+1)
    assert b["B"]["win_rate"] == 0.0 and b["B"]["median_holding_hours"] == 6


# ---- trend strategy inside the simulator
def trend_series():
    rets = [0.002] * 560 + [-0.004] * 5 + [0.012] + [0.002] * 10 + [-0.05, -0.03, 0.0]
    return candles(rets)


def test_trend_trades_are_tagged_and_books_balance_for_strategy_runs():
    res = run({SYM: trend_series()}, strategies=("trend",), params=StrategyParams(strategies=("trend",)))
    assert res.trades, "the constructed pullback should have produced a trend entry"
    assert {t["strategy"] for t in res.trades} == {"trend"}
    assert {t["reason"] for t in res.trades} <= {"initial-stop", "trailing-stop", "strategy-exit", "end-of-data", "liquidation"}
    assert_books_balance(res)


def test_breakout_only_run_never_opens_trend_trades():
    res = run({SYM: trend_series()}, strategies=("breakout",))
    assert all(t["strategy"] == "breakout" for t in res.trades)


def test_equity_and_trades_are_deterministic():
    f = {SYM: trend_series()}
    a, b = run(f, strategies=("breakout", "trend")), run(f, strategies=("breakout", "trend"))
    assert a.trades == b.trades and a.equity.equals(b.equity)
