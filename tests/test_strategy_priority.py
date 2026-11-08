"""Strict strategy priority (approved decision A): the configured priority is the PRIMARY ranking criterion.

Ranking order for competing signals: strategy priority first, then conviction, then volume ratio, then symbol name.
One ranking function (`strategy.signal_rank_key`) serves the per-symbol choice, the live scanner, the live runner and the
backtester, and the priority is always passed in, never defaulted.
"""
from dataclasses import replace

import pandas as pd
import pytest

from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.engine import strategies as strat
from g_aegisbot.engine.strategies import BREAKOUT, TREND, evaluate_symbol
from g_aegisbot.engine.strategy import (
    MultiPairScanner, PortfolioState, ScanResult, Signal, StrategyParams, compute_indicators, compute_size,
    portfolio_state, rank_signals, signal_rank_key,
)
from tests.fakes import FakeMarket
from tests.test_backtest import BASE, breakout_frame
from tests.test_strategy import make, with_breakout
from tests.trendfx import candles, trend_frame

DEFAULT = (BREAKOUT, TREND)
REVERSED = (TREND, BREAKOUT)
T0 = pd.Timestamp("2026-01-01", tz="UTC")


def sig(strategy, side="buy", conviction="normal", vol=2.0, symbol="X") -> Signal:
    return Signal(symbol, side, T0, 100.0, 1.0, 98.0 if side == "buy" else 102.0, vol, 60.0, conviction, 0.06,
                  strategy=strategy, trail_atr_mult=None)


def order(signals, priority):
    return [(s.strategy, s.symbol) for s in sorted(signals, key=lambda s: signal_rank_key(s, priority))]


# ------------------------------------------------------------------------------------------------ the one ranking key
def test_priority_beats_volume_and_conviction_and_reversing_it_reverses_the_order():
    weak_breakout = sig(BREAKOUT, vol=1.5, symbol="A")
    strong_trend = sig(TREND, vol=9.0, symbol="B")
    assert order([strong_trend, weak_breakout], DEFAULT) == [(BREAKOUT, "A"), (TREND, "B")]
    assert order([weak_breakout, strong_trend], REVERSED) == [(TREND, "B"), (BREAKOUT, "A")]


def test_high_conviction_in_the_lower_priority_strategy_does_not_override_priority():
    high_breakout = sig(BREAKOUT, conviction="high", vol=3.0, symbol="A")
    normal_trend = sig(TREND, vol=1.1, symbol="B")
    assert order([normal_trend, high_breakout], REVERSED) == [(TREND, "B"), (BREAKOUT, "A")]
    assert order([high_breakout, normal_trend], DEFAULT) == [(BREAKOUT, "A"), (TREND, "B")]


def test_conviction_ranks_signals_within_one_strategy():
    high = sig(BREAKOUT, conviction="high", vol=1.6, symbol="HIGH")
    normal = sig(BREAKOUT, conviction="normal", vol=5.0, symbol="NORMAL")      # more volume, but normal conviction
    assert order([normal, high], DEFAULT) == [(BREAKOUT, "HIGH"), (BREAKOUT, "NORMAL")]


def test_volume_ratio_ranks_signals_within_one_strategy():
    low, high = sig(TREND, vol=1.2, symbol="LOW"), sig(TREND, vol=3.4, symbol="HIGH")
    assert order([low, high], DEFAULT) == [(TREND, "HIGH"), (TREND, "LOW")]
    assert order([high, low], REVERSED) == [(TREND, "HIGH"), (TREND, "LOW")]


def test_equal_everything_falls_back_to_symbol_name_so_runs_are_reproducible():
    a, b = sig(TREND, symbol="AAA"), sig(TREND, symbol="BBB")
    assert order([b, a], DEFAULT) == order([a, b], DEFAULT) == [(TREND, "AAA"), (TREND, "BBB")]


def test_unknown_strategy_names_rank_after_every_listed_one_and_deterministically():
    known = sig(TREND, vol=1.0, symbol="K")
    zeta, alpha = sig("zeta", vol=50.0, conviction="high", symbol="S"), sig("alpha", vol=50.0, conviction="high", symbol="S")
    expected = [(TREND, "K"), ("alpha", "S"), ("zeta", "S")]
    assert order([zeta, known, alpha], DEFAULT) == expected
    assert order([alpha, zeta, known], DEFAULT) == expected
    assert order([zeta, alpha], ()) == [("alpha", "S"), ("zeta", "S")]            # an empty priority is safe too


def test_the_priority_is_never_defaulted_so_no_ranking_path_can_ignore_it():
    s = sig(BREAKOUT)
    with pytest.raises(TypeError):
        signal_rank_key(s)
    with pytest.raises(TypeError):
        rank_signals([ScanResult("X", s, "signal")])


def test_rank_signals_uses_the_priority_it_is_given():
    results = [ScanResult("A", sig(BREAKOUT, vol=1.5, symbol="A"), "signal"), ScanResult("B", None, "no"),
               ScanResult("C", sig(TREND, vol=8.0, symbol="C"), "signal")]
    assert [s.symbol for s in rank_signals(results, DEFAULT)] == ["A", "C"]
    assert [s.symbol for s in rank_signals(results, REVERSED)] == ["C", "A"]


