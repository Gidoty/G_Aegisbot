"""Historical data layer: integrity checks, cache validation, pagination, retries, portfolio failures.
Everything uses a fake exchange and synthetic candles. No real network call is made."""
import asyncio

import ccxt.async_support as ccxt
import pandas as pd
import pytest

from g_aegisbot.backtest import data
from g_aegisbot.backtest.data import (
    COLUMNS, DAY_MS, DataIntegrityError, DownloadError, PortfolioDownloadError, analyze_rows, cache_path,
    download_all, download_symbol, load_or_download, make_window, rows_to_frame, warmup_span_ms, with_retries,
)

HOUR = 3_600_000
ms = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)
A, B, C = "A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"

W = make_window("1h", ms("2025-02-01"), ms("2025-02-11"), now_ms=ms("2025-03-01"))   # 10 measured days + 25 warm-up days


def candles(first_ms=None, n=None):
    first_ms = W.expected_first if first_ms is None else first_ms
    n = W.expected_count if n is None else n
    return [[first_ms + i * HOUR, 100 + i * 0.01, 101 + i * 0.01, 99 + i * 0.01, 100.5 + i * 0.01, 10.0] for i in range(n)]


class FakeExchange:
    """Serves canned candles. Failures are injected per call or per symbol."""

    def __init__(self, symbols=(A,), rows=None):
        self.rows = {s: list(rows if rows is not None else candles()) for s in symbols}
        self.markets = {s: {} for s in symbols}
        self.calls: list[tuple[str, int]] = []
        self.errors: list[Exception] = []           # raised, in order, on the next fetch calls
        self.fail: dict[str, Exception] = {}        # always raised for this symbol
        self.stuck = False                          # ignore `since` and return the same first page
        self.market_errors: list[Exception] = []

    async def load_markets(self):
        if self.market_errors:
            raise self.market_errors.pop(0)
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1500):
        self.calls.append((symbol, since))
        if symbol in self.fail:
            raise self.fail[symbol]
        if self.errors:
            raise self.errors.pop(0)
        rows = self.rows[symbol]
        if self.stuck:
            return rows[:limit]
        return [r for r in rows if r[0] >= since][:limit]

    async def close(self):
        pass


class Sleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, d):
        self.delays.append(d)


def drop(rows, idx):
    return [r for i, r in enumerate(rows) if i not in set(idx)]


def write_csv(path, rows):
    pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False)


# ------------------------------------------------------------------ window and warm-up span
def test_window_downloads_at_least_25_days_and_500_candles_of_warmup():
    assert W.measure_start_ms - W.download_start_ms == 25 * DAY_MS
    assert W.expected_first == W.download_start_ms
    assert (W.measure_start_ms - W.expected_first) // HOUR >= 500
    four_hour = 4 * HOUR
    assert warmup_span_ms("4h") >= 500 * four_hour                      # 500 four-hour candles exceed 25 days
    assert warmup_span_ms("1h") == 25 * DAY_MS


def test_window_rejects_future_or_inverted_period():
    with pytest.raises(ValueError, match="future"):
        make_window("1h", ms("2025-02-01"), ms("2025-04-01"), now_ms=ms("2025-03-01"))
    with pytest.raises(ValueError, match="after"):
        make_window("1h", ms("2025-02-10"), ms("2025-02-01"), now_ms=ms("2025-03-01"))


# ------------------------------------------------------------------ integrity analysis
def test_complete_data_passes():
    rep = analyze_rows(candles(), W)
    assert rep.ok and rep.count == W.expected_count and rep.missing_total == 0


def test_missing_hourly_candles_are_reported_not_filled():
    rows = drop(candles(), [100, 101, 102, 400])                     # a 3-candle gap and a 1-candle gap
    rep = analyze_rows(rows, W)
    assert not rep.ok and len(rep.gaps) == 2 and rep.missing_total == 4
    assert [g[2] for g in rep.gaps] == [3, 1]
    with pytest.raises(DataIntegrityError, match=r"4 missing candle\(s\) in 2 gap\(s\)") as e:
        rows_to_frame(A, rows, W)
    assert e.value.symbol == A


def test_incomplete_coverage_at_start_fails_unless_late_start_is_allowed():
    rows = candles()[10:]
    with pytest.raises(DataIntegrityError, match="10 candle.* after the required start"):
        rows_to_frame(A, rows, W)
    df, rep = rows_to_frame(A, rows, W, allow_late_start=True)
    assert len(df) == W.expected_count - 10 and rep.missing_at_start == 10
    assert rep.summary()["late_start_candles"] == 10


def test_late_start_does_not_excuse_a_gap():
    rows = drop(candles()[10:], [50])
    with pytest.raises(DataIntegrityError, match="missing candle"):
        rows_to_frame(A, rows, W, allow_late_start=True)


def test_incomplete_coverage_at_end_always_fails():
    rows = candles()[:-5]
    for late in (False, True):
        with pytest.raises(DataIntegrityError, match="5 candle.* before the required end"):
            rows_to_frame(A, rows, W, allow_late_start=late)


