"""Live mode cannot be reached by creating an account. The only route is: paper account, keys, cap, explicit confirmation.
Unexpected trading modes fail closed everywhere an adapter is chosen."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from g_aegisbot.admin import AdminError
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import LiveCreationError, ProfileRepository
from g_aegisbot.exchange import BinanceFuturesAdapter
from tests.test_admin import do, env, profile  # noqa: F401  (env is a fixture)
from tests.test_critical_safety import build, prepared


def count(env):
    with env["sf"]() as s:
        return s.scalar(select(func.count()).select_from(UserProfile))


# ---------------------------------------------------------------- creation
@pytest.mark.parametrize("extra", [[], ["--cap", "500"], ["--balance", "100"], ["--admin"]])
def test_the_cli_rejects_direct_live_creation_and_stores_nothing(env, extra):
    code, out = do(env, "create-user", "bob", "--mode", "live", *extra)
    assert code == 1 and "cannot be created in live mode" in out and "set-mode" in out
    assert count(env) == 0 and profile(env, "bob") is None


def test_the_cli_still_creates_paper_accounts_with_the_default_balance_and_cap(env):
    for argv in (["create-user", "alice"], ["create-user", "carl", "--mode", "testnet"]):
        assert do(env, *argv)[0] == 0
    p = profile(env, "alice")
    assert p.trading_mode == "testnet" and float(p.virtual_balance) == 10000 and float(p.max_wallet_cap_usdt) == 10000
    assert profile(env, "carl").trading_mode == "testnet"


@pytest.mark.parametrize("extra", [{}, {"allow_live": True}, {"allow_live": False}])
def test_the_repository_always_rejects_live_creation(env, extra):
    with env["sf"]() as s:
        repo = ProfileRepository(s, env["box"])
        with pytest.raises((LiveCreationError, TypeError)):          # the old escape hatch no longer exists
            repo.create("x", trading_mode="live", **extra)
        with pytest.raises(ValueError):
            repo.create("y", trading_mode="paper")
        s.rollback()
    assert count(env) == 0


def test_no_application_code_has_a_live_creation_bypass():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "g_aegisbot"
    assert [str(p) for p in root.rglob("*.py") if "allow_live" in p.read_text()] == []


def test_a_live_profile_for_an_isolated_test_is_a_paper_profile_switched_directly(env):
    with env["sf"]() as s:
        p = ProfileRepository(s, env["box"]).create("fixture")
        p.trading_mode = "live"
        s.commit()
        assert s.scalar(select(UserProfile).where(UserProfile.user_id == "fixture")).trading_mode == "live"


# ---------------------------------------------------------------- the supported route to live still works, and still checks
def test_live_activation_without_credentials_is_rejected(env):
    do(env, "create-user", "alice")
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 1 and "no API keys" in out and profile(env, "alice").trading_mode == "testnet"


def test_live_activation_without_explicit_confirmation_is_rejected(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    code, out = do(env, "set-mode", "alice", "live")
    assert code == 1 and "--yes-real-money" in out and profile(env, "alice").trading_mode == "testnet"


def test_live_activation_without_a_wallet_cap_is_rejected(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    do(env, "set-cap", "alice", "0")
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 1 and "wallet cap is 0" in out and profile(env, "alice").trading_mode == "testnet"


def test_live_activation_with_everything_in_place_and_confirmation_succeeds(env):
    do(env, "create-user", "alice")
    assert do(env, "set-keys", "alice", "--verify")[0] == 0
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 0 and "One-way" in out and profile(env, "alice").trading_mode == "live"


# ---------------------------------------------------------------- unexpected modes fail closed
@pytest.mark.parametrize("mode", ["paper", "", None, "LIVE", "Testnet", "demo"])
def test_from_profile_accepts_only_live_and_testnet(mode):
    with pytest.raises(ValueError, match="unknown trading mode"):
        BinanceFuturesAdapter.from_profile(SimpleNamespace(trading_mode=mode), "k", "s")


def test_from_profile_builds_the_right_adapter_for_each_valid_mode():
    live = BinanceFuturesAdapter.from_profile(SimpleNamespace(trading_mode="live"), "k", "s")
    assert live.testnet is False
    sandbox = BinanceFuturesAdapter.from_profile(SimpleNamespace(trading_mode="testnet"), "k", "s")
    assert sandbox.testnet is True


@pytest.mark.parametrize("mode", ["paper", "", None, "LIVE"])
async def test_the_runner_refuses_an_unexpected_mode(tmp_path, monkeypatch, mode):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, _, _ = await prepared(w)
    bad = SimpleNamespace(user_id="mallory", trading_mode=mode, updated_at=None, virtual_balance=1000)
    with pytest.raises(ValueError, match="unknown trading mode"):
        await r._adapter_for(bad)
    assert "mallory" not in r._adapters                                   # nothing was cached or built


async def test_a_bad_mode_never_trades_in_the_runner_loop(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, profile_, adapter = await prepared(w)
    bad = SimpleNamespace(user_id="mallory", trading_mode="paper", updated_at=None, virtual_balance=1000, has_credentials=False)
    monkeypatch.setattr(r, "_load_profiles", lambda: [bad])
    await r.risk_pass()
    assert "could not build exchange adapter" in caplog.text
    assert adapter.count("market") == 0 and "mallory" not in r._adapters


@pytest.mark.parametrize("mode", ["paper", "", None])
def test_reconciliation_adapter_selection_rejects_an_unexpected_mode(env, mode):
    bad = SimpleNamespace(user_id="mallory", trading_mode=mode)
    with pytest.raises(AdminError, match="unknown trading mode"):
        asyncio.run(env["admin"]._gather_for(bad, "BTC/USDT:USDT", "cid", __import__("datetime").datetime.now()))
