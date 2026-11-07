"""Historical candle download with integrity checks, a validated CSV cache, and synthetic data.

Rules enforced here (nothing is ever invented or interpolated):
  * every candle between the required start and end must be present, with no gaps;
  * timestamps must be unique, ascending and aligned to the timeframe;
  * pagination must advance on every request and must reach the requested end;
  * transient network and rate-limit errors are retried a bounded number of times with
    exponential backoff, anything else fails immediately with its real reason;
  * a symbol that fails fails the whole portfolio download unless the caller opts out;
  * a cached file is trusted only if it passes the same checks, otherwise it is downloaded again.

Window model: the measured period is [measure_start, end). Before it, at least 25 days
(and never fewer than 500 candles) of warm-up history are downloaded so indicators are
converged before the first signal. Warm-up candles are never part of reported statistics.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

log = logging.getLogger("aegis.backtest.data")

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
DAY_MS = 86_400_000
WARMUP_CANDLES = 500        # closed candles required before the first signal is evaluated
WARMUP_MIN_DAYS = 25        # extra history downloaded before the measured period
# Binance USDT-M futures returns at most 1,000 klines per request, even when a larger `limit` is asked
# for (confirmed against the live exchange). The page budget below must use this real size.
BINANCE_FUTURES_PAGE_LIMIT = 1000


# --------------------------------------------------------------------------- errors
class DataIntegrityError(Exception):
    """Downloaded or cached candles are incomplete or malformed."""

    def __init__(self, symbol: str, problems: list[str]):
        self.symbol, self.problems = symbol, list(problems)
        super().__init__(f"{symbol}: " + "; ".join(self.problems))


class DownloadError(Exception):
    """A symbol could not be fetched. `transient` is True when network retries were exhausted."""

    def __init__(self, symbol: str, reason: str, transient: bool = False):
        self.symbol, self.reason, self.transient = symbol, reason, transient
        super().__init__(f"{symbol}: {reason}")


class PortfolioDownloadError(Exception):
    """One or more required symbols failed, so the portfolio backtest cannot run."""

    def __init__(self, failures: dict[str, str], not_attempted: list[str] | None = None):
        self.failures, self.not_attempted = dict(failures), list(not_attempted or [])
        lines = [f"  {s}: {r}" for s, r in self.failures.items()]
        if self.not_attempted:
            lines.append(f"  not attempted after a network failure: {', '.join(self.not_attempted)}")
        super().__init__("Historical data download failed for required symbols:\n" + "\n".join(lines))


# --------------------------------------------------------------------------- time window
def timeframe_ms(timeframe: str) -> int:
    return int(ccxt.Exchange.parse_timeframe(timeframe) * 1000)


def warmup_span_ms(timeframe: str, candles: int = WARMUP_CANDLES, min_days: int = WARMUP_MIN_DAYS) -> int:
    """History to download before the measured period: at least 25 days AND at least 500 candles,
    rounded up to whole days."""
    span = max(min_days * DAY_MS, candles * timeframe_ms(timeframe))
    return math.ceil(span / DAY_MS) * DAY_MS


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class Window:
    timeframe: str
    tf_ms: int
    measure_start_ms: int      # first measured candle (open time)
    end_ms: int                # exclusive end, aligned to the timeframe
    download_start_ms: int     # measure_start minus the warm-up span
    expected_first: int        # open time of the first required candle
    expected_last: int         # open time of the last required candle (inclusive)

    @property
    def expected_count(self) -> int:
        return (self.expected_last - self.expected_first) // self.tf_ms + 1


def make_window(timeframe: str, measure_start_ms: int, end_ms: int, *, now_ms: int | None = None) -> Window:
    tf = timeframe_ms(timeframe)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if end_ms > now_ms // tf * tf:
        raise ValueError(f"end {fmt_ms(end_ms)} is in the future or inside the still-forming candle")
    end = end_ms // tf * tf
    if end <= measure_start_ms:
        raise ValueError("end must be after the measurement start")
    download_start = measure_start_ms - warmup_span_ms(timeframe)
    return Window(timeframe, tf, measure_start_ms, end, download_start,
                  -(-download_start // tf) * tf, end - tf)


# --------------------------------------------------------------------------- integrity checks
@dataclass
class IntegrityReport:
    count: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    duplicates: int = 0
    out_of_order: int = 0
    misaligned: int = 0
    invalid_values: int = 0
    gaps: list[tuple[int, int, int]] = field(default_factory=list)   # (candle before, candle after, missing)
    missing_at_start: int = 0
    missing_at_end: int = 0
    late_start_allowed: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def missing_total(self) -> int:
        return sum(g[2] for g in self.gaps)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> dict:
        return {"candles": self.count,
                "first": fmt_ms(self.first_ms) if self.first_ms is not None else None,
                "last": fmt_ms(self.last_ms) if self.last_ms is not None else None,
                "duplicates": self.duplicates, "out_of_order": self.out_of_order,
                "missing_candles": self.missing_total, "late_start_candles": self.missing_at_start
                if self.late_start_allowed else 0}


def analyze_rows(rows: list[list], w: Window, *, allow_late_start: bool = False) -> IntegrityReport:
    """Check raw OHLCV rows (timestamp first) against the required window. Pure function."""
    rep = IntegrityReport(count=len(rows), late_start_allowed=allow_late_start)
    if not rows:
        rep.problems.append("no candles returned")
        return rep
    ts = [int(r[0]) for r in rows]
    rep.first_ms, rep.last_ms = min(ts), max(ts)
    rep.duplicates = len(ts) - len(set(ts))
    rep.out_of_order = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    rep.misaligned = sum(1 for t in ts if t % w.tf_ms)
    vals = np.asarray([[float(x) for x in r[1:6]] for r in rows], dtype=float)
    bad = ~np.isfinite(vals).all(axis=1) | (vals[:, :4] <= 0).any(axis=1) | (vals[:, 4] < 0) | (vals[:, 1] < vals[:, 2])
    rep.invalid_values = int(bad.sum())

    uniq = sorted(set(ts))
    for a, b in zip(uniq, uniq[1:]):
        if b - a > w.tf_ms:
            rep.gaps.append((a, b, (b - a) // w.tf_ms - 1))
    if rep.first_ms > w.expected_first:
        rep.missing_at_start = (rep.first_ms - w.expected_first) // w.tf_ms
    if rep.last_ms < w.expected_last:
        rep.missing_at_end = (w.expected_last - rep.last_ms) // w.tf_ms

    p = rep.problems
    if rep.duplicates:
        p.append(f"{rep.duplicates} duplicate timestamp(s)")
    if rep.out_of_order:
        p.append(f"{rep.out_of_order} out-of-order timestamp(s) (data is not in chronological order)")
    if rep.misaligned:
        p.append(f"{rep.misaligned} timestamp(s) not aligned to the {w.timeframe} grid")
    if rep.invalid_values:
        p.append(f"{rep.invalid_values} candle(s) with NaN, non-positive or inconsistent OHLC values")
    if rep.gaps:
        g = rep.gaps[0]
        p.append(f"{rep.missing_total} missing candle(s) in {len(rep.gaps)} gap(s); first gap: {g[2]} missing "
                 f"between {fmt_ms(g[0])} and {fmt_ms(g[1])}")
    if rep.missing_at_start and not allow_late_start:
        p.append(f"history starts {fmt_ms(rep.first_ms)}, {rep.missing_at_start} candle(s) after the required "
                 f"start {fmt_ms(w.expected_first)} (symbol listed later? use allow_late_start to accept this)")
    if rep.missing_at_end:
        p.append(f"history ends {fmt_ms(rep.last_ms)}, {rep.missing_at_end} candle(s) before the required end "
                 f"{fmt_ms(w.expected_last)}")
    return rep


def rows_to_frame(symbol: str, rows: list[list], w: Window, *, allow_late_start: bool = False
                  ) -> tuple[pd.DataFrame, IntegrityReport]:
    """Validate raw rows and convert them to a frame. Raises DataIntegrityError; never repairs data."""
    rep = analyze_rows(rows, w, allow_late_start=allow_late_start)
    if not rep.ok:
        raise DataIntegrityError(symbol, rep.problems)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("timestamp").astype(float)
    return df[(df.index >= pd.Timestamp(w.expected_first, unit="ms", tz="UTC"))
              & (df.index < pd.Timestamp(w.end_ms, unit="ms", tz="UTC"))], rep


# --------------------------------------------------------------------------- network
async def with_retries(call, *, symbol: str, retries: int = 5, base_delay: float = 1.0,
                       max_delay: float = 30.0, sleep=asyncio.sleep):
    """Run `call()` up to `retries` times. Only network-class errors (timeouts, rate limits,
    exchange unavailable) are retried, with delays base, 2*base, 4*base ... capped at max_delay.
    Any other exception propagates immediately."""
    for attempt in range(1, retries + 1):
        try:
            return await call()
        except ccxt.NetworkError as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            if attempt == retries:
                raise DownloadError(symbol, f"gave up after {attempt} attempts, last error {reason}",
                                    transient=True) from exc
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            log.warning("%s: %s (attempt %d/%d), retrying in %.1fs", symbol, reason, attempt, retries, delay)
            await sleep(delay)


async def fetch_rows(exchange, symbol: str, w: Window, *, page: int = BINANCE_FUTURES_PAGE_LIMIT, retries: int = 5,
                     base_delay: float = 1.0, sleep=asyncio.sleep) -> list[list]:
    """Page through fetch_ohlcv over [expected_first, end). Returns raw rows, unmodified.

    The cursor always moves to just after the newest candle actually returned, so a server that returns
    fewer rows than requested is handled. The page budget assumes the exchange's real page size, never
    more than BINANCE_FUTURES_PAGE_LIMIT, so asking for a bigger `limit` cannot shrink the budget."""
    since, rows = w.expected_first, []
    per_page = min(page, BINANCE_FUTURES_PAGE_LIMIT)
    max_pages = math.ceil((w.end_ms - since) / (w.tf_ms * per_page)) + 5
    requests = 0
    while since < w.end_ms:
        if requests >= max_pages:            # the budget counts requests; finishing never costs an extra one
            raise DataIntegrityError(symbol, [f"gave up after {max_pages} pages without reaching {fmt_ms(w.end_ms)}"])
        requests += 1
        batch = await with_retries(
            lambda s=since: exchange.fetch_ohlcv(symbol, w.timeframe, since=s, limit=page),
            symbol=symbol, retries=retries, base_delay=base_delay, sleep=sleep)
        if not batch:
            return rows                      # nothing more; coverage validation reports any shortfall
        newest = max(int(r[0]) for r in batch)
        if newest + w.tf_ms <= since:
            raise DataIntegrityError(symbol, [
                f"pagination did not advance: asked for candles from {fmt_ms(since)}, "
                f"newest candle returned is {fmt_ms(newest)}"])
        rows.extend(r for r in batch if int(r[0]) < w.end_ms)
        since = newest + w.tf_ms
    return rows


async def download_symbol(exchange, symbol: str, w: Window, *, allow_late_start: bool = False, **kw
                          ) -> tuple[pd.DataFrame, IntegrityReport]:
    rows = await fetch_rows(exchange, symbol, w, **kw)
    return rows_to_frame(symbol, rows, w, allow_late_start=allow_late_start)


async def earliest_candle_ms(exchange, symbol: str, timeframe: str, *, retries: int = 5, sleep=asyncio.sleep) -> int:
    """Open time of the first candle the exchange holds for a symbol. Raises DataIntegrityError if it returns nothing."""
    batch = await with_retries(lambda: exchange.fetch_ohlcv(symbol, timeframe, since=0, limit=1), symbol=symbol,
                               retries=retries, sleep=sleep)
    if not batch:
        raise DataIntegrityError(symbol, ["the exchange returned no candles, so the listing date cannot be established"])
    return int(batch[0][0])


# --------------------------------------------------------------------------- cache
def cache_path(cache_dir: str | Path, symbol: str, timeframe: str) -> Path:
    return Path(cache_dir) / f"{symbol.replace('/', '_').replace(':', '_')}_{timeframe}.csv"


def read_cache_rows(path: Path) -> list[list]:
    """Raw rows exactly as stored, so duplicates and ordering problems stay visible."""
    raw = pd.read_csv(path)
    if list(raw.columns) != COLUMNS:
        raise ValueError(f"unexpected cache columns {list(raw.columns)}")
    return raw.to_numpy().tolist()


def write_cache(path: Path, df: pd.DataFrame) -> None:
    """Write validated data atomically so an interrupted run cannot leave a half-written cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = (out.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)
    out.index.name = "timestamp"
    tmp = path.with_suffix(".csv.tmp")
    out.to_csv(tmp)
    os.replace(tmp, path)