def test_duplicate_timestamps_fail_and_are_not_silently_dropped():
    rows = candles()
    rows.insert(200, list(rows[200]))
    with pytest.raises(DataIntegrityError, match="1 duplicate timestamp"):
        rows_to_frame(A, rows, W)


def test_out_of_order_timestamps_fail_and_are_not_silently_sorted():
    rows = candles()
    rows[300], rows[301] = rows[301], rows[300]
    with pytest.raises(DataIntegrityError, match="out-of-order"):
        rows_to_frame(A, rows, W)


def test_misaligned_and_invalid_values_fail():
    rows = candles()
    rows[5][0] += 1234
    with pytest.raises(DataIntegrityError, match="not aligned"):
        rows_to_frame(A, rows, W)
    rows = candles()
    rows[7][2] = float("nan")
    with pytest.raises(DataIntegrityError, match="NaN"):
        rows_to_frame(A, rows, W)
    rows = candles()
    rows[9][2], rows[9][3] = 120.0, 130.0                            # high 101 < low 120
    with pytest.raises(DataIntegrityError, match="inconsistent OHLC"):
        rows_to_frame(A, rows, W)


def test_empty_response_fails():
    with pytest.raises(DataIntegrityError, match="no candles"):
        rows_to_frame(A, [], W)


# ------------------------------------------------------------------ pagination
async def test_download_pages_through_the_whole_range():
    ex = FakeExchange()
    df, rep = await download_symbol(ex, A, W, page=300)
    assert len(df) == W.expected_count and rep.ok
    assert len(ex.calls) >= 3
    assert [c[1] for c in ex.calls] == sorted(c[1] for c in ex.calls)         # requests move forward only


async def test_pagination_that_does_not_advance_is_detected_and_bounded():
    ex = FakeExchange()
    ex.stuck = True                                                           # always returns the first page
    with pytest.raises(DataIntegrityError, match="pagination did not advance") as e:
        await download_symbol(ex, A, W, page=100)
    assert e.value.symbol == A
    assert len(ex.calls) <= 3                                                 # stopped immediately, no endless loop


async def test_exchange_that_stops_early_is_reported_as_missing_end_coverage():
    ex = FakeExchange(rows=candles()[:150])
    with pytest.raises(DataIntegrityError, match="before the required end"):
        await download_symbol(ex, A, W, page=100)
    assert len(ex.calls) <= 4


# ------------------------------------------------------------------ retries
async def test_transient_failures_are_retried_with_exponential_backoff():
    ex, sleeper = FakeExchange(), Sleeper()
    ex.errors = [ccxt.RequestTimeout("timeout"), ccxt.RateLimitExceeded("429 too many requests")]
    df, rep = await download_symbol(ex, A, W, page=2000, sleep=sleeper)
    assert rep.ok and len(df) == W.expected_count
    assert sleeper.delays == [1.0, 2.0]


async def test_retries_are_bounded_and_the_real_reason_is_reported():
    ex, sleeper = FakeExchange(), Sleeper()
    ex.errors = [ccxt.ExchangeNotAvailable("HTTP 503 maintenance")] * 50
    with pytest.raises(DownloadError, match="gave up after 5 attempts.*ExchangeNotAvailable.*503") as e:
        await download_symbol(ex, A, W, retries=5, sleep=sleeper)
    assert e.value.transient and e.value.symbol == A
    assert len(ex.calls) == 5                                                  # exactly the allowed attempts
    assert sleeper.delays == [1.0, 2.0, 4.0, 8.0]                              # no sleep after the final attempt


async def test_backoff_delay_is_capped():
    sleeper, n = Sleeper(), 0

    async def always_fails():
        nonlocal n
        n += 1
        raise ccxt.DDoSProtection("rate limited")

    with pytest.raises(DownloadError):
        await with_retries(always_fails, symbol=A, retries=4, base_delay=20.0, max_delay=30.0, sleep=sleeper)
    assert sleeper.delays == [20.0, 30.0, 30.0] and n == 4


async def test_non_transient_errors_are_not_retried():
    ex, sleeper = FakeExchange(), Sleeper()
    ex.errors = [ccxt.BadSymbol("binanceusdm does not have market symbol A/USDT:USDT")]
    with pytest.raises(ccxt.BadSymbol):
        await download_symbol(ex, A, W, sleep=sleeper)
    assert len(ex.calls) == 1 and sleeper.delays == []


# ------------------------------------------------------------------ cache
def seed_cache(tmp_path, rows):
    path = cache_path(tmp_path, A, "1h")
    write_csv(path, rows)
    return path