# ------------------------------------------------------------------------------------------------ one symbol, real candles
# The fixture's unbroken trend leaves RSI at about 71 (long) and 28 (short), just outside the production band, so the TEST
# parameters widen the breakout RSI band. That is the only way to make both rules fire on one candle; no rule is weakened in
# the production defaults.
DUAL = replace(StrategyParams(), rsi_long=(50.0, 80.0), rsi_short=(20.0, 50.0), strategies=(BREAKOUT, TREND))


def dual_frame(direction):
    return trend_frame(direction=direction, confirm_ret=0.0225, confirm_volume=160.0)


@pytest.mark.parametrize("direction,side", [(1, "buy"), (-1, "sell")])
def test_both_strategies_signalling_one_symbol_gives_breakout_by_default(direction, side):
    ind = compute_indicators(dual_frame(direction), DUAL)
    res, decisions = evaluate_symbol("X", ind, len(ind) - 1, DUAL)
    assert res.signal.strategy == BREAKOUT and res.signal.side == side
    assert [(d.strategy, d.kept) for d in decisions] == [(BREAKOUT, True), (TREND, False)]
    assert "duplicate" in decisions[1].reason


@pytest.mark.parametrize("direction", [1, -1])
def test_reversing_the_configured_priority_reverses_the_selection(direction):
    ind = compute_indicators(dual_frame(direction), DUAL)
    res, decisions = evaluate_symbol("X", ind, len(ind) - 1, replace(DUAL, strategy_priority=REVERSED))
    assert res.signal.strategy == TREND
    assert [(d.strategy, d.kept) for d in decisions] == [(TREND, True), (BREAKOUT, False)]


def test_trend_is_selected_when_breakout_has_no_eligible_signal():
    ind = compute_indicators(trend_frame(), DUAL)
    res, decisions = evaluate_symbol("X", ind, len(ind) - 1, DUAL)
    assert res.signal.strategy == TREND and [(d.strategy, d.kept) for d in decisions] == [(TREND, True)]


def test_one_symbol_never_yields_two_signals():
    ind = compute_indicators(dual_frame(1), DUAL)
    res, decisions = evaluate_symbol("X", ind, len(ind) - 1, DUAL)
    assert res.signal is not None and sum(d.kept for d in decisions) == 1 and len(decisions) == 2


# ------------------------------------------------------------------------------------------------ conflicts and duplicates
# Real candles cannot make the two rules disagree on direction (their trend filters exclude each other), so the two
# evaluators are replaced by stubs that return a fixed signal. `evaluate_symbol` itself, with its conflict handling and its
# ranking, is the real code under test.
def stub(monkeypatch, breakout=None, trend=None):
    for name, s in ((BREAKOUT, breakout), (TREND, trend)):
        monkeypatch.setitem(strat._EVALUATORS, name,
                            lambda symbol, ind, i, p, _s=s: ScanResult(symbol, _s, "signal" if _s else "none"))


@pytest.mark.parametrize("priority", [DEFAULT, REVERSED])
def test_opposite_direction_signals_produce_no_trade_whatever_the_priority(monkeypatch, priority):
    stub(monkeypatch, breakout=sig(BREAKOUT, "buy", conviction="high"), trend=sig(TREND, "sell"))
    p = replace(StrategyParams(), strategies=(BREAKOUT, TREND), strategy_priority=priority)
    res, decisions = evaluate_symbol("X", None, 0, p)
    assert res.signal is None and res.reason.startswith("conflict: opposite signals")
    assert [(d.strategy, d.kept) for d in decisions] == [(BREAKOUT, False), (TREND, False)]


@pytest.mark.parametrize("priority,winner", [(DEFAULT, BREAKOUT), (REVERSED, TREND)])
def test_same_direction_duplicates_keep_exactly_one_signal_chosen_by_priority(monkeypatch, priority, winner):
    stub(monkeypatch, breakout=sig(BREAKOUT, "buy", conviction="high", vol=3.0), trend=sig(TREND, "buy", vol=9.0))
    p = replace(StrategyParams(), strategies=(BREAKOUT, TREND), strategy_priority=priority)
    res, decisions = evaluate_symbol("X", None, 0, p)
    assert res.signal.strategy == winner
    assert [d.kept for d in decisions if d.strategy == winner] == [True]
    assert sum(d.kept for d in decisions) == 1 and all("duplicate" in d.reason for d in decisions if not d.kept)


def test_only_the_enabled_strategies_are_considered(monkeypatch):
    stub(monkeypatch, breakout=sig(BREAKOUT, "buy"), trend=sig(TREND, "buy"))
    res, decisions = evaluate_symbol("X", None, 0, StrategyParams(), enabled=(TREND,))
    assert res.signal.strategy == TREND and [d.strategy for d in decisions] == [TREND]


