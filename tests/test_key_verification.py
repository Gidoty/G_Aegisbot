"""Live mode needs API keys that passed the Binance permission check no more than 7 days ago. The record is a timestamp stored
with the ciphertext, replaced keys lose it unless they pass, and every place that can start live trading re-checks it."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.exchange.key_check import KeyCheck, interpret_restrictions
from g_aegisbot.services import (
    KEYS_FUTURE, KEYS_MAX_AGE, KEYS_MISSING, KEYS_STALE, KEYS_UNREADABLE, keys_verification_problem, live_switch_problems,
)
from tests.test_admin import NOW, do, env, profile  # noqa: F401  (env is a fixture)
from tests.test_critical_safety import build, prepared

GOOD = {"enableWithdrawals": False, "enableReading": True, "enableFutures": True, "ipRestrict": True}


# ---------------------------------------------------------------- Binance permission answers fail closed
def test_a_complete_and_correct_answer_passes():
    for withdrawals, reading, futures in ((False, True, True), ("false", "true", "true"), ("False", "TRUE", "True")):
        r = interpret_restrictions({**GOOD, "enableWithdrawals": withdrawals, "enableReading": reading, "enableFutures": futures})
        assert r.ok and not r.problems


@pytest.mark.parametrize("field", ["enableWithdrawals", "enableReading", "enableFutures"])
@pytest.mark.parametrize("bad", ["__missing__", None, "garbage", "", "0", "1", 0, 1, 0.0, [], ["true"], {}, " true"])
def test_a_missing_null_or_malformed_permission_is_refused(field, bad):
    answer = dict(GOOD)
    if bad == "__missing__":
        del answer[field]
    else:
        answer[field] = bad
    r = interpret_restrictions(answer)
    assert not r.ok and r.problems


def test_enabled_withdrawals_and_disabled_reading_or_futures_are_refused():
    assert not interpret_restrictions({**GOOD, "enableWithdrawals": True}).ok
    assert not interpret_restrictions({**GOOD, "enableWithdrawals": "true"}).ok
    assert not interpret_restrictions({**GOOD, "enableReading": False}).ok
    assert not interpret_restrictions({**GOOD, "enableFutures": "false"}).ok


@pytest.mark.parametrize("answer", [None, [], "ok", 5, {}])
def test_an_answer_that_is_not_a_permission_object_is_refused(answer):
    assert not interpret_restrictions(answer).ok


# ---------------------------------------------------------------- the verification record
def prof(stamp):
    return SimpleNamespace(keys_verified_at=stamp)


def test_verification_is_current_for_seven_days_inclusive_and_stale_after():
    assert KEYS_MAX_AGE == timedelta(days=7)
    assert keys_verification_problem(prof(NOW), NOW) is None
    assert keys_verification_problem(prof(NOW - timedelta(days=7)), NOW) is None
    assert keys_verification_problem(prof(NOW - timedelta(days=7, seconds=1)), NOW) == KEYS_STALE


def test_missing_future_dated_and_unreadable_records_are_refused():
    assert keys_verification_problem(prof(None), NOW) == KEYS_MISSING
    assert keys_verification_problem(prof(NOW + timedelta(seconds=1)), NOW) == KEYS_FUTURE
    for junk in ("2026-03-05", 12345, object(), True):
        assert keys_verification_problem(prof(junk), NOW) == KEYS_UNREADABLE


def test_a_naive_sqlite_timestamp_is_read_as_utc():
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
    assert keys_verification_problem(prof(naive), NOW) is None
    assert keys_verification_problem(prof((NOW - timedelta(days=8)).replace(tzinfo=None)), NOW) == KEYS_STALE
    assert keys_verification_problem(prof((NOW + timedelta(hours=1)).replace(tzinfo=None)), NOW) == KEYS_FUTURE


def test_live_switch_problems_lists_the_verification_problem():
    p = SimpleNamespace(has_credentials=True, max_wallet_cap_usdt=100, keys_verified_at=None)
    assert KEYS_MISSING in live_switch_problems(p, True, NOW)
    p.keys_verified_at = NOW
    assert live_switch_problems(p, True, NOW) == []
    assert live_switch_problems(p, False, NOW) == ["real-money confirmation missing"]      # confirmation is still required


# ---------------------------------------------------------------- storing, replacing and rotating credentials
def get(env, user="alice"):
    return profile(env, user)


def test_verified_credentials_store_ciphertext_and_the_time_together(env):
    do(env, "create-user", "alice")
    assert do(env, "set-keys", "alice", "--verify")[0] == 0
    p = get(env)
    assert p.has_credentials and keys_verification_problem(p, NOW) is None
    assert "KEY123456" not in (p.encrypted_api_key or "")                           # ciphertext only


def test_unverified_credentials_have_no_verification(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice")
    assert get(env).keys_verified_at is None


def test_replacing_verified_credentials_without_a_check_clears_the_verification(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    assert get(env).keys_verified_at is not None
    env["admin"].stdin.seek(0)
    do(env, "set-keys", "alice")                                                    # new keys, not verified
    assert get(env).keys_verified_at is None


def test_replacing_credentials_with_a_check_that_passes_records_a_new_time(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    env["admin"].now = lambda: NOW + timedelta(days=3)
    env["admin"].stdin.seek(0)
    do(env, "set-keys", "alice", "--verify")
    assert get(env).keys_verified_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=3)


def test_a_failed_check_changes_neither_the_keys_nor_the_existing_verification(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    before = get(env)
    env["admin"].verify = lambda k, s: KeyCheck(False, ["WITHDRAWALS ARE ENABLED"])
    env["admin"].stdin.seek(0)
    code, _ = do(env, "set-keys", "alice", "--verify")
    after = get(env)
    assert code == 1 and (after.encrypted_api_key, after.keys_verified_at) == (before.encrypted_api_key, before.keys_verified_at)


def test_key_rotation_keeps_the_verification_because_the_credentials_are_the_same(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    before = get(env)
    assert do(env, "rotate-keys")[0] == 0
    after = get(env)
    assert after.keys_verified_at == before.keys_verified_at and after.has_credentials
    assert env["box"].decrypt(after.encrypted_api_key) == "KEY123456"


def test_set_credentials_requires_an_explicit_verified_flag(env):
    with env["sf"]() as s:
        repo = ProfileRepository(s, env["box"])
        p = repo.create("x")
        with pytest.raises(TypeError):
            repo.set_credentials(p, "k", "s")


# ---------------------------------------------------------------- CLI activation
def test_cli_refuses_live_for_unverified_missing_and_stale_keys_and_accepts_current_ones(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice")                                                    # stored, never verified
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 1 and "verification missing" in out and "set-keys --verify" in out and get(env).trading_mode == "testnet"
    env["admin"].stdin.seek(0)
    do(env, "set-keys", "alice", "--verify")
    env["admin"].now = lambda: NOW + timedelta(days=8)
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 1 and "older than 7 days" in out and get(env).trading_mode == "testnet"
    env["admin"].now = lambda: NOW + timedelta(days=6)
    assert do(env, "set-mode", "alice", "live", "--yes-real-money")[0] == 0 and get(env).trading_mode == "live"


def test_cli_still_needs_the_explicit_confirmation_even_with_current_verification(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice", "--verify")
    code, out = do(env, "set-mode", "alice", "live")
    assert code == 1 and "--yes-real-money" in out and get(env).trading_mode == "testnet"


# ---------------------------------------------------------------- runner: verification gates NEW entries, never management
def set_verified(w, value):
    with w["sf"]() as s:
        s.scalar(select(UserProfile)).keys_verified_at = value
        s.commit()


def live_profile(w):
    with w["sf"]() as s:
        p = s.scalar(select(UserProfile))
        s.expunge(p)
        return p


def entries(adapter):
    return adapter.count("market", reduce_only=False)


CONDITIONS = {
    "missing": lambda w: None,
    "expired": lambda w: w["clock"].now - timedelta(days=7, seconds=1),
    "future-dated": lambda w: w["clock"].now + timedelta(hours=2),
}


@pytest.mark.parametrize("name", list(CONDITIONS))
async def test_the_profile_is_still_loaded_and_its_position_is_still_protected(tmp_path, monkeypatch, name):
    from tests.test_pending_release import add_position
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, _, adapter = await prepared(w)
    set_verified(w, CONDITIONS[name](w))
    add_position(adapter, w, amount=5.0, stop=None)                          # an open position with NO protective stop
    assert [p.user_id for p in r._load_profiles()] == ["alice"]              # not skipped
    await r.risk_pass()                                                      # reconciliation and protection still run
    assert adapter.count("stop_market") >= 1 and "alice" not in r._unverified
    assert entries(adapter) == 0


@pytest.mark.parametrize("name", list(CONDITIONS))
async def test_no_new_entry_is_submitted_under_any_invalid_verification(tmp_path, monkeypatch, name, caplog):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    set_verified(w, CONDITIONS[name](w))
    await r.run_cycle()
    await r.run_cycle()
    assert entries(adapter) == 0 and "NEW LIVE ENTRIES BLOCKED" in caplog.text


async def test_an_unreadable_verification_blocks_entries(tmp_path, monkeypatch):
    from sqlalchemy import text
    w = build(tmp_path, monkeypatch)
    r, _, _ = await prepared(w)
    assert r._entry_keys_problem("alice") is None
    with w["sf"].kw["bind"].begin() as c:
        c.execute(text("update user_profiles set keys_verified_at = 'not a date'"))      # a damaged value in the row
    assert r._entry_keys_problem("alice") == KEYS_UNREADABLE
    assert r._entry_keys_problem("nobody") is not None


async def test_a_failing_database_read_blocks_entries(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, _, _ = await prepared(w)

    def broken():
        raise RuntimeError("database unavailable")
    r.sf = broken
    assert r._entry_keys_problem("alice") == KEYS_UNREADABLE


async def test_current_verification_permits_entries_when_everything_else_passes(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    await r.run_cycle()
    assert entries(adapter) >= 1


async def test_current_verification_does_not_override_other_controls(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    with w["sf"]() as s:
        p = s.scalar(select(UserProfile))
        p.subscription_status = "expired"
        s.commit()
    await r.run_cycle()
    assert entries(adapter) == 0                                              # verified keys, but no active subscription


async def test_a_cached_adapter_cannot_bypass_the_entry_restriction(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, loaded, adapter = await prepared(w)
    assert "alice" in r._adapters and await r._adapter_for(loaded) is adapter       # the adapter is cached and reusable
    set_verified(w, None)                                                           # verification disappears; `loaded` is stale
    sig = SimpleNamespace(symbol="BTC/USDT:USDT", side="buy")
    assert await r._enter(loaded, adapter, sig) is False                            # refused at the entry itself
    assert entries(adapter) == 0
    w["clock"].now += timedelta(days=30)                                            # and expiry over time, on the same cache
    set_verified(w, w["clock"].now - timedelta(days=8))
    assert await r._enter(loaded, adapter, sig) is False and entries(adapter) == 0


async def test_rejected_credentials_raise_a_critical_alert_and_block_entries(tmp_path, monkeypatch, caplog):
    import ccxt.async_support as ccxt
    from g_aegisbot.engine import runner as runner_mod
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r = w["runner"]
    await r.start()

    async def rejected():
        raise ccxt.AuthenticationError("invalid api key")

    async def close():
        pass
    monkeypatch.setattr(runner_mod.BinanceFuturesAdapter, "from_profile",
                        staticmethod(lambda p, k, s: SimpleNamespace(exchange=SimpleNamespace(load_markets=rejected), close=close)))
    with caplog.at_level("CRITICAL"):
        await r.risk_pass()
    assert "alice" in r._unverified and "cannot be verified" in r._unverified["alice"]
    assert any(rec.levelname == "CRITICAL" and "alice" in rec.getMessage() for rec in caplog.records)
    assert r._entries_blocked("alice")                                               # nothing is assumed protected or safe
    await r.run_cycle()
    assert w["holder"].get("adapter") is None                                        # no trading adapter was ever used


async def test_an_authentication_failure_while_reading_positions_is_critical_and_not_treated_as_protected(tmp_path, monkeypatch, caplog):
    import ccxt.async_support as ccxt
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, _, adapter = await prepared(w)

    async def rejected(symbols=None):
        raise ccxt.AuthenticationError("signature invalid")
    adapter.fetch_open_positions = rejected
    with caplog.at_level("CRITICAL"):
        await r.risk_pass()
    assert "alice" in r._unverified and r._entries_blocked("alice")
    assert any(rec.levelname == "CRITICAL" for rec in caplog.records)
    await r.run_cycle()
    assert entries(adapter) == 0


async def test_paper_trading_is_unchanged_and_never_needs_verification(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    with w["sf"]() as s:
        p = s.scalar(select(UserProfile))
        p.trading_mode, p.keys_verified_at = "testnet", None
        s.commit()
    r = w["runner"]
    await r.start()
    assert [p.user_id for p in r._load_profiles()] == ["alice"]
    await r.run_cycle()
    with w["sf"]() as s:
        from g_aegisbot.db.trading_models import PaperPosition
        assert s.scalars(select(PaperPosition)).first() is not None              # a paper position opened without verification
