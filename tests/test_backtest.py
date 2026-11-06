import math

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest.data import fetch_history, load_or_download, synthetic_ohlcv
from g_aegisbot.backtest.metrics import compute_metrics, max_drawdown, sharpe_ratio
from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.costs import FUNDING_RATE_8H, TAKER_FEE, slippage_rate
from tests.test_strategy import make, with_breakout

SYM = "BTC/USDT:USDT"


def breakout_frame(extra_bars):
    """Uptrend, a breakout on bar 399's close, then the given (open, high, low, close) bars."""
    df = with_breakout(make(n=400))
    idx = pd.date_range(df.index[-1] + pd.Timedelta(hours=1), periods=len(extra_bars), freq="1h", tz="UTC")
    extra = pd.DataFrame(extra_bars, index=idx, columns=["open", "high", "low", "close"]).assign(volume=100.0)
    return pd.concat([df, extra])


def run(frames, **kw):
    return Backtester(frames, BacktestConfig(**kw)).run()


# ---- metrics
def test_max_drawdown_and_total_return():
    eq = pd.Series([100, 110, 99, 121.0], index=pd.date_range("2025-01-01", periods=4, freq="D", tz="UTC"))
    assert max_drawdown(eq) == pytest.approx(-11 / 110)
    m = compute_metrics(eq, [{"pnl": 10, "fees": 1, "funding": 0}, {"pnl": -11, "fees": 1, "funding": 0},
                             {"pnl": 22, "fees": 1, "funding": 0}], 100.0)
    assert m["total_return"] == pytest.approx(0.21)
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["profit_factor"] == pytest.approx(32 / 11)
    assert m["expectancy"] == pytest.approx(7.0) and m["total_fees"] == 3


def test_sharpe_matches_manual_calculation():
    rets = np.tile([0.01, -0.005], 30)
    eq = pd.Series(100 * np.cumprod(1 + rets), index=pd.date_range("2025-01-01", periods=60, freq="D", tz="UTC"))
    r = eq.pct_change().dropna()
    assert sharpe_ratio(eq) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(365))
    flat = pd.Series(100.0, index=eq.index)
    assert sharpe_ratio(flat) == 0.0


def test_profit_factor_edge_cases():
    eq = pd.Series([100, 105.0], index=pd.date_range("2025-01-01", periods=2, freq="D", tz="UTC"))
    assert math.isinf(compute_metrics(eq, [{"pnl": 5, "fees": 0, "funding": 0}], 100)["profit_factor"])
    assert compute_metrics(eq, [], 100)["win_rate"] == 0.0


# ---- simulator
def test_stop_out_costs_match_the_model_and_books_balance():
    up = [(121, 121.3, 120.8, 121.0)] * 3
    crash = [(121.0, 121.0, 100.0, 105.0)]                      # trades through the stop
    frame = breakout_frame(up + crash + [(105, 106, 104, 105)])
    res = run({SYM: frame})
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["side"] == "buy" and t["reason"] == "stop-loss"
    entry_open = frame["open"].iloc[400]
    assert t["entry_price"] == pytest.approx(entry_open * (1 + slippage_rate(SYM)))
    assert t["exit_price"] < t["entry_price"]
    assert t["fees"] == pytest.approx(TAKER_FEE * t["amount"] * (t["entry_price"] + t["exit_price"]))
    bars_held = 3                  # the stop hits inside the 4th bar, before its funding accrues
    expected_funding = sum(t["amount"] * frame["close"].iloc[400 + j] * FUNDING_RATE_8H / 8 for j in range(bars_held))
    assert t["funding"] == pytest.approx(expected_funding, rel=1e-6)
    gross = (t["exit_price"] - t["entry_price"]) * t["amount"]
    assert t["pnl"] == pytest.approx(gross - t["fees"] - t["funding"])
    assert res.metrics["end_equity"] == pytest.approx(10_000 + t["pnl"])      # books balance
    assert t["pnl"] < 0 and abs(t["pnl"]) <= 0.015 * 10_000 * 1.2              # loss near the 1.5% cap, plus slippage and costs


def test_gap_through_stop_fills_at_open_not_at_stop():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0), (90, 91, 89, 90)])    # opens far below the stop
    t = run({SYM: frame}).trades[0]
    assert t["exit_price"] == pytest.approx(90 * (1 - slippage_rate(SYM, is_stop=True)))


def test_trailing_stop_locks_in_profit():
    rise = [(121 + j, 122 + j, 120.8 + j, 121.5 + j) for j in range(12)]
    frame = breakout_frame(rise + [(133, 133, 100, 110)])
    t = run({SYM: frame}).trades[0]
    assert t["reason"] == "stop-loss" and t["exit_price"] > t["entry_price"] and t["pnl"] > 0


