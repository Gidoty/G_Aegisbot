"""Strategy B (trend-following pullback): each rule is broken on its own, both directions, plus look-ahead and exits."""
import pandas as pd
import pytest

from g_aegisbot.engine.strategies import (
    BREAKOUT, TREND, evaluate_symbol, evaluate_trend_at, exit_signal, validate_strategies,
)
from g_aegisbot.engine.strategy import StrategyParams, compute_indicators
from tests.trendfx import candles, trend_frame

P = StrategyParams()


def run(df, p=P):
    ind = compute_indicators(df, p)
    return evaluate_trend_at("X", ind, len(ind) - 1, p), ind


@pytest.mark.parametrize("direction,side", [(1, "buy"), (-1, "sell")])
def test_valid_setup_signals_in_both_directions(direction, side):
    res, ind = run(trend_frame(direction=direction))
    sig = res.signal
    assert sig is not None, res.reason
    assert sig.strategy == TREND and sig.side == side and sig.conviction == "normal"
    dist = P.trend_stop_atr_mult * sig.atr
    assert sig.stop_price == pytest.approx(sig.entry_price - dist if side == "buy" else sig.entry_price + dist)
    assert sig.trail_atr_mult == P.trend_trail_atr_mult
    assert P.normal_pct[0] <= sig.size_pct <= P.normal_pct[1]


@pytest.mark.parametrize("direction", [1, -1])
def test_no_pullback_means_no_signal(direction):
    res, _ = run(trend_frame(direction=direction, pullback=(), confirm_ret=0.002))
    assert res.signal is None


def test_pullback_that_breaks_the_trend_is_rejected():
    res, _ = run(trend_frame(pullback=(-0.02,) * 6, confirm_ret=0.03))
    assert res.signal is None


def test_shallow_pullback_is_rejected_when_depth_is_required():
    p = StrategyParams(min_pullback_atr=50.0)
    res, _ = run(trend_frame(), p)
    assert res.signal is None and "shallow" in res.reason


@pytest.mark.parametrize("direction", [1, -1])
def test_bad_confirmation_candle_is_rejected(direction):
    res, _ = run(trend_frame(direction=direction, confirm_ret=-0.004))
    assert res.signal is None


def test_low_volume_on_confirmation_is_rejected():
    res, _ = run(trend_frame(confirm_volume=10.0))
    assert res.signal is None and "volume" in res.reason


def test_opposite_trend_gives_no_signal_for_the_wrong_side():
    long_res, _ = run(trend_frame(direction=1))
    short_res, _ = run(trend_frame(direction=-1))
    assert long_res.signal.side == "buy" and short_res.signal.side == "sell"


def test_not_enough_history_is_rejected():
    res, _ = run(trend_frame(n_trend=100))
    assert res.signal is None and "not enough candles" in res.reason


def test_no_lookahead_future_candles_do_not_change_the_signal():
    df = trend_frame()
    full = compute_indicators(pd.concat([df, candles([0.05, -0.05, 0.05], start_price=float(df["close"].iloc[-1]),
                                                     start=df.index[-1] + pd.Timedelta(hours=1))]), P)
    cut = compute_indicators(df, P)
    i = len(df) - 1
    a = evaluate_trend_at("X", cut, i, P).signal
    b = evaluate_trend_at("X", full, i, P).signal
    assert a is not None and b is not None
    assert (a.side, a.entry_price, a.stop_price, a.atr) == (b.side, b.entry_price, b.stop_price, b.atr)


def test_exit_signal_only_for_trend_and_only_when_structure_lost():
    df = trend_frame()
    ind = compute_indicators(df, P)
    i = len(ind) - 1
    assert exit_signal(TREND, ind, i, "buy") is None
    assert exit_signal(BREAKOUT, ind, i, "buy") is None
    # crash the last close far below EMA50
    crash = df.copy()
    crash.iloc[-1, crash.columns.get_loc("close")] = float(crash["close"].iloc[-1]) * 0.7
    ind2 = compute_indicators(crash, P)
    assert exit_signal(TREND, ind2, len(ind2) - 1, "buy") is not None
    assert exit_signal(BREAKOUT, ind2, len(ind2) - 1, "buy") is None
    assert exit_signal(TREND, ind2, -1, "buy") is None


def test_exit_signal_short_mirror():
    df = trend_frame(direction=-1)
    spike = df.copy()
    spike.iloc[-1, spike.columns.get_loc("close")] = float(spike["close"].iloc[-1]) * 1.6
    ind = compute_indicators(spike, P)
    assert exit_signal(TREND, ind, len(ind) - 1, "sell") is not None


def test_validate_strategies():
    assert validate_strategies(["trend"]) == ("trend",)
    with pytest.raises(ValueError):
        validate_strategies(["nope"])
    with pytest.raises(ValueError):
        validate_strategies([])


def test_evaluate_symbol_single_trend_keeps_signal_and_records_decision():
    ind = compute_indicators(trend_frame(), P)
    res, dec = evaluate_symbol("X", ind, len(ind) - 1, P, enabled=(TREND,))
    assert res.signal and res.signal.strategy == TREND
    assert [(d.strategy, d.kept) for d in dec] == [(TREND, True)]


def test_evaluate_symbol_both_enabled_records_reason_when_nothing_fires():
    ind = compute_indicators(trend_frame(confirm_volume=1.0), P)
    res, dec = evaluate_symbol("X", ind, len(ind) - 1, P, enabled=(BREAKOUT, TREND))
    assert res.signal is None and "breakout:" in res.reason and "trend:" in res.reason and dec == []
