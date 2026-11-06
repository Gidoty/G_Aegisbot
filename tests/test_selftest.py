import pytest

from g_aegisbot.engine.paper import PaperExchange
from g_aegisbot.tools import selftest
from g_aegisbot.tools.selftest import run_selftest
from tests.fakes import FakeMarket
from tests.test_safety import SYM, add_user  # noqa: F401
from tests.test_runner import world  # noqa: F401


def adapter(world, balance=10_000.0):
    world["market"].prices[SYM] = 100.0
    return PaperExchange(world["sf"], "selftest", world["market"], starting_balance=balance, clock=world["clock"])


async def test_every_step_passes_and_the_account_ends_flat(world):
    a = adapter(world)
    out = []
    results = await run_selftest(a, SYM, notional=120, say=out.append)
    assert [r.ok for r in results] == [True] * len(results) and len(results) == 12
    assert await a.fetch_open_positions() == [] and await a.fetch_stop_orders(SYM) == []
    assert any("PASS" in line and "stop-loss discoverable" in line for line in out)


async def test_a_failing_step_stops_the_run_but_cleanup_still_closes_the_position(world):
    a = adapter(world)

    async def broken(*args, **kwargs):
        raise RuntimeError("exchange rejected the stop")

    a.stop_market = broken
    out = []
    results = await run_selftest(a, SYM, notional=120, say=out.append)
    failed = [r for r in results if not r.ok]
    assert len(failed) == 1 and failed[0].name == "place stop-loss" and "rejected the stop" in failed[0].detail
    assert not any(r.name == "stop-loss discoverable" for r in results)       # later steps were skipped
    assert await a.fetch_open_positions() == []                              # the test position was closed anyway
    assert any("CLEANUP" in line and "leftover" in line for line in out)


async def test_not_finding_the_stop_is_a_failure_with_an_explanation(world):
    a = adapter(world)

    async def nothing(symbol):
        return []

    a.fetch_stop_orders = nothing
    results = await run_selftest(a, SYM, notional=120, say=lambda *_: None)
    bad = next(r for r in results if not r.ok)
    assert bad.name == "stop-loss discoverable" and "do not go live" in bad.detail
    assert await a.fetch_open_positions() == []


async def test_insufficient_balance_fails_before_any_order(world):
    a = adapter(world, balance=5.0)
    results = await run_selftest(a, SYM, notional=120, say=lambda *_: None)
    bad = [r for r in results if not r.ok]
    assert bad[0].name == "balance and equity" and "below" in bad[0].detail
    assert world["market"].prices[SYM] == 100.0 and await a.fetch_open_positions() == []


async def test_unlisted_symbol_fails_first(world):
    a = adapter(world)
    results = await run_selftest(a, "NOPE/USDT:USDT", say=lambda *_: None)
    assert results[0].name == "market listed" and not results[0].ok and len(results) == 1


def test_cli_refuses_without_keys_and_without_mainnet_confirmation(monkeypatch):
    monkeypatch.delenv("AEGIS_SELFTEST_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_SELFTEST_API_SECRET", raising=False)
    with pytest.raises(SystemExit, match="AEGIS_SELFTEST_API_KEY"):
        selftest.main([])
    monkeypatch.setenv("AEGIS_SELFTEST_API_KEY", "k")
    monkeypatch.setenv("AEGIS_SELFTEST_API_SECRET", "s")
    with pytest.raises(SystemExit, match="REAL order"):
        selftest.main(["--mainnet"])
