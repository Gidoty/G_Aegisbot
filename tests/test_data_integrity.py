"""Historical data layer: integrity checks, cache validation, pagination, retries, portfolio failures.
Everything uses a fake exchange and synthetic candles. No real network call is made."""
import asyncio
import bisect
import re

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
    """Serves canned candles like Binance USDT-M: at most `max_rows` (1,000) per request even if a larger
    `limit` is asked for. Failures are injected per call, per call number, or per symbol."""

    max_rows = 1000

    def __init__(self, symbols=(A,), rows=None):
        self.rows = {s: list(rows if rows is not None else candles()) for s in symbols}
        self.markets = {s: {} for s in symbols}
        self.calls: list[tuple[str, int]] = []
        self.errors: list[Exception] = []           # raised, in order, on the next fetch calls
        self.fail: dict[str, Exception] = {}        # always raised for this symbol
        self.stuck = False                          # ignore `since` and return the same first page
        self.market_errors: list[Exception] = []
        self.limits: list[int] = []                 # the `limit` each request asked for
        self.fail_on_call: dict[int, Exception] = {}  # 1-based request number -> error raised once
        self.overlap = False                        # a faulty server that repeats the previous candle on every later page
        self._index: dict = {}                      # per-symbol timestamp index so five-year feeds are served quickly

    async def load_markets(self):
        if self.market_errors:
            raise self.market_errors.pop(0)
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1500):
        self.calls.append((symbol, since))
        self.limits.append(limit)
        if symbol in self.fail:
            raise self.fail[symbol]
        if len(self.calls) in self.fail_on_call:
            raise self.fail_on_call.pop(len(self.calls))
        if self.errors:
            raise self.errors.pop(0)
        rows = self.rows[symbol]
        cap = min(limit, self.max_rows)
        if self.stuck:
            return rows[:cap]
        page = self._from(symbol, rows, since, cap)
        if self.overlap and page and page[0][0] > rows[0][0]:
            page = [r for r in rows if r[0] == page[0][0] - HOUR] + page[:cap - 1]
        return page

    def _from(self, symbol, rows, since, cap):
        """Rows with timestamp >= since, at most `cap`. Binary search when the feed is sorted (the normal case),
        a linear scan otherwise (feeds that a test deliberately scrambles)."""
        key = (id(rows), len(rows))
        cached = self._index.get(symbol)
        if cached is None or cached[0] != key:
            ts = [r[0] for r in rows]
            cached = (key, ts, all(a <= b for a, b in zip(ts, ts[1:])))
            self._index[symbol] = cached
        _, ts, ordered = cached
        if ordered:
            i = bisect.bisect_left(ts, since)
            return rows[i:i + cap]
        return [r for r in rows if r[0] >= since][:cap]

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
    df, rep = await download_symbol(ex, A, W, sleep=sleeper)
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
    df1, _, src1 = await load_or_download(ex, A, W, tmp_path)
    calls = len(ex.calls)
    df2, rep2, src2 = await load_or_download(ex, A, W, tmp_path)
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
    df, rep, src = await load_or_download(ex, A, W, tmp_path)
    assert src == "download" and len(ex.calls) > 0, label
    assert rep.ok and len(df) == W.expected_count
    again, _, src2 = await load_or_download(FakeExchange(), A, W, tmp_path)
    assert src2 == "cache" and len(again) == W.expected_count                    # cache was rewritten with valid data


async def test_corrupt_cache_file_is_downloaded_again(tmp_path):
    cache_path(tmp_path, A, "1h").write_text("this,is,not\na candle file")
    _, _, src = await load_or_download(FakeExchange(), A, W, tmp_path)
    assert src == "download"


async def test_bad_download_is_never_written_to_the_cache(tmp_path):
    path = seed_cache(tmp_path, candles()[:-30])                                  # stale cache
    before = path.read_bytes()
    ex = FakeExchange(rows=drop(candles(), [10, 11]))                             # the exchange also returns a gap
    with pytest.raises(DataIntegrityError, match="missing candle"):
        await load_or_download(ex, A, W, tmp_path)
    assert path.read_bytes() == before                                            # nothing invalid was cached
    assert not list(tmp_path.glob("*.tmp"))


async def test_refresh_ignores_a_valid_cache(tmp_path):
    ex = FakeExchange()
    await load_or_download(ex, A, W, tmp_path)
    n = len(ex.calls)
    _, _, src = await load_or_download(ex, A, W, tmp_path, refresh=True)
    assert src == "download" and len(ex.calls) > n


