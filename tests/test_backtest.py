import math

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest.data import synthetic_ohlcv
from g_aegisbot.backtest.metrics import compute_metrics, max_drawdown, sharpe_ratio
from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.costs import FUNDING_RATE_8H, TAKER_FEE, slippage_rate
from tests.test_strategy import make, with_breakout

SYM = "BTC/USDT:USDT"


BASE = 520    # bars before the extras; > 500 so the 500-candle warm-up rule is satisfied


def breakout_frame(extra_bars, base=BASE):
    """Uptrend, a breakout on the close of bar base-1, then the given (open, high, low, close) bars."""
    df = with_breakout(make(n=base))
    idx = pd.date_range(df.index[-1] + pd.Timedelta(hours=1), periods=len(extra_bars), freq="1h", tz="UTC")
    scale = df["close"].iloc[-1] / 120.9     # extras are written for a signal close of ~120.9; rescale to the real one
    extra = pd.DataFrame(extra_bars, index=idx, columns=["open", "high", "low", "close"]).mul(scale).assign(volume=100.0)
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
    entry_open = frame["open"].iloc[BASE]
    assert t["entry_price"] == pytest.approx(entry_open * (1 + slippage_rate(SYM)))
    assert t["exit_price"] < t["entry_price"]
    assert t["fees"] == pytest.approx(TAKER_FEE * t["amount"] * (t["entry_price"] + t["exit_price"]))
    bars_held = 3                  # the stop hits inside the 4th bar, before its funding accrues
    expected_funding = sum(t["amount"] * frame["close"].iloc[BASE + j] * FUNDING_RATE_8H / 8 for j in range(bars_held))
    assert t["funding"] == pytest.approx(expected_funding, rel=1e-6)
    gross = (t["exit_price"] - t["entry_price"]) * t["amount"]
    assert t["pnl"] == pytest.approx(gross - t["fees"] - t["funding"])
    assert res.metrics["end_equity"] == pytest.approx(10_000 + t["pnl"])      # books balance
    assert t["pnl"] < 0 and abs(t["pnl"]) <= 0.015 * 10_000 * 1.2              # loss near the 1.5% cap, plus slippage and costs


def test_gap_through_stop_fills_at_open_not_at_stop():
    frame = breakout_frame([(121, 121.3, 120.8, 121.0), (90, 91, 89, 90)])    # opens far below the stop
    t = run({SYM: frame}).trades[0]
    gap_open = frame["open"].iloc[BASE + 1]
    assert t["exit_price"] == pytest.approx(gap_open * (1 - slippage_rate(SYM, is_stop=True)))


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
    down = make(n=BASE, up=False)
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


