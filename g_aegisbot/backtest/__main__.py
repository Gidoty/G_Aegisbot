"""CLI:  python -m g_aegisbot.backtest --years 2

Downloads Binance USDT-M futures candles (cached under data/candles), replays the strategy with
fees, slippage and funding, prints the summary and writes summary.json, trades.csv, equity.csv.
Needs network access to Binance (fapi.binance.com). --synthetic runs on random data instead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from g_aegisbot.backtest.data import download_all, synthetic_ohlcv
from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
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
    ap.add_argument("--synthetic", action="store_true", help="random data, pipeline check only")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)

    cfg = BacktestConfig(initial_capital=a.capital, leverage=a.leverage, slippage_scale=a.slippage_scale,
                         use_breaker=not a.no_breaker, params=StrategyParams(timeframe=a.timeframe))
    if a.synthetic:
        n = int(a.years * 365 * 24)
        frames = {s: synthetic_ohlcv(n, a.seed + i, start="2024-01-01") for i, s in enumerate(a.symbols)}
        print("SYNTHETIC random data. These numbers say nothing about the strategy.")
    else:
        print(f"Downloading {a.years:g} years of {a.timeframe} candles for {len(a.symbols)} pairs...")
        frames = asyncio.run(download_all(a.symbols, a.timeframe, a.years, a.cache_dir, a.refresh))
        if not frames:
            raise SystemExit("No data downloaded. Check network access to Binance.")

    short = [s for s, f in frames.items() if len(f) < cfg.params.min_candles]
    if short:
        print(f"Warning: too little history to trade (<{cfg.params.min_candles} candles): {short}")
    res = Backtester(frames, cfg).run()

    first, last = res.equity.index[0], res.equity.index[-1]
    print(f"\nBacktest {first:%Y-%m-%d} to {last:%Y-%m-%d} | {a.timeframe} | {a.leverage}x | {len(frames)} pairs"
          f" | slippage x{a.slippage_scale:g} | breaker {'off' if a.no_breaker else 'on'}")
    print(_fmt(res.metrics))
    print("\n  Per pair:")
    for s, v in sorted(res.by_symbol.items(), key=lambda kv: -kv[1]["net_pnl"]):
        print(f"    {s:<22} {v['trades']:>4} trades  net ${v['net_pnl']:>10,.2f}  win {v['win_rate']:.0%}")
    if res.notes["breaker_trip_days"]:
        print(f"\n  Daily breaker tripped on {len(res.notes['breaker_trip_days'])} days")

    out = Path(a.out) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"metrics": res.metrics, "by_symbol": res.by_symbol, "notes": res.notes,
         "synthetic": a.synthetic}, indent=2, default=lambda o: None if isinstance(o, float) and math.isinf(o) else str(o)))
    import pandas as pd
    pd.DataFrame(res.trades).to_csv(out / "trades.csv", index=False)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    print(f"\n  Saved to {out}/")


if __name__ == "__main__":
    main()