async def load_or_download(exchange, symbol: str, w: Window, cache_dir: str | Path, *, refresh: bool = False,
                           allow_late_start: bool = False, **kw) -> tuple[pd.DataFrame, IntegrityReport, str]:
    """Return (frame, report, source). A cache file is used only if it passes every integrity and
    coverage check for this window; otherwise it is ignored and the data is downloaded again."""
    path = cache_path(cache_dir, symbol, w.timeframe)
    if path.exists() and not refresh:
        try:
            df, rep = rows_to_frame(symbol, read_cache_rows(path), w, allow_late_start=allow_late_start)
            return df, rep, "cache"
        except (DataIntegrityError, ValueError, KeyError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            log.warning("%s: cache %s is not usable (%s); downloading again", symbol, path.name, exc)
    df, rep = await download_symbol(exchange, symbol, w, allow_late_start=allow_late_start, **kw)
    write_cache(path, df)                    # only validated data is ever cached
    return df, rep, "download"


# --------------------------------------------------------------------------- portfolio
@dataclass
class PortfolioData:
    frames: dict[str, pd.DataFrame]
    reports: dict[str, dict]                 # JSON-safe integrity summary per symbol
    failures: dict[str, str]                 # empty unless allow_partial was used


async def download_all(symbols: list[str], w: Window, cache_dir: str | Path, *, refresh: bool = False,
                       allow_partial: bool = False, allow_late_start: bool = False, exchange=None,
                       **kw) -> PortfolioData:
    """Download every symbol. If any required symbol fails, the whole call raises
    PortfolioDownloadError naming each failed symbol and its reason (unless allow_partial=True,
    in which case failures are returned in PortfolioData.failures and must be reported by the caller).
    After a network failure that exhausted its retries, remaining symbols are not attempted."""
    own = exchange is None
    ex = exchange or ccxt.binanceusdm({"enableRateLimit": True})
    frames: dict[str, pd.DataFrame] = {}
    reports: dict[str, dict] = {}
    failures: dict[str, str] = {}
    not_attempted: list[str] = []
    try:
        try:
            await with_retries(ex.load_markets, symbol="load_markets",
                               retries=kw.get("retries", 5), base_delay=kw.get("base_delay", 1.0),
                               sleep=kw.get("sleep", asyncio.sleep))
        except DownloadError as exc:
            raise PortfolioDownloadError({"load_markets": exc.reason}, list(symbols)) from exc
        for i, s in enumerate(symbols):
            if s not in ex.markets:
                failures[s] = "not listed on Binance USDT-M futures"
                continue
            try:
                df, rep, source = await load_or_download(ex, s, w, cache_dir, refresh=refresh,
                                                         allow_late_start=allow_late_start, **kw)
            except DownloadError as exc:
                failures[s] = exc.reason
                if exc.transient:
                    not_attempted = list(symbols[i + 1:])
                    break
                continue
            except DataIntegrityError as exc:
                failures[s] = "; ".join(exc.problems)
                continue
            except Exception as exc:  # a real exchange error such as BadSymbol or an auth problem
                failures[s] = f"{type(exc).__name__}: {str(exc)[:300]}"
                continue
            frames[s] = df
            reports[s] = {**rep.summary(), "source": source}
            log.info("%s: %d candles %s to %s (%s, no gaps)", s, len(df), fmt_ms(rep.first_ms),
                     fmt_ms(rep.last_ms), source)
        if (failures or not_attempted) and not allow_partial:
            raise PortfolioDownloadError(failures, not_attempted)
        return PortfolioData(frames, reports, failures)
    finally:
        if own:
            await ex.close()


# --------------------------------------------------------------------------- synthetic data
def synthetic_ohlcv(n: int, seed: int, *, start: str = "2024-01-01", freq: str = "1h", drift: float = 0.0,
                    vol: float = 0.006, start_price: float = 100.0) -> pd.DataFrame:
    """Random-walk candles with volatility clustering and volume that rises with move size.
    For testing the pipeline only. A strategy with no edge should NOT make money on it."""
    rng = np.random.default_rng(seed)
    regime = np.exp(np.cumsum(rng.normal(0, 0.05, n)) * 0.3)
    rets = rng.normal(drift, vol, n) * regime
    close = start_price * np.cumprod(1 + rets)
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.5, (n, 2))) * regime[:, None]
    high = np.maximum(open_, close) * (1 + wick[:, 0])
    low = np.minimum(open_, close) * (1 - wick[:, 1])
    volume = rng.lognormal(0, 0.4, n) * 100 * (1 + 3 * np.abs(rets) / vol)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