# ------------------------------------------------------------------ portfolio
async def test_portfolio_download_returns_every_symbol(tmp_path):
    ex = FakeExchange((A, B, C))
    res = await download_all([A, B, C], W, tmp_path, exchange=ex)
    assert set(res.frames) == {A, B, C} and not res.failures
    assert res.reports[A]["missing_candles"] == 0 and res.reports[A]["source"] == "download"


async def test_unlisted_symbol_fails_the_portfolio_instead_of_being_skipped(tmp_path):
    ex = FakeExchange((A, B))
    with pytest.raises(PortfolioDownloadError, match=r"C/USDT:USDT: not listed") as e:
        await download_all([A, B, C], W, tmp_path, exchange=ex)
    assert list(e.value.failures) == [C]


async def test_required_symbol_with_a_gap_fails_everything_and_names_the_symbol(tmp_path):
    ex = FakeExchange((A, B))
    ex.rows[B] = drop(candles(), [40, 41, 42])
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B], W, tmp_path, exchange=ex)
    assert list(e.value.failures) == [B] and "3 missing candle" in e.value.failures[B]
    assert B in str(e.value)


async def test_real_exchange_error_reason_is_reported(tmp_path):
    ex = FakeExchange((A, B))
    ex.fail[B] = ccxt.BadSymbol("market closed for B")
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B], W, tmp_path, exchange=ex)
    assert "BadSymbol" in e.value.failures[B] and "market closed for B" in e.value.failures[B]


async def test_network_outage_stops_the_run_and_lists_what_was_not_attempted(tmp_path):
    ex = FakeExchange((A, B, C))
    ex.fail[B] = ccxt.RequestTimeout("timed out")
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B, C], W, tmp_path, exchange=ex, sleep=Sleeper())
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
    res = await download_all([A, B], W, tmp_path, exchange=ex, allow_partial=True)
    assert set(res.frames) == {A} and B in res.failures and "before the required end" in res.failures[B]