def test_cli_synthetic_end_to_end(tmp_path, capsys):
    from g_aegisbot.backtest.__main__ import main
    main(["--synthetic", "--years", "0.5", "--symbols", "A/USDT:USDT", "B/USDT:USDT", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "Win rate" in out and "Profit factor" in out and "Max drawdown" in out
    run_dir = next(tmp_path.iterdir())
    assert {"summary.json", "trades.csv", "equity.csv"} <= {p.name for p in run_dir.iterdir()}


# ---- warm-up enforcement and exclusion from statistics
def flat(n):
    """n flat bars written for a signal close of ~120.9 (breakout_frame rescales them)."""
    return [(120.9, 120.9, 120.9, 120.9)] * n


def frame_breaking_out_at(k, total):
    """A valid long breakout whose signal bar is index k, then flat bars up to `total` bars."""
    f = breakout_frame(flat(total - (k + 1)), base=k + 1)
    assert len(f) == total
    return f


def trades_for(frame, **cfg):
    return run({SYM: frame}, **cfg)


def test_control_breakout_inside_warmup_is_a_valid_signal_without_the_rule():
    """Proves the warm-up tests below can fail: with a 250-candle rule this same data does trade."""
    frame = frame_breaking_out_at(300, 700)
    res = trades_for(frame, warmup_candles=250)
    assert len(res.trades) == 1 and res.trades[0]["entry_time"] == frame.index[301]


def test_no_trade_can_start_inside_the_500_candle_warmup():
    frame = frame_breaking_out_at(300, 700)
    assert trades_for(frame).trades == []                       # default warm-up is 500 candles


@pytest.mark.parametrize("k,expect_trade", [(498, False), (499, False), (500, True), (501, True)])
def test_first_signal_is_allowed_only_after_500_closed_candles(k, expect_trade):
    frame = frame_breaking_out_at(k, k + 40)
    assert len(trades_for(frame, warmup_candles=250).trades) == 1       # signal exists at every k
    res = trades_for(frame, measure_start=frame.index[0])               # only the 500-candle rule can block it
    assert len(res.trades) == (1 if expect_trade else 0)
    if expect_trade:
        assert res.trades[0]["entry_time"] == frame.index[k + 1]


def test_warmup_is_counted_per_symbol_for_late_listed_pairs():
    """A pair with fewer than 500 candles of its own history cannot trade, even late in the shared timeline."""
    a = frame_breaking_out_at(800, 900)                                  # unrelated, long history
    a["close"] = a["open"] = a["high"] = a["low"] = 100.0                # make A flat so it produces no signals
    a["volume"] = 100.0
    late = frame_breaking_out_at(449, 560)                              # only 449 bars behind its signal
    late.index = late.index + (a.index[400] - late.index[0])            # listed 400 bars after A
    frames = {"A/USDT:USDT": a, "LATE/USDT:USDT": late}
    assert not [t for t in run(frames).trades if t["symbol"] == "LATE/USDT:USDT"]
    assert [t for t in run(frames, warmup_candles=250).trades if t["symbol"] == "LATE/USDT:USDT"]   # control


def test_signals_before_measure_start_are_ignored_even_with_enough_history():
    frame = frame_breaking_out_at(520, 700)
    start = frame.index[600]
    assert len(trades_for(frame).trades) == 1                           # default measure start = bar 500: trades
    res = trades_for(frame, measure_start=start)
    assert res.trades == []


def test_warmup_is_excluded_from_equity_period_and_statistics():
    frame = frame_breaking_out_at(650, 720)
    start = frame.index[600]
    res = trades_for(frame, measure_start=start)
    assert len(res.trades) == 1
    assert all(t["entry_time"] >= start for t in res.trades)
    assert res.equity.index[0] == start and res.equity.index.min() == start        # nothing from the warm-up
    assert res.notes["measurement_period"]["start"] == str(start)
    assert res.notes["measurement_period"]["end"] == str(frame.index[-1] + pd.Timedelta(hours=1))
    assert res.notes["warmup"]["excluded_from_statistics"] is True
    assert res.notes["warmup"]["required_candles"] == 500
    assert res.notes["warmup"]["first_signal_eligible"][SYM] == str(start)
    assert res.notes["data_range_including_warmup"][SYM][0] == str(frame.index[0])  # warm-up is reported separately
    # statistics use only the measured span
    assert len(res.equity) == 1 + (len(frame) - 600)           # opening point plus one per measured bar, none from warm-up
    assert res.metrics["start_equity"] == 10_000


def test_idle_measured_period_reports_zero_return_proving_warmup_has_no_pnl():
    frame = frame_breaking_out_at(520, 700)
    res = trades_for(frame, measure_start=frame.index[600])
    assert res.metrics["total_return"] == 0 and res.metrics["trades"] == 0
    assert (res.equity == 10_000).all()


def test_too_little_data_for_a_measured_period_is_an_error():
    with pytest.raises(ValueError, match="more than 500 candles"):
        Backtester({SYM: make(n=400)}, BacktestConfig())
    with pytest.raises(ValueError, match="after the last candle"):
        Backtester({SYM: make(n=600)}, BacktestConfig(measure_start=pd.Timestamp("2100-01-01", tz="UTC")))


# ---- assumptions are stated in the report metadata
def test_funding_and_liquidation_are_labelled_as_assumptions():
    res = trades_for(frame_breaking_out_at(520, 700))
    a = res.notes["assumptions"]
    assert "approximation" in a["funding"] and "NOT used" in a["funding"]
    assert "NOT an exact Binance liquidation simulation" in a["liquidation"]
    assert a["maintenance_margin"] == 0.005 and a["funding_rate_8h"] == FUNDING_RATE_8H


def test_cli_report_states_assumptions_and_excludes_warmup(tmp_path, capsys):
    import json
    from g_aegisbot.backtest.__main__ import main
    main(["--synthetic", "--years", "0.3", "--symbols", "A/USDT:USDT", "B/USDT:USDT", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "not an exact exchange simulation" in out and "NOT an exact Binance liquidation simulation" in out
    assert "Historical Binance funding rates are NOT used" in out
    assert "measured period only; warm-up excluded" in out and "500 closed candles required" in out
    summary = json.loads(next(tmp_path.iterdir()).joinpath("summary.json").read_text())
    assert "NOT an exact Binance liquidation simulation" in summary["notes"]["assumptions"]["liquidation"]
    assert summary["notes"]["warmup"]["required_candles"] == 500


def test_cli_aborts_without_running_when_a_required_symbol_fails(tmp_path, monkeypatch, capsys):
    from g_aegisbot.backtest import __main__ as cli
    from g_aegisbot.backtest.data import PortfolioDownloadError

    async def fake_download_all(*a, **k):
        raise PortfolioDownloadError({"SUI/USDT:USDT": "3 missing candle(s) in 1 gap(s)"})

    monkeypatch.setattr(cli, "download_all", fake_download_all)
    with pytest.raises(SystemExit) as e:
        cli.main(["--years", "1", "--symbols", "SUI/USDT:USDT", "--cache-dir", str(tmp_path / "c"), "--out", str(tmp_path / "o")])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "ABORTED" in err and "SUI/USDT:USDT" in err and "3 missing candle" in err
    assert not (tmp_path / "o").exists()                                # no backtest, no output files