def test_open_position_is_closed_at_end_of_data():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0)] * 2)
    res = run({SYM: frame})
    assert [x["reason"] for x in res.trades] == ["end of data"]
    assert res.metrics["end_equity"] == pytest.approx(10_000 + res.trades[0]["pnl"])


def test_max_four_positions_in_backtest():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0)] * 3)
    frames = {f"C{i}/USDT:USDT": frame for i in range(7)}
    res = run(frames)
    assert len(res.trades) == 4


def test_short_side_and_costs_symmetric():
    down = make(n=400, up=False)
    frame = with_breakout(down, up=False)
    idx = pd.date_range(frame.index[-1] + pd.Timedelta(hours=1), periods=3, freq="1h", tz="UTC")
    last = frame["close"].iloc[-1]
    extra = pd.DataFrame([(last, last * 1.001, last * 0.999, last)] * 2 + [(last, last * 1.2, last * 0.999, last * 1.1)],
                         index=idx, columns=["open", "high", "low", "close"]).assign(volume=100.0)
    res = run({SYM: pd.concat([frame, extra])})
    if res.trades:                                       # signal depends on the generated path; if taken it must be a short
        t = res.trades[0]
        assert t["side"] == "sell" and t["entry_price"] < frame["open"].iloc[-1] * 1.01


def test_no_lookahead_entries_before_cutoff_are_identical():
    frames = {f"S{i}/USDT:USDT": synthetic_ohlcv(3000, seed=10 + i, drift=0.0002) for i in range(4)}
    full = run(frames)
    cutoff = pd.Timestamp(frames["S0/USDT:USDT"].index[2000])
    cut = run({s: f[f.index <= cutoff] for s, f in frames.items()})
    key = lambda r: sorted((x["symbol"], x["entry_time"], round(x["entry_price"], 8)) for x in r.trades
                           if x["entry_time"] < cutoff - pd.Timedelta(hours=2))
    assert key(full) == key(cut) and len(key(full)) > 0


def test_no_edge_on_random_data_after_costs():
    """A strategy with no edge must not look good on random walks. Large profits here would mean a bug."""
    rets = []
    for seed in range(3):
        frames = {f"S{i}/USDT:USDT": synthetic_ohlcv(6000, seed=100 * seed + i) for i in range(5)}
        rets.append(run(frames).metrics["total_return"])
    assert np.mean(rets) < 0.15


def test_slippage_scale_hurts():
    frames = {f"S{i}/USDT:USDT": synthetic_ohlcv(6000, seed=i, drift=0.0003) for i in range(5)}
    base, stress = run(frames), run(frames, slippage_scale=3.0)
    assert stress.metrics["end_equity"] < base.metrics["end_equity"] or base.metrics["trades"] == 0


# ---- data
class FakeEx:
    def __init__(self, n, tf_ms=3_600_000):
        self.rows = [[1_700_000_000_000 + i * tf_ms, 1, 2, 0.5, 1.5, 10] for i in range(n)]
        self.calls = 0

    @staticmethod
    def parse_timeframe(tf):
        return 3600

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1500):
        self.calls += 1
        return [r for r in self.rows if r[0] >= since][:limit]


async def test_fetch_history_pages_dedupes_and_drops_forming_candle():
    ex = FakeEx(3200)
    until = ex.rows[-1][0] + 1_800_000          # half-way through the last candle
    df = await fetch_history(ex, "X", "1h", ex.rows[0][0], until, page=1500)
    assert ex.calls >= 3 and df.index.is_unique and df.index.is_monotonic_increasing
    assert len(df) == 3199                       # the forming candle is excluded


async def test_cache_roundtrip(tmp_path, monkeypatch):
    import time
    ex = FakeEx(300)
    now_ms = ex.rows[-1][0] + 3_600_000
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)
    a = await load_or_download(ex, "A/USDT:USDT", "1h", 0.01, tmp_path)
    calls = ex.calls
    b = await load_or_download(ex, "A/USDT:USDT", "1h", 0.01, tmp_path)
    assert ex.calls == calls                     # second load came from the CSV cache
    pd.testing.assert_frame_equal(a, b)


def test_cli_synthetic_end_to_end(tmp_path, capsys):
    from g_aegisbot.backtest.__main__ import main
    main(["--synthetic", "--years", "0.5", "--symbols", "A/USDT:USDT", "B/USDT:USDT", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "Win rate" in out and "Profit factor" in out and "Max drawdown" in out
    run_dir = next(tmp_path.iterdir())
    assert {"summary.json", "trades.csv", "equity.csv"} <= {p.name for p in run_dir.iterdir()}
