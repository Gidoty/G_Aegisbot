"""Per-strategy performance inside a combined backtest: each strategy gets its own equity curve (its realised net PnL plus
the open profit and loss of its own positions, after its own fees and funding), so its drawdown, Sharpe and return are
visible and the strategies together add up exactly to the portfolio."""
from dataclasses import replace

import pandas as pd
import pytest

from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.engine.strategy import StrategyParams
from tests.test_strategy_priority import BREAKOUT, DEFAULT, TREND, mixed_frames

KEYS = {"equity_return", "max_drawdown", "sharpe", "sortino", "final_equity_attributed"}


def run_mixed(**kw):
    params = replace(StrategyParams(), strategies=(BREAKOUT, TREND), strategy_priority=DEFAULT)
    return Backtester(mixed_frames(), BacktestConfig(params=params, **kw)).run()


def test_every_enabled_strategy_reports_its_own_curve_statistics():
    res = run_mixed()
    assert set(res.by_strategy) == {BREAKOUT, TREND}
    for sid in (BREAKOUT, TREND):
        assert KEYS <= set(res.by_strategy[sid]), sid
        assert res.by_strategy[sid]["max_drawdown"] <= 0.0


def test_the_strategies_add_up_to_the_portfolio_exactly():
    res = run_mixed()
    initial = res.metrics["start_equity"]
    attributed = sum(res.by_strategy[s]["final_equity_attributed"] - initial for s in (BREAKOUT, TREND))
    assert attributed == pytest.approx(res.metrics["end_equity"] - initial, rel=1e-9, abs=1e-9)
    for sid in (BREAKOUT, TREND):
        net = sum(t["pnl"] for t in res.trades if t["strategy"] == sid)
        assert res.by_strategy[sid]["final_equity_attributed"] == pytest.approx(initial + net, rel=1e-9)
        assert res.by_strategy[sid]["equity_return"] == pytest.approx(net / initial, rel=1e-9)


def test_a_strategy_with_no_trades_reports_a_flat_curve():
    res = Backtester(mixed_frames(), BacktestConfig(params=replace(StrategyParams(), strategies=(BREAKOUT, TREND)),
                                                    strategies=(BREAKOUT,))).run()
    assert set(res.by_strategy) == {BREAKOUT}
    res2 = run_mixed()
    flat = Backtester({"B0/USDT:USDT": mixed_frames()["B0/USDT:USDT"]},
                      BacktestConfig(params=replace(StrategyParams(), strategies=(BREAKOUT, TREND)))).run()
    assert flat.by_strategy[TREND]["trades"] == 0 and flat.by_strategy[TREND]["equity_return"] == 0.0
    assert flat.by_strategy[TREND]["max_drawdown"] == 0.0 and res2.by_strategy[TREND]["trades"] >= 1


def test_the_added_statistics_do_not_change_the_portfolio_result():
    a = run_mixed()
    b = run_mixed()
    assert a.trades == b.trades and a.equity.equals(b.equity) and a.metrics == b.metrics
