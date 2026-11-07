"""Offline tests: no network calls are made."""
import pytest

from g_aegisbot.exchange import BinanceFuturesAdapter


def test_testnet_endpoints():
    a = BinanceFuturesAdapter("k", "s", testnet=True)
    assert "testnet.binancefuture.com" in a.exchange.urls["api"]["fapiPrivate"]
    assert a.exchange.isSandboxModeEnabled


def test_mainnet_endpoints():
    a = BinanceFuturesAdapter("k", "s", testnet=False)
    assert "fapi.binance.com" in a.exchange.urls["api"]["fapiPrivate"]
    assert not a.exchange.isSandboxModeEnabled


def test_from_profile_defaults_to_testnet():
    class P:
        trading_mode = "testnet"
    class L:
        trading_mode = "live"
    assert BinanceFuturesAdapter.from_profile(P(), "k", "s").testnet is True
    assert BinanceFuturesAdapter.from_profile(L(), "k", "s").testnet is False


@pytest.mark.asyncio
async def test_leverage_range_enforced():
    a = BinanceFuturesAdapter("k", "s")
    with pytest.raises(ValueError):
        await a.configure_symbol("BTC/USDT:USDT", 10)
    await a.close()


# ---- evidence queries used to reconcile an unresolved entry: incomplete is never reported as empty
import ccxt.async_support as ccxt_async
from types import SimpleNamespace as NS

from g_aegisbot.exchange.binance_futures import (
    EVIDENCE_MAX_PAGES, EVIDENCE_PAGE, BinanceFuturesAdapter as _Adapter, EvidenceIncomplete,
)


def _rows(n, start=0):
    return [{"id": str(start + i), "timestamp": 1_000_000 + start + i, "clientOrderId": f"c{start + i}", "status": "closed",
             "filled": 1.0, "amount": 1.0, "order": str(start + i), "side": "buy"} for i in range(n)]


async def test_a_short_page_is_the_complete_answer_and_the_window_is_passed_to_the_exchange():
    a = _Adapter("k", "s")
    seen = []

    async def fetch_orders(symbol, since, limit, params):
        seen.append((symbol, since, limit, params))
        return _rows(3)

    a.exchange = NS(fetch_orders=fetch_orders)
    out = await a.fetch_orders_history("BTC/USDT:USDT", 1_000_000, 2_000_000)
    assert len(out) == 3 and seen == [("BTC/USDT:USDT", 1_000_000, EVIDENCE_PAGE, {"until": 2_000_000})]


async def test_full_pages_are_followed_until_a_short_page_and_rows_are_not_duplicated():
    a = _Adapter("k", "s")
    pages = [_rows(EVIDENCE_PAGE), _rows(EVIDENCE_PAGE, EVIDENCE_PAGE - 1), _rows(5, 2 * EVIDENCE_PAGE - 2)]   # pages overlap by one row
    cursors = []

    async def fetch_my_trades(symbol, since, limit, params):
        cursors.append(since)
        return pages[len(cursors) - 1]

    a.exchange = NS(fetch_my_trades=fetch_my_trades)
    out = await a.fetch_account_trades("BTC/USDT:USDT", 0, 10_000_000)
    assert len(cursors) == 3 and cursors[1] == 1_000_000 + EVIDENCE_PAGE - 1 + 1
    assert len({t["id"] for t in out}) == len(out) == 2 * EVIDENCE_PAGE + 3


async def test_running_out_of_pages_is_incomplete_not_done():
    a = _Adapter("k", "s")
    calls = {"n": 0}

    async def fetch_orders(symbol, since, limit, params):
        calls["n"] += 1
        return _rows(EVIDENCE_PAGE, start=calls["n"] * EVIDENCE_PAGE)          # always full, always advancing

    a.exchange = NS(fetch_orders=fetch_orders)
    with pytest.raises(EvidenceIncomplete, match="pages"):
        await a.fetch_orders_history("X", 0, 10_000_000)
    assert calls["n"] == EVIDENCE_MAX_PAGES


async def test_a_full_page_that_does_not_advance_is_incomplete():
    a = _Adapter("k", "s")

    async def fetch_orders(symbol, since, limit, params):
        return [{**r, "timestamp": 5} for r in _rows(EVIDENCE_PAGE)]

    a.exchange = NS(fetch_orders=fetch_orders)
    with pytest.raises(EvidenceIncomplete, match="did not advance"):
        await a.fetch_orders_history("X", 1_000_000, 2_000_000)


async def test_a_window_longer_than_the_endpoint_serves_is_incomplete_and_asks_nothing():
    a = _Adapter("k", "s")

    async def fetch_orders(*args, **kw):
        raise AssertionError("must not be called")

    a.exchange = NS(fetch_orders=fetch_orders, fetch_my_trades=fetch_orders)
    day = 24 * 3600 * 1000
    with pytest.raises(EvidenceIncomplete, match="7 days"):
        await a.fetch_orders_history("X", 0, 8 * day)
    with pytest.raises(EvidenceIncomplete, match="7 days"):
        await a.fetch_account_trades("X", 0, 8 * day)


async def test_exchange_errors_propagate_instead_of_looking_like_an_empty_history():
    a = _Adapter("k", "s")

    async def boom(*args, **kw):
        raise ccxt_async.RequestTimeout("timed out")

    a.exchange = NS(fetch_orders=boom, fetch_my_trades=boom, fetch_open_orders=boom)
    for call in (a.fetch_orders_history("X", 0, 1000), a.fetch_account_trades("X", 0, 1000), a.fetch_conditional_orders("X")):
        with pytest.raises(ccxt_async.RequestTimeout):
            await call


async def test_the_conditional_listing_asks_for_trigger_orders():
    a = _Adapter("k", "s")
    seen = []

    async def fetch_open_orders(symbol, params=None):
        seen.append(params)
        return [{"id": 9, "clientOrderId": "c9", "type": "STOP_MARKET", "side": "sell", "status": "open", "amount": 1.0}]

    a.exchange = NS(fetch_open_orders=fetch_open_orders)
    out = await a.fetch_conditional_orders("X")
    assert seen == [{"trigger": True}] and out[0]["clientOrderId"] == "c9"