# ------------------------------------------------------------------ Binance returns 1,000 rows per request
# A real two-year run: 730 measured days + 25 warm-up days = 755 days = 18,120 hourly candles, which needs
# 19 requests at 1,000 rows each. The old budget assumed 1,500 rows per request (18 pages) and gave up.
BIG = make_window("1h", ms("2024-11-06"), ms("2026-11-06"), now_ms=ms("2026-11-07"))
BIG_ROWS = candles(BIG.expected_first, BIG.expected_count)
PAGES_NEEDED = -(-BIG.expected_count // 1000)


def big_exchange(rows=None, **attrs):
    ex = FakeExchange(rows=BIG_ROWS if rows is None else rows)
    for k, v in attrs.items():
        setattr(ex, k, v)
    return ex


def test_the_real_two_year_window_needs_more_pages_than_the_old_budget_allowed():
    assert BIG.expected_count == 18_120 and PAGES_NEEDED == 19
    old_budget = -(-(BIG.end_ms - BIG.expected_first) // (BIG.tf_ms * 1500)) + 5
    assert old_budget == 18 < PAGES_NEEDED                              # the confirmed failure, in numbers


async def test_default_page_size_is_binances_real_1000():
    from g_aegisbot.backtest.data import BINANCE_FUTURES_PAGE_LIMIT
    assert BINANCE_FUTURES_PAGE_LIMIT == 1000
    ex = big_exchange()
    await download_symbol(ex, A, BIG)
    assert set(ex.limits) == {1000}


async def test_two_year_download_completes_across_19_requests_with_exact_cursors():
    ex = big_exchange()
    df, rep = await download_symbol(ex, A, BIG)
    assert rep.ok and len(df) == BIG.expected_count and rep.missing_total == 0
    assert len(ex.calls) == PAGES_NEEDED == 19                          # the last short page ends it: no extra empty request
    starts = [since for _, since in ex.calls]
    assert starts == [BIG.expected_first + i * 1000 * HOUR for i in range(19)]    # each page starts right after the last
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert df.index[0] == pd.Timestamp(BIG.expected_first, unit="ms", tz="UTC")
    assert df.index[-1] == pd.Timestamp(BIG.expected_last, unit="ms", tz="UTC")


async def test_asking_for_1500_still_works_when_the_server_returns_only_1000():
    """The exact situation that aborted the real run."""
    ex = big_exchange()
    df, rep = await download_symbol(ex, A, BIG, page=1500)
    assert set(ex.limits) == {1500}                                     # the caller's request is passed through
    assert len(df) == BIG.expected_count and rep.ok and len(ex.calls) == 19


async def test_no_candle_is_duplicated_or_skipped_at_page_boundaries():
    ex = big_exchange()
    df, _ = await download_symbol(ex, A, BIG)
    steps = df.index.to_series().diff().dropna().unique()
    assert list(steps) == [pd.Timedelta(hours=1)]                       # every step exactly one hour, nothing repeated
    for boundary in range(1000, BIG.expected_count, 1000):              # the two candles either side of each page break
        assert df.index[boundary] - df.index[boundary - 1] == pd.Timedelta(hours=1)


async def test_download_stops_at_the_requested_end_even_if_the_exchange_has_more():
    extra = candles(BIG.expected_first, BIG.expected_count + 300)       # the exchange also holds candles after `end`
    ex = big_exchange(rows=extra)
    df, rep = await download_symbol(ex, A, BIG)
    assert len(df) == BIG.expected_count and rep.ok
    assert df.index[-1] == pd.Timestamp(BIG.expected_last, unit="ms", tz="UTC")
    assert len(ex.calls) == 19                                          # no request is made once `end` is covered


async def test_the_download_ends_cleanly_when_the_last_page_is_exactly_full():
    from dataclasses import replace
    exact = replace(W, end_ms=W.expected_first + 2000 * HOUR, expected_last=W.expected_first + 1999 * HOUR)
    ex = FakeExchange(rows=candles(W.expected_first, 2000))
    df, rep = await download_symbol(ex, A, exact)
    assert len(df) == 2000 and rep.ok and len(ex.calls) == 2           # two full pages, then `end` is reached


async def test_empty_response_before_the_required_end_is_rejected_by_the_integrity_checks():
    ex = big_exchange(rows=BIG_ROWS[:5500])                             # five full pages, a half page, then nothing
    with pytest.raises(DataIntegrityError, match="before the required end") as e:
        await download_symbol(ex, A, BIG)
    assert e.value.symbol == A
    assert len(ex.calls) == 7                                           # 6 with data, 1 empty; it does not keep asking


async def test_a_response_that_does_not_advance_fails_on_a_two_year_window_too():
    ex = big_exchange(stuck=True)
    with pytest.raises(DataIntegrityError, match="pagination did not advance"):
        await download_symbol(ex, A, BIG)
    assert len(ex.calls) == 2                                           # caught on the second request, not after 19


async def test_a_missing_candle_exactly_at_a_page_boundary_is_rejected():
    rows = drop(BIG_ROWS, [1000])                                       # the first candle of page two
    with pytest.raises(DataIntegrityError, match=r"1 missing candle\(s\) in 1 gap\(s\)"):
        await download_symbol(big_exchange(rows=rows), A, BIG)


async def test_a_missing_candle_deep_in_the_history_is_rejected():
    rows = drop(BIG_ROWS, [9_999, 10_000, 10_001])
    with pytest.raises(DataIntegrityError, match=r"3 missing candle\(s\)"):
        await download_symbol(big_exchange(rows=rows), A, BIG)


async def test_a_server_that_repeats_a_candle_at_each_page_boundary_is_rejected_as_duplicates():
    ex = big_exchange(overlap=True)
    with pytest.raises(DataIntegrityError, match="duplicate timestamp"):
        await download_symbol(ex, A, BIG)


async def test_malformed_data_on_a_late_page_is_rejected():
    rows = [list(r) for r in BIG_ROWS]
    rows[14_500][4] = float("nan")                                      # a NaN close on page 15
    with pytest.raises(DataIntegrityError, match="NaN"):
        await download_symbol(big_exchange(rows=rows), A, BIG)


async def test_history_that_ends_early_is_rejected_after_a_full_paged_download():
    ex = big_exchange(rows=BIG_ROWS[:-48])                              # two days short of the required end
    with pytest.raises(DataIntegrityError, match="48 candle.* before the required end"):
        await download_symbol(ex, A, BIG)


async def test_a_transient_error_on_a_late_page_is_retried_without_losing_or_repeating_candles():
    ex, sleeper = big_exchange(), Sleeper()
    ex.fail_on_call = {10: ccxt.RateLimitExceeded("429 too many requests")}
    df, rep = await download_symbol(ex, A, BIG, sleep=sleeper)
    assert rep.ok and len(df) == BIG.expected_count and df.index.is_unique
    assert len(ex.calls) == 20 and sleeper.delays == [1.0]               # 19 pages plus one retried request
    assert ex.calls[9][1] == ex.calls[10][1]                            # the retry asked for the same cursor


async def test_retries_stay_bounded_on_a_late_page():
    ex, sleeper = big_exchange(), Sleeper()
    ex.fail_on_call = {i: ccxt.RequestTimeout("t") for i in range(8, 40)}
    with pytest.raises(DownloadError, match="gave up after 5 attempts"):
        await download_symbol(ex, A, BIG, sleep=sleeper, retries=5)
    assert len(ex.calls) == 7 + 5 and sleeper.delays == [1.0, 2.0, 4.0, 8.0]


async def test_the_whole_portfolio_still_fails_when_one_symbol_is_short_on_a_two_year_window(tmp_path):
    ex = FakeExchange((A, B), rows=BIG_ROWS)
    ex.rows[B] = BIG_ROWS[:-24]
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all([A, B], BIG, tmp_path, exchange=ex)
    assert list(e.value.failures) == [B] and "before the required end" in e.value.failures[B]


async def test_a_two_year_cache_round_trips_and_a_truncated_cache_is_not_trusted(tmp_path):
    ex = big_exchange()
    df1, _, src1 = await load_or_download(ex, A, BIG, tmp_path)
    calls = len(ex.calls)
    df2, _, src2 = await load_or_download(ex, A, BIG, tmp_path)
    assert (src1, src2) == ("download", "cache") and len(ex.calls) == calls
    pd.testing.assert_frame_equal(df1, df2)
    write_csv(cache_path(tmp_path, A, "1h"), BIG_ROWS[:-5])             # truncated cache must be re-downloaded
    _, _, src3 = await load_or_download(ex, A, BIG, tmp_path)
    assert src3 == "download" and len(ex.calls) > calls


async def test_fetch_rows_returns_exactly_the_required_range_and_nothing_beyond_end():
    from g_aegisbot.backtest.data import fetch_rows
    ex = big_exchange(rows=candles(BIG.expected_first, BIG.expected_count + 300))
    rows = await fetch_rows(ex, A, BIG)
    stamps = [r[0] for r in rows]
    assert len(rows) == BIG.expected_count and stamps == sorted(set(stamps))
    assert stamps[0] == BIG.expected_first and stamps[-1] == BIG.expected_last and max(stamps) < BIG.end_ms


# ====================================================================================================
# Five-year history for the full 15-pair watchlist
# The window and the symbol list are captured from the REAL production CLI (`--years 5`), so the date
# arithmetic is production's, not a copy of it. Everything below uses fake exchanges and synthetic
# candles to test the downloader only. It says nothing about any strategy.
# ====================================================================================================
import math
from types import SimpleNamespace
from unittest import mock

import g_aegisbot.backtest.__main__ as cli
from g_aegisbot.backtest import data as data_module
from g_aegisbot.config import DEFAULT_COINS

FIVE_END = "2026-11-06"


def run_cli_and_capture_window(years: float, end: str) -> SimpleNamespace:
    """Run production main() up to the download call and capture what it hands to download_all."""
    got = {}

    async def stub_download_all(symbols, w, cache_dir, **kw):
        got.update(symbols=list(symbols), window=w, kwargs=kw)
        raise PortfolioDownloadError({})                                 # stop before any backtest runs

    frozen_now = (ms(end) + 86_400_000) / 1000                           # the clock reads one day after `end`
    with mock.patch.object(cli, "download_all", stub_download_all), \
            mock.patch.object(data_module.time, "time", return_value=frozen_now):
        with pytest.raises(SystemExit):
            cli.main(["--years", str(years), "--end", end, "--out", "/nonexistent-never-written"])
    return SimpleNamespace(**got)


@pytest.fixture(scope="module")
def five():
    cap = run_cli_and_capture_window(5.0, FIVE_END)
    cap.rows = candles(cap.window.expected_first, cap.window.expected_count)
    return cap


def served(five, per_page=1000, rows=None, symbols=(A,), **attrs):
    ex = FakeExchange(symbols, rows=five.rows if rows is None else rows)
    ex.max_rows = per_page
    for k, v in attrs.items():
        setattr(ex, k, v)
    return ex


def budget_for(w, per_page=1000):
    return math.ceil((w.end_ms - w.expected_first) / (w.tf_ms * per_page)) + 5


# ---------------------------------------------------------------- the window
def test_five_year_window_is_exactly_what_the_production_cli_builds(five):
    w = five.window
    assert five.symbols == list(DEFAULT_COINS) and len(five.symbols) == 15
    assert w.timeframe == "1h" and w.tf_ms == HOUR
    assert round(5.0 * 365.25) == 1826                                   # 1826.25 rounds to 1826 days
    assert (w.end_ms - w.measure_start_ms) // DAY_MS == 1826
    assert (w.measure_start_ms - w.expected_first) // DAY_MS == 25      # the existing 25-day warm-up
    assert w.end_ms == ms(FIVE_END) and w.measure_start_ms == ms("2021-11-06")
    assert w.expected_first == ms("2021-10-12")
    assert w.expected_count == (1826 + 25) * 24 == 44_424
    assert five.kwargs.get("allow_partial") is False                     # the CLI default accepts no partial data
    assert not five.kwargs.get("allow_late_start")


def test_the_cli_window_is_the_same_object_make_window_returns(five):
    again = make_window("1h", ms("2021-11-06"), ms(FIVE_END), now_ms=ms("2026-11-07"))
    assert five.window == again


# ---------------------------------------------------------------- the request budget
def test_budget_has_room_for_1000_and_900_rows_per_page_and_is_derived_independently(five):
    w = five.window
    assert budget_for(w) == 50 == math.ceil(44_424 / 1000) + 5
    assert math.ceil(w.expected_count / 1000) == 45 and math.ceil(w.expected_count / 900) == 50
    assert budget_for(w) - 45 == 5 and budget_for(w) - 50 == 0           # 900 rows per page uses the whole budget


@pytest.mark.parametrize("per_page,requests", [(1000, 45), (950, 47), (900, 50)])
async def test_five_years_download_completely_at_each_supported_page_size(five, per_page, requests):
    w, ex = five.window, served(five, per_page)
    df, rep = await download_symbol(ex, A, w)
    assert rep.ok and len(df) == 44_424 and rep.missing_total == 0 and rep.duplicates == 0
    assert set(ex.limits) == {1000}                                     # always asks for 1,000; the fake caps what it returns
    assert len(ex.calls) == requests <= budget_for(w)
    assert df.index[0] == pd.Timestamp(w.expected_first, unit="ms", tz="UTC")
    assert df.index[-1] == pd.Timestamp(w.expected_last, unit="ms", tz="UTC")


async def test_the_smallest_page_size_the_budget_can_absorb_is_889_rows(five):
    assert math.ceil(44_424 / 889) == 50 and math.ceil(44_424 / 888) == 51          # 50 requests is the budget
    ex = served(five, 889)
    df, rep = await download_symbol(ex, A, five.window)
    assert rep.ok and len(df) == 44_424 and len(ex.calls) == 50


@pytest.mark.parametrize("per_page", [888, 880, 800, 500])
async def test_a_slower_server_is_rejected_loudly_and_never_accepted_partially(five, per_page):
    ex = served(five, per_page)
    with pytest.raises(DataIntegrityError, match="gave up after 50 pages without reaching 2026-11-06") as e:
        await download_symbol(ex, A, five.window)
    assert e.value.symbol == A and len(ex.calls) == 50                   # it stopped at the budget, not later


async def test_the_budget_boundary_is_exact_at_small_scale():
    """Exactly `budget` requests must succeed (the old loop spent one request's worth detecting completion)."""
    budget = math.ceil((W.end_ms - W.expected_first) / (HOUR * 1000)) + 5            # 6 for the 840-candle window
    ex = FakeExchange(rows=candles()); ex.max_rows = 140                             # 840 / 140 = exactly 6 requests
    df, rep = await download_symbol(ex, A, W)
    assert len(ex.calls) == budget == 6 and rep.ok and len(df) == 840
    ex2 = FakeExchange(rows=candles()); ex2.max_rows = 139                           # 7 requests needed: one too many
    with pytest.raises(DataIntegrityError, match="gave up after 6 pages"):
        await download_symbol(ex2, A, W)
    assert len(ex2.calls) == 6


# ---------------------------------------------------------------- pagination: no gaps, no repeats, exact cursors
@pytest.mark.parametrize("per_page", [1000, 900])
async def test_pagination_introduces_no_missing_or_duplicate_candles(five, per_page):
    w, ex = five.window, served(five, per_page)
    rows = await data_module.fetch_rows(ex, A, w)
    stamps = [r[0] for r in rows]
    assert len(stamps) == len(set(stamps)) == 44_424                                  # nothing repeated
    assert stamps == list(range(w.expected_first, w.end_ms, HOUR))                    # every hour, in order, no holes
    starts = [since for _, since in ex.calls]
    assert starts == [w.expected_first + i * per_page * HOUR for i in range(len(starts))]
    assert all(b - a == HOUR for a, b in zip(stamps, stamps[1:]))


@pytest.mark.parametrize("per_page,last_page_rows", [(1000, 424), (900, 324)])
async def test_the_final_page_is_the_remainder_and_no_request_follows_it(five, per_page, last_page_rows):
    ex = served(five, per_page)
    await download_symbol(ex, A, five.window)
    assert 44_424 - (len(ex.calls) - 1) * per_page == last_page_rows
    assert ex.calls[-1][1] == five.window.expected_first + (len(ex.calls) - 1) * per_page * HOUR


@pytest.mark.parametrize("per_page", [1000, 900])
async def test_download_stops_at_the_requested_end_even_if_the_exchange_holds_more(five, per_page):
    w = five.window
    extra = candles(w.expected_first, w.expected_count + 700)                         # 700 candles beyond `end`
    ex = served(five, per_page, rows=extra)
    rows = await data_module.fetch_rows(ex, A, w)
    stamps = [r[0] for r in rows]
    assert len(rows) == 44_424 and stamps[-1] == w.expected_last and max(stamps) < w.end_ms
    assert len(ex.calls) == math.ceil(44_424 / per_page)                              # nothing is requested once `end` is covered
    df, rep = await download_symbol(served(five, per_page, rows=extra), A, w)
    assert len(df) == 44_424 and rep.ok and df.index[-1] < pd.Timestamp(w.end_ms, unit="ms", tz="UTC")


async def test_a_last_page_that_ends_exactly_on_the_boundary_needs_no_extra_request(five):
    from dataclasses import replace
    w = replace(five.window, end_ms=five.window.expected_first + 45_000 * HOUR,
                expected_last=five.window.expected_first + 44_999 * HOUR)          # 45 full pages of 1,000
    ex = served(five, 1000, rows=candles(w.expected_first, 45_000))
    df, rep = await download_symbol(ex, A, w)
    assert len(df) == 45_000 and rep.ok and len(ex.calls) == 45


# ---------------------------------------------------------------- failures are loud
async def test_incomplete_history_is_rejected_with_the_coverage_message(five):
    ex = served(five, 1000, rows=five.rows[:-72])                                      # three days short of the end
    with pytest.raises(DataIntegrityError, match="72 candle.* before the required end"):
        await download_symbol(ex, A, five.window)


async def test_incomplete_history_at_the_zero_spare_page_size_is_still_rejected_just_by_the_budget_message(five):
    """900 rows per page uses all 50 requests of the budget. If the history is also short, finding the empty
    reply would need a 51st request, so the run stops on the budget instead. Either way it is rejected."""
    ex = served(five, 900, rows=five.rows[:-72])
    with pytest.raises(DataIntegrityError, match="gave up after 50 pages|before the required end"):
        await download_symbol(ex, A, five.window)
    assert len(ex.calls) == 50


@pytest.mark.parametrize("per_page,cut", [(1000, 20_500), (900, 20_700)])
async def test_an_early_empty_response_is_rejected_and_the_downloader_stops_asking(five, per_page, cut):
    ex = served(five, per_page, rows=five.rows[:cut])
    with pytest.raises(DataIntegrityError, match="before the required end"):
        await download_symbol(ex, A, five.window)
    assert len(ex.calls) == math.ceil(cut / per_page) + 1                              # the pages with data, then one empty reply


@pytest.mark.parametrize("per_page,index", [(1000, 1000), (900, 900), (1000, 22_222), (900, 44_000), (1000, 44_423)])
async def test_a_missing_candle_anywhere_is_rejected(five, per_page, index):
    ex = served(five, per_page, rows=drop(five.rows, [index]))
    with pytest.raises(DataIntegrityError, match=r"1 missing candle\(s\)|before the required end"):
        await download_symbol(ex, A, five.window)


async def test_a_run_of_missing_candles_is_rejected_and_counted(five):
    ex = served(five, 900, rows=drop(five.rows, range(30_000, 30_025)))
    with pytest.raises(DataIntegrityError, match=r"25 missing candle\(s\) in 1 gap\(s\)"):
        await download_symbol(ex, A, five.window)


async def test_repeated_candles_at_page_boundaries_are_rejected(five):
    with pytest.raises(DataIntegrityError, match="duplicate timestamp"):
        await download_symbol(served(five, 900, overlap=True), A, five.window)


async def test_history_that_starts_late_is_rejected_by_default(five):
    ex = served(five, 1000, rows=five.rows[240:])                                      # ten days of required history missing
    with pytest.raises(DataIntegrityError, match="240 candle.* after the required start"):
        await download_symbol(ex, A, five.window)


async def test_a_response_that_never_advances_is_rejected_on_the_second_request(five):
    ex = served(five, 1000, stuck=True)
    with pytest.raises(DataIntegrityError, match="pagination did not advance"):
        await download_symbol(ex, A, five.window)
    assert len(ex.calls) == 2


async def test_malformed_candles_deep_in_the_history_are_rejected(five):
    rows = [list(r) for r in five.rows]
    rows[33_333][2] = float("nan")
    with pytest.raises(DataIntegrityError, match="NaN"):
        await download_symbol(served(five, 1000, rows=rows), A, five.window)


async def test_exhausted_retries_fail_loudly_and_stay_bounded(five):
    ex, sleeper = served(five, 1000), Sleeper()
    ex.fail_on_call = {i: ccxt.ExchangeNotAvailable("HTTP 503") for i in range(30, 200)}
    with pytest.raises(DownloadError, match="gave up after 5 attempts.*ExchangeNotAvailable") as e:
        await download_symbol(ex, A, five.window, sleep=sleeper, retries=5)
    assert e.value.transient and len(ex.calls) == 29 + 5 and sleeper.delays == [1.0, 2.0, 4.0, 8.0]


async def test_a_transient_error_mid_history_is_retried_without_losing_candles(five):
    ex, sleeper = served(five, 900), Sleeper()
    ex.fail_on_call = {25: ccxt.RateLimitExceeded("429")}
    df, rep = await download_symbol(ex, A, five.window, sleep=sleeper)
    assert rep.ok and len(df) == 44_424 and df.index.is_unique and len(ex.calls) == 51
    assert ex.calls[24][1] == ex.calls[25][1] and sleeper.delays == [1.0]


# ---------------------------------------------------------------- nothing incomplete is accepted or cached
BAD_FEEDS = {
    "short by three days": lambda five: dict(rows=five.rows[:-72]),
    "early empty response": lambda five: dict(rows=five.rows[:20_000]),
    "one missing candle": lambda five: dict(rows=drop(five.rows, [31_000])),
    "late start": lambda five: dict(rows=five.rows[100:]),
    "duplicates": lambda five: dict(rows=five.rows, overlap=True),
    "no advance": lambda five: dict(rows=five.rows, stuck=True),
    "too slow for the budget": lambda five: dict(rows=five.rows, per_page=700),
}


@pytest.mark.parametrize("name", list(BAD_FEEDS))
async def test_incomplete_downloads_are_never_cached(five, tmp_path, name):
    ex = served(five, **{"per_page": 1000, **BAD_FEEDS[name](five)})
    with pytest.raises(DataIntegrityError):
        await load_or_download(ex, A, five.window, tmp_path)
    assert not cache_path(tmp_path, A, "1h").exists() and not list(tmp_path.glob("*.tmp")), name


@pytest.mark.parametrize("name", ["short by three days", "one missing candle", "late start"])
async def test_a_bad_five_year_cache_is_not_trusted_and_a_bad_redownload_does_not_replace_it(five, tmp_path, name):
    path = cache_path(tmp_path, A, "1h")
    write_csv(path, BAD_FEEDS[name](five)["rows"])
    before = path.read_bytes()
    with pytest.raises(DataIntegrityError):
        await load_or_download(served(five, 1000, **BAD_FEEDS[name](five)), A, five.window, tmp_path)
    assert path.read_bytes() == before                                                 # the invalid file was left alone


async def test_a_complete_five_year_download_is_cached_and_reused_only_while_it_stays_valid(five, tmp_path):
    ex = served(five, 900)
    df1, rep1, src1 = await load_or_download(ex, A, five.window, tmp_path)
    n = len(ex.calls)
    df2, _, src2 = await load_or_download(ex, A, five.window, tmp_path)
    assert (src1, src2) == ("download", "cache") and len(ex.calls) == n and len(df2) == 44_424
    pd.testing.assert_frame_equal(df1, df2)
    path = cache_path(tmp_path, A, "1h")
    write_csv(path, drop(five.rows, [20_000]))                                         # corrupt the cache with a gap
    _, _, src3 = await load_or_download(ex, A, five.window, tmp_path)
    assert src3 == "download" and len(ex.calls) > n                                    # re-fetched, not trusted
    _, _, src4 = await load_or_download(ex, A, five.window, tmp_path)
    assert src4 == "cache"                                                             # and the repaired cache is valid


# ---------------------------------------------------------------- the 15-pair portfolio
@pytest.mark.parametrize("per_page,per_symbol", [(1000, 45), (900, 50)])
async def test_all_15_pairs_download_five_years_and_every_pair_is_cached(five, tmp_path, per_page, per_symbol):
    ex = served(five, per_page, symbols=tuple(five.symbols))
    res = await download_all(five.symbols, five.window, tmp_path, exchange=ex)
    assert set(res.frames) == set(DEFAULT_COINS) and not res.failures
    assert all(len(f) == 44_424 and f.index.is_unique for f in res.frames.values())
    assert all(r["missing_candles"] == 0 and r["duplicates"] == 0 for r in res.reports.values())
    assert len(ex.calls) == 15 * per_symbol
    assert len(list(tmp_path.glob("*.csv"))) == 15


def portfolio(five, **overrides):
    ex = served(five, 1000, symbols=tuple(five.symbols))
    for sym, rows in overrides.items():
        ex.rows[sym] = rows
    return ex


@pytest.mark.parametrize("label,make_rows,needle", [
    ("one candle short", lambda r: r[:-1], "1 candle.* before the required end"),
    ("a gap", lambda r: drop(r, [12_345, 12_346]), r"2 missing candle\(s\)"),
    ("late listing", lambda r: r[500:], "500 candle.* after the required start"),
])
async def test_one_bad_pair_rejects_the_whole_15_pair_portfolio_and_is_named(five, tmp_path, label, make_rows, needle):
    bad = "SUI/USDT:USDT"
    ex = portfolio(five, **{bad: make_rows(five.rows)})
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all(five.symbols, five.window, tmp_path, exchange=ex)
    assert list(e.value.failures) == [bad] and re.search(needle, e.value.failures[bad]), label
    assert bad in str(e.value)
    assert not cache_path(tmp_path, bad, "1h").exists()                                # the bad pair was never cached


async def test_an_unlisted_pair_rejects_the_portfolio(five, tmp_path):
    ex = served(five, 1000, symbols=tuple(s for s in five.symbols if s != "RENDER/USDT:USDT"))
    with pytest.raises(PortfolioDownloadError, match="RENDER/USDT:USDT: not listed") as e:
        await download_all(five.symbols, five.window, tmp_path, exchange=ex)
    assert list(e.value.failures) == ["RENDER/USDT:USDT"]


async def test_a_network_failure_on_one_pair_stops_the_run_and_lists_the_rest_as_not_attempted(five, tmp_path):
    failing = five.symbols[5]
    ex = portfolio(five)
    ex.fail[failing] = ccxt.RequestTimeout("timed out")
    with pytest.raises(PortfolioDownloadError) as e:
        await download_all(five.symbols, five.window, tmp_path, exchange=ex, sleep=Sleeper())
    assert list(e.value.failures) == [failing] and "RequestTimeout" in e.value.failures[failing]
    assert e.value.not_attempted == five.symbols[6:]
    assert not any(sym in five.symbols[6:] for sym, _ in ex.calls)


async def test_partial_data_is_only_ever_returned_when_explicitly_requested(five, tmp_path):
    ex = portfolio(five, **{"DOGE/USDT:USDT": five.rows[:-24]})
    with pytest.raises(PortfolioDownloadError):                                        # default: reject
        await download_all(five.symbols, five.window, tmp_path / "a", exchange=ex)
    res = await download_all(five.symbols, five.window, tmp_path / "b", exchange=ex, allow_partial=True)
    assert "DOGE/USDT:USDT" in res.failures and len(res.frames) == 14                  # explicit opt-in, and recorded
    assert all(len(f) == 44_424 for f in res.frames.values())
