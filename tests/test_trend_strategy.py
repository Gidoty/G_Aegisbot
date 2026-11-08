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


# ---- approved decision B: the SIGNAL candle must close strictly beyond EMA50 (long above, short below; equality fails)
from g_aegisbot.engine.trend import add_trend_columns  # noqa: E402


def with_ema50(df, factor):
    """Indicators for `df` where the signal candle's EMA50 is set to close * factor and every trend column is recomputed.
    Only that candle's EMA50 changes, so the other clauses read exactly what they read for the unmodified frame."""
    ind = compute_indicators(df, P).copy()
    ind.iloc[-1, ind.columns.get_loc("ema_f")] = float(ind["close"].iloc[-1]) * factor
    add_trend_columns(ind, P)
    return ind


def verdict(ind):
    return evaluate_trend_at("X", ind, len(ind) - 1, P)


@pytest.mark.parametrize("direction", [1, -1])
def test_unmodified_setups_close_beyond_ema50_and_signal(direction):
    res, ind = run(trend_frame(direction=direction))
    last = ind.iloc[-1]
    assert (last["close"] > last["ema_f"]) if direction == 1 else (last["close"] < last["ema_f"])
    assert res.signal is not None


def test_long_is_rejected_when_the_signal_close_is_below_ema50():
    res = verdict(with_ema50(trend_frame(direction=1), 1.001))             # EMA50 0.1% above the close
    assert res.signal is None and "EMA50" in res.reason


def test_short_is_rejected_when_the_signal_close_is_above_ema50():
    res = verdict(with_ema50(trend_frame(direction=-1), 0.999))            # EMA50 0.1% below the close
    assert res.signal is None and "EMA50" in res.reason


@pytest.mark.parametrize("direction", [1, -1])
def test_equality_with_ema50_does_not_qualify(direction):
    ind = with_ema50(trend_frame(direction=direction), 1.0)
    assert ind["close"].iloc[-1] == ind["ema_f"].iloc[-1]
    res = verdict(ind)
    assert res.signal is None and "EMA50" in res.reason


@pytest.mark.parametrize("direction,factor", [(1, 1 - 1e-9), (-1, 1 + 1e-9)])
def test_the_smallest_step_beyond_ema50_qualifies(direction, factor):
    res = verdict(with_ema50(trend_frame(direction=direction), factor))
    assert res.signal is not None, res.reason


def test_the_rule_is_judged_on_the_signal_candle_not_the_next_one():
    df = trend_frame()
    signal_ind = compute_indicators(df, P)
    i = len(df) - 1
    crash = candles([-0.2], start_price=float(df["close"].iloc[-1]), start=df.index[-1] + pd.Timedelta(hours=1))
    full = compute_indicators(pd.concat([df, crash]), P)
    assert full["close"].iloc[-1] < full["ema_f"].iloc[-1]                  # the execution candle is far below EMA50
    assert evaluate_trend_at("X", full, i, P).signal is not None             # the signal candle is still valid
    assert evaluate_trend_at("X", full, i + 1, P).signal is None            # and the crash candle itself is not an entry
    assert evaluate_trend_at("X", signal_ind, i, P).signal is not None


def test_the_other_clauses_are_unchanged_by_the_new_rule():
    ind = compute_indicators(trend_frame(), P)
    for clause in ("L_trend", "L_touch", "L_valid", "L_depth", "L_confirm", "L_vol", "L_ema50"):
        assert bool(ind[clause].iloc[-1]), clause


# ---- the existing EMA50 exit is untouched (strict: a close exactly on EMA50 does not exit)
def test_trend_exit_still_fires_below_ema50_and_not_on_equality_for_longs():
    base = trend_frame()
    ind_eq = with_ema50(base, 1.0)
    assert exit_signal(TREND, ind_eq, len(ind_eq) - 1, "buy") is None
    ind_below = with_ema50(base, 1.001)
    assert "close below EMA50" in exit_signal(TREND, ind_below, len(ind_below) - 1, "buy")


def test_trend_exit_still_fires_above_ema50_and_not_on_equality_for_shorts():
    base = trend_frame(direction=-1)
    ind_eq = with_ema50(base, 1.0)
    assert exit_signal(TREND, ind_eq, len(ind_eq) - 1, "sell") is None
    ind_above = with_ema50(base, 0.999)
    assert "close above EMA50" in exit_signal(TREND, ind_above, len(ind_above) - 1, "sell")
