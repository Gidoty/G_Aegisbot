"""Historical Binance USDT-M funding rates: download, validate, cache, and a schedule the simulator reads.

Integrity rules (same philosophy as the candle downloader, nothing is invented or interpolated):
  * timestamps strictly ascending and unique, rates finite and plausible;
  * no gap longer than the 8-hour maximum funding interval (plus a few minutes of tolerance) inside the required span;
  * the history must reach both ends of the required span;
  * a symbol that fails fails the whole download unless the caller opts out;
  * a cached file is trusted only if it passes the same checks.
Funding is paid by LONGS when the rate is positive and by SHORTS when it is negative.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import ccxt.async_support as ccxt
import pandas as pd

from g_aegisbot.backtest import safeio
from g_aegisbot.backtest.data import (
    DAY_MS, DataIntegrityError, DownloadError, PortfolioDownloadError, fmt_ms, with_retries,
)

log = logging.getLogger("aegis.backtest.funding")

MAX_INTERVAL_MS = 8 * 3_600_000 + 5 * 60_000      # funding settles at least every 8 hours
MIN_INTERVAL_MS = 3_600_000 - 60_000
MAX_ABS_RATE = 0.10                               # a 10% rate in one interval is a data error, not a market
PAGE = 1000


@dataclass
class FundingSchedule:
    """Sorted funding events for one symbol."""
    ts: list[int]
    rate: list[float]

    def events_in(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """Events with start_ms <= timestamp < end_ms."""
        i, j = bisect.bisect_left(self.ts, start_ms), bisect.bisect_left(self.ts, end_ms)
        return list(zip(self.ts[i:j], self.rate[i:j]))

    def problems(self, start_ms: int, end_ms: int) -> list[str]:
        """Why this schedule cannot be trusted for [start_ms, end_ms). Empty means it can."""
        out: list[str] = []
        if not self.ts:
            return ["no funding events at all"]
        if any(b <= a for a, b in zip(self.ts, self.ts[1:])):
            out.append("timestamps are not strictly ascending and unique")
        if any(not math.isfinite(r) or abs(r) > MAX_ABS_RATE for r in self.rate):
            out.append("a funding rate is not finite or is implausibly large")
        if self.ts[0] > start_ms + MAX_INTERVAL_MS:
            out.append(f"history starts {fmt_ms(self.ts[0])}, after the required start {fmt_ms(start_ms)}")
        if self.ts[-1] < end_ms - MAX_INTERVAL_MS:
            out.append(f"history ends {fmt_ms(self.ts[-1])}, before the required end {fmt_ms(end_ms)}")
        i, j = bisect.bisect_left(self.ts, start_ms - MAX_INTERVAL_MS), bisect.bisect_right(self.ts, end_ms + MAX_INTERVAL_MS)
        window = self.ts[i:j]
        gaps = [(a, b) for a, b in zip(window, window[1:]) if b - a > MAX_INTERVAL_MS]
        if gaps:
            a, b = gaps[0]
            out.append(f"{len(gaps)} gap(s) longer than 8 hours; first between {fmt_ms(a)} and {fmt_ms(b)}")
        fast = [(a, b) for a, b in zip(window, window[1:]) if 0 < b - a < MIN_INTERVAL_MS]
        if fast:
            out.append(f"{len(fast)} interval(s) shorter than one hour (duplicate or misreported events)")
        return out


# ------------------------------------------------------------------ network
async def fetch_funding_rows(exchange, symbol: str, start_ms: int, end_ms: int, *, retries: int = 5,
                             base_delay: float = 1.0, sleep=asyncio.sleep) -> list[tuple[int, float]]:
    """Page through fetch_funding_rate_history over [start_ms, end_ms). Returns (timestamp, rate) rows, unmodified."""
    since, rows = start_ms - MAX_INTERVAL_MS, []          # one interval of margin so the first event is not missed
    max_pages = math.ceil((end_ms - since) / (3_600_000 * PAGE)) + 5
    requests = 0
    while since < end_ms:
        if requests >= max_pages:
            raise DataIntegrityError(symbol, [f"funding download gave up after {max_pages} pages"])
        requests += 1
        batch = await with_retries(lambda s=since: exchange.fetch_funding_rate_history(symbol, since=s, limit=PAGE),
                                   symbol=symbol, retries=retries, base_delay=base_delay, sleep=sleep)
        if not batch:
            break
        newest = max(int(b["timestamp"]) for b in batch)
        if newest < since:
            raise DataIntegrityError(symbol, [f"funding pagination did not advance past {fmt_ms(since)}"])
        rows.extend((int(b["timestamp"]), float(b["fundingRate"])) for b in batch if int(b["timestamp"]) < end_ms + MAX_INTERVAL_MS)
        since = newest + 1
    return rows


def rows_to_schedule(symbol: str, rows: list[tuple[int, float]], start_ms: int, end_ms: int) -> FundingSchedule:
    sched = FundingSchedule([r[0] for r in rows], [r[1] for r in rows])
    problems = sched.problems(start_ms, end_ms)
    if problems:
        raise DataIntegrityError(symbol, [f"funding: {p}" for p in problems])
    return sched


# ------------------------------------------------------------------ cache
def funding_cache_path(cache_dir: str | Path, symbol: str) -> Path:
    return Path(cache_dir) / f"funding_{symbol.replace('/', '_').replace(':', '_')}.csv"


def _write(path: Path, rows) -> None:
    safeio.guard_write(path, overwrite=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    pd.DataFrame(rows, columns=["timestamp", "rate"]).to_csv(tmp, index=False)
    os.replace(tmp, path)


async def load_or_download_funding(exchange, symbol: str, start_ms: int, end_ms: int, cache_dir: str | Path, *,
                                   refresh: bool = False, **kw) -> tuple[FundingSchedule, str]:
    path = funding_cache_path(cache_dir, symbol)
    if path.exists() and not refresh:
        try:
            safeio.guard_read(path)
            raw = pd.read_csv(path)
            if list(raw.columns) != ["timestamp", "rate"]:
                raise ValueError("unexpected columns")
            return rows_to_schedule(symbol, [(int(a), float(b)) for a, b in raw.to_numpy().tolist()], start_ms, end_ms), "cache"
        except (DataIntegrityError, ValueError, KeyError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            log.warning("%s: funding cache is not usable (%s); downloading again", symbol, exc)
    rows = await fetch_funding_rows(exchange, symbol, start_ms, end_ms, **kw)
    sched = rows_to_schedule(symbol, rows, start_ms, end_ms)
    _write(path, rows)                                      # only validated data is ever cached
    return sched, "download"


async def download_funding_all(symbols: list[str], start_ms: int, end_ms: int, cache_dir: str | Path, *,
                               refresh: bool = False, exchange=None, **kw) -> dict[str, FundingSchedule]:
    """Every symbol or nothing: any failure raises PortfolioDownloadError naming the symbols and the reasons."""
    own = exchange is None
    ex = exchange or ccxt.binanceusdm({"enableRateLimit": True})
    out: dict[str, FundingSchedule] = {}
    failures: dict[str, str] = {}
    try:
        for s in symbols:
            try:
                out[s], source = await load_or_download_funding(ex, s, start_ms, end_ms, cache_dir, refresh=refresh, **kw)
                log.info("%s: %d funding events (%s)", s, len(out[s].ts), source)
            except DownloadError as exc:
                failures[s] = exc.reason
                if exc.transient:
                    raise PortfolioDownloadError(failures, [x for x in symbols if x not in out and x not in failures])
            except DataIntegrityError as exc:
                failures[s] = "; ".join(exc.problems)
            except Exception as exc:
                failures[s] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if failures:
            raise PortfolioDownloadError(failures)
        return out
    finally:
        if own:
            await ex.close()