async def test_valid_cache_is_reused_without_network(tmp_path):
    ex = FakeExchange()
    df1, _, src1 = await load_or_download(ex, A, W, tmp_path, page=2000)
    calls = len(ex.calls)
    df2, rep2, src2 = await load_or_download(ex, A, W, tmp_path, page=2000)
    assert (src1, src2) == ("download", "cache") and len(ex.calls) == calls and rep2.ok
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.parametrize("label,make_rows", [
    ("gap", lambda: drop(candles(), [300, 301])),
    ("stale: ends before the required end", lambda: candles()[:-30]),
    ("does not reach the required start", lambda: candles()[24:]),
    ("duplicate timestamps", lambda: candles()[:50] + [candles()[49]] + candles()[50:]),
    ("out of order", lambda: candles()[:10] + [candles()[11], candles()[10]] + candles()[12:]),
])
async def test_invalid_cache_is_rejected_and_downloaded_again(tmp_path, label, make_rows):
    path = seed_cache(tmp_path, make_rows())
    ex = FakeExchange()
    df, rep, src = await load_or_download(ex, A, W, tmp_path, page=2000)
    assert src == "download" and len(ex.calls) > 0, label
    assert rep.ok and len(df) == W.expected_count
    again, _, src2 = await load_or_download(FakeExchange(), A, W, tmp_path, page=2000)
    assert src2 == "cache" and len(again) == W.expected_count                    # cache was rewritten with valid data


async def test_corrupt_cache_file_is_downloaded_again(tmp_path):
    cache_path(tmp_path, A, "1h").write_text("this,is,not\na candle file")
    _, _, src = await load_or_download(FakeExchange(), A, W, tmp_path, page=2000)
    assert src == "download"


async def test_bad_download_is_never_written_to_the_cache(tmp_path):
    path = seed_cache(tmp_path, candles()[:-30])                                  # stale cache
    before = path.read_bytes()
    ex = FakeExchange(rows=drop(candles(), [10, 11]))                             # the exchange also returns a gap
    with pytest.raises(DataIntegrityError, match="missing candle"):
        await load_or_download(ex, A, W, tmp_path, page=2000)
    assert path.read_bytes() == before                                            # nothing invalid was cached
    assert not list(tmp_path.glob("*.tmp"))


async def test_refresh_ignores_a_valid_cache(tmp_path):
    ex = FakeExchange()
    await load_or_download(ex, A, W, tmp_path, page=2000)
    n = len(ex.calls)
    _, _, src = await load_or_download(ex, A, W, tmp_path, page=2000, refresh=True)
    assert src == "download" and len(ex.calls) > n


# ------------------------------------------------------------------ portfolio
async def test_portfolio_download_returns_every_symbol(tmp_path):
    ex = FakeExchange((A, B, C))
    res = await download_all([A, B, C], W, tmp_path, exchange=ex, page=2000)
    assert set(res.frames) == {A, B, C} and not res.failures
    assert res.reports[A]["missing_candles"] == 0 and res.reports[A]["source"] == "download"


async def test_unlisted_symbol_fails_the_portfolio_instead_of_being_skipped(tmp_path):
    ex = FakeExchange((A, B))
    with pytest.raises(PortfolioDownloadError, match=r"C/USDT:USDT: not listed") as e:
        await download_all([A, B, C], W, tmp_path, exchange=ex, page=2000)
    assert list(e.value.failures) == [C]


async def test_required_symbol_with_a_gap_fails_everything_and_names_the_symbol(tmp_path):
    ex = FakeExchange((A, B))
    ex.rows[B] = drop(candles(), [40, 41, 42])
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B], W, tmp_path, exchange=ex, page=2000)
    assert list(e.value.failures) == [B] and "3 missing candle" in e.value.failures[B]
    assert B in str(e.value)


async def test_real_exchange_error_reason_is_reported(tmp_path):
    ex = FakeExchange((A, B))
    ex.fail[B] = ccxt.BadSymbol("market closed for B")
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B], W, tmp_path, exchange=ex, page=2000)
    assert "BadSymbol" in e.value.failures[B] and "market closed for B" in e.value.failures[B]


async def test_network_outage_stops_the_run_and_lists_what_was_not_attempted(tmp_path):
    ex = FakeExchange((A, B, C))
    ex.fail[B] = ccxt.RequestTimeout("timed out")
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B, C], W, tmp_path, exchange=ex, page=2000, sleep=Sleeper())
    assert B in e.value.failures and "RequestTimeout" in e.value.failures[B]
    assert e.value.not_attempted == [C]
    assert all(sym != C for sym, _ in ex.calls)                                   # C was never requested


async def test_load_markets_failure_is_reported(tmp_path):
    ex = FakeExchange((A,))
    ex.market_errors = [ccxt.ExchangeNotAvailable("451 restricted location")] * 10
    with pytest.raises(PortfolioDownloadError, match="load_markets.*451"):
        await download_all([A], W, tmp_path, exchange=ex, sleep=Sleeper())


async def test_allow_partial_is_explicit_and_records_the_exclusion(tmp_path):
    ex = FakeExchange((A, B))
    ex.rows[B] = candles()[:-10]
    res = await download_all([A, B], W, tmp_path, exchange=ex, page=2000, allow_partial=True)
    assert set(res.frames) == {A} and B in res.failures and "before the required end" in res.failures[B]
