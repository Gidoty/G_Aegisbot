"""CLI:  python -m g_aegisbot.backtest --years 2

Downloads Binance USDT-M futures candles (cached under data/candles), replays the strategy with
fees, slippage and funding, prints the summary and writes summary.json, trades.csv, equity.csv.
Needs network access to Binance (fapi.binance.com). --synthetic runs on random data instead.

The measured period is [end - years, end). At least 25 days of extra history are downloaded before
it as warm-up (and never fewer than 500 candles); warm-up is excluded from every statistic.
Any missing candle, gap, duplicate, short history or failed symbol aborts the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from g_aegisbot.backtest.data import (
    DataIntegrityError, PortfolioDownloadError, WARMUP_CANDLES, download_all, make_window, synthetic_ohlcv,
    warmup_span_ms,
)
from g_aegisbot.backtest.simulator import ASSUMPTIONS, Backtester, BacktestConfig
from g_aegisbot.config import DEFAULT_COINS, DEFAULT_LEVERAGE
from g_aegisbot.engine.strategy import StrategyParams


def _fmt(m: dict) -> str:
    pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
    cagr = "n/a" if m["cagr"] is None else f"{m['cagr']:+.1%}"
    return "\n".join([
        f"  Total return     {m['total_return']:+.2%}   (${m['start_equity']:,.0f} -> ${m['end_equity']:,.0f}, CAGR {cagr})",
        f"  Max drawdown     {m['max_drawdown']:.2%}",
        f"  Sharpe ratio     {m['sharpe']:.2f}   (daily returns, annualised on 365 days, risk-free 0)",
        f"  Win rate         {m['win_rate']:.1%}   over {m['trades']} trades",
        f"  Profit factor    {pf}",
        f"  Avg win / loss   ${m['avg_win']:,.2f} / ${m['avg_loss']:,.2f}   expectancy ${m['expectancy']:,.2f} per trade",
        f"  Costs paid       fees ${m['total_fees']:,.2f}, funding ${m['total_funding']:,.2f}",
    ])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="G-Aegisbot historical backtester")
    ap.add_argument("--years", type=float, default=2.0, help="history length, 1 to 2 recommended")
    ap.add_argument("--timeframe", default=StrategyParams().timeframe)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    ap.add_argument("--symbols", nargs="*", default=list(DEFAULT_COINS))
    ap.add_argument("--slippage-scale", type=float, default=1.0, help="multiply the slippage assumptions (try 2.0 as a stress test)")
    ap.add_argument("--no-breaker", action="store_true", help="disable the 4%% daily drawdown breaker")
    ap.add_argument("--cache-dir", default="data/candles")
    ap.add_argument("--out", default="backtests")
    ap.add_argument("--refresh", action="store_true", help="re-download candles instead of using the cache")
    ap.add_argument("--end", help="end of the measured period, YYYY-MM-DD (UTC, exclusive). Default: today 00:00 UTC")
    ap.add_argument("--retries", type=int, default=5, help="attempts per request for network and rate-limit errors")
    ap.add_argument("--allow-late-start", action="store_true",
                    help="accept a symbol whose history begins after the required start (listed later); it "
                         "still needs %d candles of its own before its first signal" % WARMUP_CANDLES)
    ap.add_argument("--allow-partial", action="store_true",
                    help="continue without symbols that failed to download. Off by default: a failed symbol aborts the run")
    ap.add_argument("--synthetic", action="store_true", help="random data, pipeline check only")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = BacktestConfig(initial_capital=a.capital, leverage=a.leverage, slippage_scale=a.slippage_scale,
                         use_breaker=not a.no_breaker, params=StrategyParams(timeframe=a.timeframe))
    integrity: dict = {}
    failures: dict = {}
    if a.synthetic:
        n = WARMUP_CANDLES + int(a.years * 365 * 24)
        frames = {s: synthetic_ohlcv(n, a.seed + i, start="2024-01-01") for i, s in enumerate(a.symbols)}
        print("SYNTHETIC random data. These numbers say nothing about the strategy.")
    else:
        end = (datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if a.end else
               datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
        measure_start = end - timedelta(days=round(a.years * 365.25))
        try:
            w = make_window(a.timeframe, int(measure_start.timestamp() * 1000), int(end.timestamp() * 1000))
        except ValueError as exc:
            raise SystemExit(f"Invalid period: {exc}")
        cfg.measure_start = measure_start
        print(f"Measured period {measure_start:%Y-%m-%d} to {end:%Y-%m-%d}; downloading {a.timeframe} candles for "
              f"{len(a.symbols)} pairs plus {warmup_span_ms(a.timeframe) // 86_400_000} days of warm-up...")
        try:
            data = asyncio.run(download_all(a.symbols, w, a.cache_dir, refresh=a.refresh, retries=a.retries,
                                            allow_partial=a.allow_partial, allow_late_start=a.allow_late_start))
        except (PortfolioDownloadError, DataIntegrityError) as exc:
            print(f"\nABORTED. No backtest was run.\n{exc}", file=sys.stderr)
            raise SystemExit(2)
        frames, integrity, failures = data.frames, data.reports, data.failures
        if failures:
            print(f"\nWARNING: --allow-partial is on. These symbols are EXCLUDED from the portfolio: {failures}")
        if not frames:
            raise SystemExit("No data downloaded. Check network access to Binance.")

    try:
        res = Backtester(frames, cfg).run()
    except ValueError as exc:
        raise SystemExit(f"Cannot run the backtest: {exc}")

    first, last = res.equity.index[0], res.equity.index[-1]
    print(f"\nBacktest {first:%Y-%m-%d} to {last:%Y-%m-%d} (measured period only; warm-up excluded) | {a.timeframe} | "
          f"{a.leverage}x | {len(frames)} pairs | slippage x{a.slippage_scale:g} | breaker {'off' if a.no_breaker else 'on'}")
    print(f"  Warm-up: {cfg.warmup_candles} closed candles required per pair before its first signal")
    print(_fmt(res.metrics))
    print("\n  Per pair:")
    for s, v in sorted(res.by_symbol.items(), key=lambda kv: -kv[1]["net_pnl"]):
        print(f"    {s:<22} {v['trades']:>4} trades  net ${v['net_pnl']:>10,.2f}  win {v['win_rate']:.0%}")
    if res.notes["breaker_trip_days"]:
        print(f"\n  Daily breaker tripped on {len(res.notes['breaker_trip_days'])} days")
    print("\n  Assumptions and limits (this is not an exact exchange simulation):")
    for k in ("funding", "liquidation", "slippage", "fills", "breaker"):
        print(f"    {k:<12} {ASSUMPTIONS[k]}")

    out = Path(a.out) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"metrics": res.metrics, "by_symbol": res.by_symbol, "notes": res.notes,
         "data_integrity": integrity, "excluded_symbols": failures, "synthetic": a.synthetic}, indent=2, default=lambda o: None if isinstance(o, float) and math.isinf(o) else str(o)))
    import pandas as pd
    pd.DataFrame(res.trades).to_csv(out / "trades.csv", index=False)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    print(f"\n  Saved to {out}/")


if __name__ == "__main__":
    main()
