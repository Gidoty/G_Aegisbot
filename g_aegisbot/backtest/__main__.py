"""CLI:  python -m g_aegisbot.backtest --years 5

Downloads Binance USDT-M futures candles (and, by default, real funding rates), replays the strategies with fees,
slippage and funding, prints a report and writes summary.json, trades.csv and equity.csv per run.
Needs network access to Binance (fapi.binance.com). --synthetic runs on random data instead (pipeline check only).

The measured period is [start, end). At least 25 days of extra history are downloaded before it as warm-up (and never fewer
than 500 candles); warm-up is excluded from every statistic. Any missing candle, funding gap, duplicate, short history or
failed symbol aborts the run: nothing is invented, interpolated or silently dropped.

Typical programme (see docs/BACKTEST_PROGRAMME.md):
  python -m g_aegisbot.backtest --years 5 --end 2026-11-06 --symbols <11 pairs> --compare --split 0.5,0.25,0.25
  python -m g_aegisbot.backtest --start 2024-08-21 --end 2026-11-06 --compare              # all 15 pairs
  python -m g_aegisbot.backtest --from-listing --individually --symbols SUI/USDT:USDT ...  # newer pairs, one by one
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from g_aegisbot.backtest.data import (
    DataIntegrityError, PortfolioDownloadError, WARMUP_CANDLES, download_all, earliest_candle_ms, make_window,
    synthetic_ohlcv, warmup_span_ms,
)
from g_aegisbot.backtest.funding import download_funding_all
from g_aegisbot.backtest.liquidation import load_liquidation_config
from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.config import DEFAULT_COINS, DEFAULT_LEVERAGE
from g_aegisbot.engine.strategies import ALL_STRATEGIES, validate_strategies
from g_aegisbot.engine.strategy import StrategyParams

ASSUMPTION_KEYS = ("funding", "liquidation", "slippage", "fills", "breaker")
DAY_MS = 86_400_000


def _fmt(m: dict) -> str:
    pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
    cagr = "n/a" if m["cagr"] is None else f"{m['cagr']:+.1%}"
    sortino = "n/a" if m.get("sortino") is None else f"{m['sortino']:.2f}"
    return "\n".join([
        f"  Total return     {m['total_return']:+.2%}   (${m['start_equity']:,.0f} -> ${m['end_equity']:,.0f}, CAGR {cagr})",
        f"  Max drawdown     {m['max_drawdown']:.2%}",
        f"  Sharpe / Sortino {m['sharpe']:.2f} / {sortino}   (daily returns, annualised on 365 days, risk-free 0)",
        f"  Win rate         {m['win_rate']:.1%}   over {m['trades']} trades ({m['trades_per_month']:.1f} per month)",
        f"  Profit factor    {pf}",
        f"  Avg win / loss   ${m['avg_win']:,.2f} / ${m['avg_loss']:,.2f}   expectancy ${m['expectancy']:,.2f} net, "
        f"${m['gross_expectancy']:,.2f} before costs",
        f"  Costs paid       fees ${m['total_fees']:,.2f}, funding ${m['total_funding']:,.2f}",
        f"  Time in market   {m['exposure']:.1%}   holding avg {m['avg_holding_hours']:.1f}h, median {m['median_holding_hours']:.1f}h",
        f"  Streaks          longest win {m['longest_win_streak']}, longest loss {m['longest_loss_streak']}",
    ])


def _table(title: str, rows: dict[str, dict], first: str) -> list[str]:
    out = [f"\n  {title}:", f"    {first:<24}{'trades':>7}{'net $':>12}{'gross $':>12}{'win%':>6}{'PF':>6}{'hold h':>8}{'won/lost pre-cost':>19}"]
    for k, v in sorted(rows.items(), key=lambda kv: -kv[1]["net_pnl"]):
        pf = "inf" if math.isinf(v.get("profit_factor", 0)) else f"{v.get('profit_factor', 0):.2f}"
        out.append(f"    {k:<24}{v['trades']:>7}{v['net_pnl']:>12,.2f}{v.get('gross_pnl', 0):>12,.2f}{v['win_rate']:>6.0%}{pf:>6}"
                   f"{v.get('avg_holding_hours', 0):>8.1f}{str(v.get('profitable_before_costs', '-')) + '/' + str(v.get('losing_before_costs', '-')):>19}")
    return out


def report(label: str, res, a, frames_n: int) -> None:
    first, last = res.equity.index[0], res.equity.index[-1]
    print(f"\n=== {label}: {first:%Y-%m-%d} to {last:%Y-%m-%d} (measured period only; warm-up excluded) | {a.timeframe} | "
          f"{a.leverage}x | {frames_n} pairs | strategies {','.join(res.notes['strategies'])} | slippage x{a.slippage_scale:g} | "
          f"funding {res.notes['funding']['mode']} | breaker {'off' if a.no_breaker else 'on'}")
    print(f"  Warm-up: {WARMUP_CANDLES} closed candles required per pair before its first signal")
    print(_fmt(res.metrics))
    for line in _table("By strategy", res.by_strategy, "strategy") + _table("By exit category", res.by_exit, "exit") \
            + _table("By pair", res.by_symbol, "pair"):
        print(line)
    sig = res.notes["signals"]
    print("\n  Signals (generated / kept / dropped): " + "; ".join(f"{k} {v.get('generated', 0)}/{v.get('kept', 0)}/{v.get('dropped', 0)}"
                                                              for k, v in sig.items()))
    if res.notes["signals_dropped_reasons"]:
        print("  Signals dropped because: " + "; ".join(f"{k} x{v}" for k, v in res.notes["signals_dropped_reasons"].items()))
    if res.notes["breaker_trip_days"]:
        print(f"  Daily breaker tripped on {len(res.notes['breaker_trip_days'])} days")
    print("\n  Assumptions and limits (this is not an exact exchange simulation):")
    for k in ASSUMPTION_KEYS:
        print(f"    {k:<12} {res.notes['assumptions'][k]}")


def _save(root: Path, label: str, res, extra: dict) -> Path:
    out = root / label
    out.mkdir(parents=True, exist_ok=True)
    conv = lambda o: None if isinstance(o, float) and math.isinf(o) else str(o)
    (out / "summary.json").write_text(json.dumps(
        {"label": label, "metrics": res.metrics, "by_strategy": res.by_strategy, "by_exit": res.by_exit,
         "by_symbol": res.by_symbol, "notes": res.notes, **extra}, indent=2, default=conv))
    pd.DataFrame(res.trades).to_csv(out / "trades.csv", index=False)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="G-Aegisbot historical backtester")
    ap.add_argument("--years", type=float, default=2.0, help="history length; ignored when --start is given")
    ap.add_argument("--start", help="first measured day, YYYY-MM-DD (UTC)")
    ap.add_argument("--end", help="end of the measured period, YYYY-MM-DD (UTC, exclusive). Default: today 00:00 UTC")
    ap.add_argument("--timeframe", default=StrategyParams().timeframe)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    ap.add_argument("--symbols", nargs="*", default=list(DEFAULT_COINS))
    ap.add_argument("--strategies", default="breakout,trend", help="comma list of: " + ", ".join(ALL_STRATEGIES))
    ap.add_argument("--compare", action="store_true", help="run breakout alone, trend alone and both combined on identical data")
    ap.add_argument("--split", help="chronological development,validation,out-of-sample shares, e.g. 0.5,0.25,0.25")
    ap.add_argument("--windows", type=int, default=0, help="also report N equal consecutive windows (stability check)")
    ap.add_argument("--funding", choices=["historical", "approx", "none"], default="historical",
                    help="historical: real funding rates (aborts if they cannot be fully downloaded); approx: constant scenario")
    ap.add_argument("--liq-brackets", help="JSON file of Binance maintenance-margin brackets (see liquidation.py)")
    ap.add_argument("--slippage-scale", type=float, default=1.0, help="multiply the slippage assumptions (try 2.0 as a stress test)")
    ap.add_argument("--no-breaker", action="store_true", help="disable the 4%% daily drawdown breaker")
    ap.add_argument("--legacy-risk", action="store_true",
                    help="disable the aggregate-risk, same-side and gross-notional limits added later, to compare with "
                         "older results. Never use it to judge whether the bot is safe to run")
    ap.add_argument("--cache-dir", default="data/candles")
    ap.add_argument("--out", default="backtests")
    ap.add_argument("--refresh", action="store_true", help="re-download candles instead of using the cache")
    ap.add_argument("--retries", type=int, default=5, help="attempts per request for network and rate-limit errors")
    ap.add_argument("--allow-late-start", action="store_true",
                    help="accept a symbol whose history begins after the required start (listed later); it "
                         "still needs %d candles of its own before its first signal" % WARMUP_CANDLES)
    ap.add_argument("--allow-partial", action="store_true",
                    help="continue without symbols that failed to download. Off by default: a failed symbol aborts the run")
    ap.add_argument("--from-listing", action="store_true",
                    help="start each symbol at its own listing date + 25 days (use with --individually for newer pairs)")
    ap.add_argument("--individually", action="store_true", help="run each symbol as its own single-pair backtest")
    ap.add_argument("--synthetic", action="store_true", help="random data, pipeline check only")
    ap.add_argument("--seed", type=int, default=1)
    return ap


def _day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _segments(start: pd.Timestamp, end: pd.Timestamp, split: str | None, windows: int) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    segs: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    if split:
        shares = [float(x) for x in split.split(",")]
        if len(shares) != 3 or any(x <= 0 for x in shares) or abs(sum(shares) - 1) > 1e-6:
            raise SystemExit("--split needs three positive shares that add up to 1, e.g. 0.5,0.25,0.25")
        span, cut, names = end - start, start, ("development", "validation", "out-of-sample")
        for name, share in zip(names, shares):
            nxt = end if name == names[-1] else cut + span * share
            segs.append((name, cut, nxt))
            cut = nxt
    if windows > 1:
        step = (end - start) / windows
        segs += [(f"window-{i + 1}of{windows}", start + step * i, start + step * (i + 1)) for i in range(windows)]
    return segs


def _make_cfg(a, strategies: tuple[str, ...], measure_start, measure_end, funding_history, liq) -> BacktestConfig:
    params = StrategyParams(timeframe=a.timeframe, strategies=strategies)
    if a.legacy_risk:
        params = replace(params, max_aggregate_risk_pct=0.0, max_same_direction=0, max_gross_notional_x=0.0)
    default_liq, overrides = liq
    return BacktestConfig(
        initial_capital=a.capital, leverage=a.leverage, slippage_scale=a.slippage_scale, use_breaker=not a.no_breaker,
        params=params, strategies=strategies, measure_start=measure_start, measure_end=measure_end,
        funding_mode=a.funding, funding_history=funding_history, liquidation=default_liq, liquidation_overrides=overrides)


def _run_group(a, label: str, frames: dict, measure_start, end, funding_history, liq, root: Path, extra: dict) -> None:
    combos = ([("breakout",), ("trend",), ("breakout", "trend")] if a.compare
              else [validate_strategies([s.strip() for s in a.strategies.split(",") if s.strip()])])
    summary_rows = []
    for strategies in combos:
        name = "+".join(strategies)
        try:
            res = Backtester(frames, _make_cfg(a, strategies, measure_start, end, funding_history, liq)).run()
        except ValueError as exc:
            raise SystemExit(f"Cannot run the backtest ({label}, {name}): {exc}")
        report(f"{label} | {name}", res, a, len(frames))
        out = _save(root, f"{label}/{name}", res, extra)
        summary_rows.append((name, res.metrics))
        segs = _segments(res.equity.index[0], res.equity.index[-1], a.split, a.windows)
        for seg_name, s0, s1 in segs:
            try:
                seg = Backtester(frames, _make_cfg(a, strategies, s0, s1, funding_history, liq)).run()
            except ValueError as exc:
                print(f"\n  [{seg_name}] cannot be run: {exc}")
                continue
            tag = "  <-- evaluated ONCE, not used for any decision" if seg_name == "out-of-sample" else ""
            print(f"\n  [{seg_name} {s0:%Y-%m-%d} to {s1:%Y-%m-%d}] return {seg.metrics['total_return']:+.2%}, "
                  f"max drawdown {seg.metrics['max_drawdown']:.2%}, Sharpe {seg.metrics['sharpe']:.2f}, trades {seg.metrics['trades']}, "
                  f"PF {seg.metrics['profit_factor']:.2f}{tag}")
            _save(root, f"{label}/{name}/{seg_name}", seg, extra)
        print(f"\n  Saved to {out}/")
    if len(summary_rows) > 1:
        print(f"\n=== {label}: strategy comparison (same data, same costs, same risk limits) ===")
        print(f"  {'strategy':<18}{'return':>10}{'max DD':>10}{'Sharpe':>8}{'PF':>6}{'expectancy $':>14}{'trades':>8}{'fees $':>10}{'win%':>6}")
        for name, m in summary_rows:
            pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
            print(f"  {name:<18}{m['total_return']:>10.1%}{m['max_drawdown']:>10.1%}{m['sharpe']:>8.2f}{pf:>6}{m['expectancy']:>14.2f}"
                  f"{m['trades']:>8}{m['total_fees']:>10,.0f}{m['win_rate']:>6.0%}")
        print("  One period is not proof that either strategy has an edge. Do not choose between them on this table alone.")


async def _fetch_group(a, symbols: list[str], w, funding_start_ms: int, root: Path):
    data = await download_all(symbols, w, a.cache_dir, refresh=a.refresh, retries=a.retries,
                              allow_partial=a.allow_partial, allow_late_start=a.allow_late_start)
    funding = None
    if a.funding == "historical":
        funding = await download_funding_all(list(data.frames), funding_start_ms, w.end_ms, a.cache_dir, refresh=a.refresh,
                                             retries=a.retries)
    return data, funding


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = Path(a.out) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    liq = load_liquidation_config(a.liq_brackets) if a.liq_brackets else (None, {})
    if liq[0] is None:
        from g_aegisbot.backtest.liquidation import LiquidationModel
        liq = (LiquidationModel(), {})

    if a.synthetic:
        n = WARMUP_CANDLES + int(a.years * 365 * 24)
        frames = {s: synthetic_ohlcv(n, a.seed + i, start="2024-01-01") for i, s in enumerate(a.symbols)}
        print("SYNTHETIC random data. These numbers say nothing about the strategy. Funding is the constant approximation.")
        a.funding = "approx" if a.funding == "historical" else a.funding
        _run_group(a, "synthetic", frames, None, None, None, liq, root, {"synthetic": True})
        return

    end = _day(a.end) if a.end else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    groups: list[tuple[str, list[str], datetime | None]] = []
    if a.individually:
        groups = [(s.replace("/", "_").replace(":", "_"), [s], None) for s in a.symbols]
    else:
        groups = [("portfolio", list(a.symbols), None)]

    async def _earliest(symbols):
        import ccxt.async_support as ccxt
        ex = ccxt.binanceusdm({"enableRateLimit": True})
        try:
            await ex.load_markets()
            return {s: await earliest_candle_ms(ex, s, a.timeframe) for s in symbols}
        finally:
            await ex.close()

    for label, symbols, _ in groups:
        if a.from_listing:
            try:
                first_ms = max(asyncio.run(_earliest(symbols)).values())
            except Exception as exc:
                print(f"\nABORTED. Could not establish the listing date for {symbols}: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise SystemExit(2)
            day0 = datetime.fromtimestamp(math.ceil(first_ms / DAY_MS) * DAY_MS / 1000, tz=timezone.utc)
            measure_start = day0 + timedelta(days=warmup_span_ms(a.timeframe) // DAY_MS)
            print(f"[{label}] listed {datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc):%Y-%m-%d}; measuring from "
                  f"{measure_start:%Y-%m-%d} after the warm-up")
        else:
            measure_start = _day(a.start) if a.start else end - timedelta(days=round(a.years * 365.25))
        try:
            w = make_window(a.timeframe, int(measure_start.timestamp() * 1000), int(end.timestamp() * 1000))
        except ValueError as exc:
            raise SystemExit(f"Invalid period: {exc}")
        print(f"[{label}] measured {measure_start:%Y-%m-%d} to {end:%Y-%m-%d}; downloading {a.timeframe} candles for "
              f"{len(symbols)} pairs plus {warmup_span_ms(a.timeframe) // DAY_MS} days of warm-up"
              f"{' and funding history' if a.funding == 'historical' else ''}...")
        try:
            data, funding = asyncio.run(_fetch_group(a, symbols, w, w.measure_start_ms, root))
        except (PortfolioDownloadError, DataIntegrityError) as exc:
            print(f"\nABORTED. No backtest was run.\n{exc}", file=sys.stderr)
            raise SystemExit(2)
        if data.failures:
            print(f"\nWARNING: --allow-partial is on. These symbols are EXCLUDED from the portfolio: {data.failures}")
        if not data.frames:
            raise SystemExit("No data downloaded. Check network access to Binance.")
        _run_group(a, label, data.frames, measure_start, end, funding, liq, root,
                   {"data_integrity": data.reports, "excluded_symbols": data.failures, "synthetic": False})


if __name__ == "__main__":
    main()