# ------------------------------------------------------------------------------------------------ live scanner
class Feed:
    """Serves canned candles to MultiPairScanner the way the exchange does: closed candles plus one forming candle."""

    def __init__(self, frames):
        self.market = FakeMarket(list(frames))
        self.exchange = self.market.exchange
        for symbol, df in frames.items():
            forming = df.iloc[[-1]].copy()
            forming.index = [df.index[-1] + pd.Timedelta(hours=1)]
            self.market.candles[(symbol, "1h")] = pd.concat([df, forming])

    async def fetch_ohlcv_df(self, symbol, timeframe, limit):
        return await self.market.fetch_ohlcv_df(symbol, timeframe, limit)


def two_symbol_feed():
    # BRK: a breakout with ratio 1.6. TRD: a valid pullback entry on 4x volume. By volume alone TRD would rank first.
    return Feed({"BRK/USDT:USDT": with_breakout(make(), vol=160.0),
                 "TRD/USDT:USDT": trend_frame(n_trend=394, confirm_volume=400.0)})        # both 400 candles, same clock


@pytest.mark.parametrize("priority,expected", [
    (DEFAULT, [("BRK/USDT:USDT", BREAKOUT), ("TRD/USDT:USDT", TREND)]),
    (REVERSED, [("TRD/USDT:USDT", TREND), ("BRK/USDT:USDT", BREAKOUT)]),
])
async def test_live_scanner_orders_signals_by_the_configured_priority(priority, expected):
    params = replace(StrategyParams(), strategies=(BREAKOUT, TREND), strategy_priority=priority)
    feed = two_symbol_feed()
    now = (feed.market.candles[("BRK/USDT:USDT", "1h")].index[-1] + pd.Timedelta(seconds=5)).to_pydatetime()
    scanner = MultiPairScanner(feed, symbols=["BRK/USDT:USDT", "TRD/USDT:USDT"], params=params, clock=lambda: now)
    signals = await scanner.scan_signals()
    assert [(s.symbol, s.strategy) for s in signals] == expected
    assert signals[0].volume_ratio < signals[1].volume_ratio if priority == DEFAULT else True


# ------------------------------------------------------------------------------------------------ backtester, combined limits
def trend_symbol_frame(extra_bars=3):
    """Same clock and length as breakout_frame: the trend entry is confirmed on the last of BASE bars."""
    base = trend_frame(n_trend=BASE - 6)
    assert len(base) == BASE
    flat = candles([0.0] * extra_bars, start_price=float(base["close"].iloc[-1]), start=base.index[-1] + pd.Timedelta(hours=1))
    return pd.concat([base, flat])


def mixed_frames():
    longs = breakout_frame([(121, 121.3, 120.8, 121.0)] * 3)
    return {"B0/USDT:USDT": longs, "B1/USDT:USDT": longs, "T0/USDT:USDT": trend_symbol_frame(),
            "T1/USDT:USDT": trend_symbol_frame()}


def run_mixed(priority):
    params = replace(StrategyParams(), strategies=(BREAKOUT, TREND), strategy_priority=priority)
    return Backtester(mixed_frames(), BacktestConfig(params=params)).run()


def test_backtest_respects_the_side_cap_across_both_strategies_and_priority_decides_who_gets_the_slots():
    default = run_mixed(DEFAULT)
    assert len(default.trades) == 3 and {t["side"] for t in default.trades} == {"buy"}          # 4 setups, 3 per side
    assert sorted(t["strategy"] for t in default.trades) == [BREAKOUT, BREAKOUT, TREND]
    reverse = run_mixed(REVERSED)
    assert len(reverse.trades) == 3
    assert sorted(t["strategy"] for t in reverse.trades) == [BREAKOUT, TREND, TREND]


def test_backtest_and_live_agree_on_the_order_for_the_same_signals():
    # the simulator sorts with the same key as the live path
    signals = [sig(TREND, vol=9.0, symbol="T"), sig(BREAKOUT, vol=1.6, symbol="B")]
    assert order(signals, DEFAULT) == [(s.strategy, s.symbol) for s in rank_signals(
        [ScanResult(s.symbol, s, "signal") for s in signals], DEFAULT)]


def long_positions(n):
    return [{"symbol": f"P{i}/USDT:USDT", "side": "long", "contracts": 1.0, "entryPrice": 100.0} for i in range(n)]


def test_mixed_open_positions_count_against_one_side_cap_whatever_strategy_opened_them():
    state = portfolio_state(long_positions(3))             # positions of any strategy: the state knows only the exchange
    plan = compute_size(sig(TREND, "buy"), leverage=3, allocated=10_000, used=0, free=10_000, open_positions=3, state=state)
    assert plan.reason.startswith("max 3 positions already open on the long side")


def test_aggregate_open_risk_is_shared_by_both_strategies():
    p = StrategyParams()
    state = PortfolioState(open_risk=10_000 * p.max_aggregate_risk_pct, open_notional=0.0, longs=1, shorts=0)
    plan = compute_size(sig(TREND, "buy"), leverage=3, allocated=10_000, used=0, free=10_000, open_positions=1, state=state)
    assert plan.reason.startswith("aggregate open risk")
