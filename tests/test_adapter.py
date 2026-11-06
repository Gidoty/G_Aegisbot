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
