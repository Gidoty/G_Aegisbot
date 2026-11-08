"""The live-trading gate: real money only when every technical, security, strategy-approval and risk condition holds.
Every test here uses the REAL gate (the rest of the suite runs with it stubbed open, see conftest.py)."""
import json
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import signing as sg
from g_aegisbot.engine import livegate as lg
from g_aegisbot.engine.strategy import StrategyParams
from tests.signing_helpers import NOW, approved_repo, new_key

pytestmark = pytest.mark.real_gate
real_evaluate = lg.evaluate

P = StrategyParams()
DD = 0.04
CODE = "c" * 64
ENV = {lg.LIVE_ENV: "1"}


def profile(cap=Decimal("200"), leverage=3, verified=NOW - timedelta(days=1)):
    return SimpleNamespace(max_wallet_cap_usdt=cap, leverage=leverage, keys_verified_at=verified)


def payload(root, params=P, dd=DD, at=NOW, **over):
    approval = fz.validate_protocol_approval(root, now=at)
    rec = lg.build_payload(params=params, drawdown_limit=dd, candidate="X-1", version="v1", authorised_by="owner",
                           authorised_at=at - timedelta(days=1), valid_days=30, freeze_sha256="a" * 64,
                           acceptance_report_sha256="b" * 64, paper_soak_days=30, max_wallet_cap_usdt=500.0, max_leverage=3,
                           protocol_sha256=approval["protocol_sha256"], code_sha256=CODE)
    rec.update(over)
    return rec


def write(root, rec, key, purpose=sg.PURPOSE_LIVE, sign=True):
    out = sg.sign_record(rec, key, purpose) if sign else rec
    path = root / lg.LIVE_AUTH_DOC
    path.write_text(json.dumps(out))
    return path


@pytest.fixture
def world(tmp_path):
    root, key = approved_repo(tmp_path)
    write(root, payload(root), key)
    return root, key


def gate(root, **kw):
    args = dict(params=P, drawdown_limit=DD, profile=profile(), lock_held=True, repo_root=root, now=NOW, env=ENV,
                code_identity=lambda: (CODE, True))
    args.update(kw)
    return lg.evaluate(**args)


def failed(res):
    return sorted(k for k, v in res.checks.items() if not v)


# ------------------------------------------------------------------------------------------------ default and happy path
def test_the_gate_is_closed_by_default_in_this_repository():
    if (mf.REPO_ROOT / lg.LIVE_AUTH_DOC).exists():
        pytest.skip("the owner has added a live authorisation to this checkout")
    res = lg.evaluate(params=P, profile=profile(), env=ENV)
    assert res.allowed is False and res.problems
    assert any("G2" in p for p in res.problems) and any("G3" in p for p in res.problems)


def test_everything_in_place_opens_the_gate(world):
    res = gate(world[0])
    assert res.allowed is True and res.problems == [] and all(res.checks.values())
    assert {"G1 switch", "G2 protocol", "G3 record", "G4 strategy", "G4 protocol", "G5 code", "G6 risk", "G7 account",
            "G7 keys", "G8 runner"} <= set(res.checks)


def test_a_result_is_falsy_when_refused_and_the_gate_never_raises(world):
    root, _ = world
    assert not gate(root, env={})
    assert not gate(root, code_identity=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not gate(root, profile=object())                                    # a malformed profile is a refusal, not a crash


# ------------------------------------------------------------------------------------------------ G1, G8
@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "2", " 1"])
def test_the_operator_switch_must_be_exactly_1(world, value):
    env = {} if value is None else {lg.LIVE_ENV: value}
    res = gate(world[0], env=env)
    assert not res.allowed and "G1 switch" in failed(res)


def test_the_switch_can_be_left_to_the_runner_for_profile_checks(world):
    res = gate(world[0], env={}, check_switch=False)
    assert res.allowed and "G1 switch" not in res.checks


def test_the_single_runner_lock_must_be_held(world):
    res = gate(world[0], lock_held=False)
    assert not res.allowed and failed(res) == ["G8 runner"]


# ------------------------------------------------------------------------------------------------ G2 protocol approval
def test_no_approval_no_live(world):
    root, _ = world
    (root / fz.APPROVAL_DOC).unlink()
    assert "G2 protocol" in failed(gate(root))


def test_changing_the_protocol_after_approval_closes_the_gate(world):
    root, _ = world
    (root / mf.PROTOCOL_DOC).write_text("a different protocol")
    res = gate(root)
    assert not res.allowed and "G2 protocol" in failed(res)


def test_the_authorisation_must_name_the_approved_protocol(world):
    root, key = world
    write(root, payload(root, protocol_sha256="d" * 64), key)
    res = gate(root)
    assert not res.allowed and "G4 protocol" in failed(res)


# ------------------------------------------------------------------------------------------------ G3 the signed record
def test_a_missing_record_closes_the_gate(world):
    root, _ = world
    (root / lg.LIVE_AUTH_DOC).unlink()
    res = gate(root)
    assert not res.allowed and "G3 record" in failed(res)


def test_an_unsigned_record_is_refused(world):
    root, _ = world
    write(root, payload(root), None, sign=False)
    assert "G3 record" in failed(gate(root))


def test_a_record_signed_by_an_untrusted_key_is_refused(world):
    root, _ = world
    write(root, payload(root), new_key())
    assert "G3 record" in failed(gate(root))


@pytest.mark.parametrize("purpose", [sg.PURPOSE_APPROVAL, sg.PURPOSE_FREEZE])
def test_a_signature_made_for_another_purpose_is_refused(world, purpose):
    root, key = world
    write(root, payload(root), key, purpose=purpose)
    assert "G3 record" in failed(gate(root))


def test_a_record_edited_after_signing_is_refused(world):
    root, key = world
    rec = sg.sign_record(payload(root), key, sg.PURPOSE_LIVE)
    rec["max_wallet_cap_usdt"] = 1_000_000.0
    (root / lg.LIVE_AUTH_DOC).write_text(json.dumps(rec))
    assert "G3 record" in failed(gate(root))


@pytest.mark.parametrize("change,now_shift", [
    ({}, timedelta(days=31)),                                                   # expired
    ({"authorised_at": "2026-12-01T00:00:00+00:00", "valid_until": "2026-12-31T00:00:00+00:00"}, timedelta(0)),   # not yet valid
])
def test_a_record_outside_its_validity_window_is_refused(world, change, now_shift):
    root, key = world
    if change:
        write(root, payload(root, **change), key)
    res = gate(root, now=NOW + now_shift)
    assert not res.allowed and "G3 record" in failed(res)


def test_a_record_valid_for_more_than_90_days_is_refused(world):
    root, key = world
    rec = payload(root, valid_until=(NOW - timedelta(days=1) + timedelta(days=91)).isoformat())
    write(root, rec, key)
    assert "G3 record" in failed(gate(root))


def test_the_builder_refuses_a_validity_over_90_days():
    with pytest.raises(ValueError):
        lg.build_payload(params=P, drawdown_limit=DD, candidate="c", version="v", authorised_by="o", authorised_at=NOW,
                         valid_days=91, freeze_sha256="a" * 64, acceptance_report_sha256="b" * 64, paper_soak_days=30,
                         max_wallet_cap_usdt=1.0, max_leverage=3, protocol_sha256="d" * 64, code_sha256=CODE)


@pytest.mark.parametrize("change", [
    {"status": "pending"}, {"acceptance_outcome": "FAIL"}, {"acceptance_outcome": "INCONCLUSIVE"}, {"paper_soak_days": 27},
    {"paper_soak_days": True}, {"paper_soak_days": "30"}, {"max_wallet_cap_usdt": 0}, {"max_wallet_cap_usdt": -5},
    {"max_wallet_cap_usdt": True}, {"max_leverage": 6}, {"max_leverage": 0},
    {"max_leverage": 3.5}, {"params_sha256": "xyz"}, {"code_sha256": "C" * 64}, {"freeze_sha256": ""},
    {"acceptance_report_sha256": None}, {"strategies": []}, {"strategies": ["nonsense"]}, {"candidate": "  "},
    {"authorised_at": "2026-11-06T00:00:00"}, {"valid_until": "soon"}, {"risk_limits": {"max_risk_pct": 0.01}},
    {"risk_limits": {**lg.risk_snapshot(P, DD), "max_risk_pct": -1}},
])
def test_a_malformed_or_non_passing_record_is_refused(world, change):
    root, key = world
    write(root, payload(root, **change), key)
    res = gate(root)
    assert not res.allowed and "G3 record" in failed(res)


def test_extra_or_missing_fields_are_refused(world):
    root, key = world
    write(root, {**payload(root), "extra": 1}, key)
    assert "G3 record" in failed(gate(root))
    rec = payload(root)
    del rec["freeze_sha256"]
    write(root, rec, key)
    assert "G3 record" in failed(gate(root))


@pytest.mark.parametrize("text", ["{", "[]", "null", '{"a": 1, "a": 2}', '{"a": NaN}', "x" * (fz.MAX_RECORD_BYTES + 1)])
def test_a_damaged_record_file_is_refused(world, text):
    root, _ = world
    (root / lg.LIVE_AUTH_DOC).write_text(text)
    assert not gate(root).allowed


def test_a_symlinked_record_is_refused(world, tmp_path):
    root, _ = world
    real = tmp_path / "elsewhere.json"
    real.write_text((root / lg.LIVE_AUTH_DOC).read_text())
    (root / lg.LIVE_AUTH_DOC).unlink()
    (root / lg.LIVE_AUTH_DOC).symlink_to(real)
    assert not gate(root).allowed


# ------------------------------------------------------------------------------------------------ G4 strategy, G5 code
def test_different_strategy_parameters_are_refused(world):
    root, _ = world
    for other in (replace(P, strategies=("breakout", "trend")), replace(P, stop_atr_mult=3.0), replace(P, timeframe="4h"),
                  replace(P, strategy_priority=("trend", "breakout"))):
        res = gate(root, params=other)
        assert not res.allowed and "G4 strategy" in failed(res), other


def test_different_code_is_refused(world):
    root, _ = world
    res = gate(root, code_identity=lambda: ("e" * 64, True))
    assert not res.allowed and failed(res) == ["G5 code"]


def test_uncommitted_code_is_refused_even_when_the_hash_matches(world):
    root, _ = world
    res = gate(root, code_identity=lambda: (CODE, False))
    assert not res.allowed and failed(res) == ["G5 code"]


def test_the_live_code_identity_covers_the_runner_and_the_gate_itself():
    from g_aegisbot.backtest import source_closure as sc
    c = sc.closure(mf.PACKAGE_DIR, lg.LIVE_ENTRY_POINTS, declared_external=lg.LIVE_EXTERNAL)
    for needed in ("engine/runner.py", "engine/livegate.py", "engine/strategy.py", "engine/strategies.py", "engine/trend.py",
                   "exchange/binance_futures.py", "backtest/signing.py", "services.py"):
        assert needed in c["files"], needed
    sha, committed = lg.live_code_identity()
    assert len(sha) == 64 and isinstance(committed, bool) and lg.live_code_identity() == (sha, committed)


# ------------------------------------------------------------------------------------------------ G6 risk limits
def stricter_record(root, key, params, **limits):
    """A record whose parameter hash is `params` but whose authorised risk limits are the PRODUCTION ones."""
    write(root, payload(root, params=params, risk_limits=lg.risk_snapshot(P, DD)), key)


@pytest.mark.parametrize("field,value", [
    ("max_risk_pct", 0.02), ("max_open_positions", 5), ("max_same_direction", 4), ("max_aggregate_risk_pct", 0.05),
    ("max_gross_notional_x", 3.0), ("max_same_direction", 0), ("max_aggregate_risk_pct", 0), ("max_gross_notional_x", 0),
])
def test_a_looser_risk_limit_than_authorised_is_refused(world, field, value):
    root, key = world
    loose = replace(P, **{field: value})
    stricter_record(root, key, loose)
    res = gate(root, params=loose)
    assert not res.allowed and "G6 risk" in failed(res) and field in " ".join(res.problems)


def test_a_looser_daily_drawdown_limit_is_refused(world):
    root, key = world
    res = gate(root, drawdown_limit=0.05)
    assert not res.allowed and "G6 risk" in failed(res)


@pytest.mark.parametrize("field,value", [("max_risk_pct", 0.01), ("max_open_positions", 3), ("max_same_direction", 2),
                                         ("max_aggregate_risk_pct", 0.03), ("max_gross_notional_x", 1.5)])
def test_stricter_limits_are_fine(world, field, value):
    root, key = world
    strict = replace(P, **{field: value})
    stricter_record(root, key, strict)
    assert gate(root, params=strict).allowed
    assert gate(root, params=strict, drawdown_limit=0.03).allowed


# ------------------------------------------------------------------------------------------------ G7 the account
def test_wallet_cap_leverage_and_keys_are_checked_per_account(world):
    root, _ = world
    assert gate(root, profile=profile(cap=Decimal("500"), leverage=3)).allowed                  # exactly at the authorised limits
    res = gate(root, profile=profile(cap=Decimal("500.01")))
    assert not res.allowed and "G7 account" in failed(res)
    assert "G7 account" in failed(gate(root, profile=profile(leverage=4)))
    assert "G7 account" in failed(gate(root, profile=profile(cap=Decimal("0"))))
    assert "G7 account" in failed(gate(root, profile=profile(cap=None)))


def test_keys_must_have_been_verified_recently(world):
    root, _ = world
    for stamp in (None, NOW - timedelta(days=8), NOW + timedelta(days=1)):
        res = gate(root, profile=profile(verified=stamp))
        assert not res.allowed and "G7 keys" in failed(res)


# ------------------------------------------------------------------------------------------------ the record is only a template for the owner
def test_the_builder_output_has_the_exact_shape_and_nothing_signs_it(world):
    root, key = world
    rec = payload(root)
    assert lg.record_shape_problems({**rec, "signature": {"algorithm": "ed25519", "key_id": "x", "value": "y"}}) == []
    assert "signature" not in rec and set(rec) | {"signature"} == lg.RECORD_FIELDS
    assert rec["params_sha256"] == mf.digest(P) and rec["strategies"] == ["breakout"]


# ------------------------------------------------------------------------------------------------ the runner, the CLI and the API use it
from tests.test_critical_safety import build, positions, prepared  # noqa: E402


def open_gate(root):
    return lambda **kw: real_evaluate(**{**kw, "lock_held": True, "repo_root": root, "env": ENV, "now": NOW,
                                         "code_identity": lambda: (CODE, True)})


async def test_the_runner_opens_no_live_entry_without_authorisation_and_says_why(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    with caplog.at_level("CRITICAL"):
        await r.run_cycle()
    assert positions(w["sf"]) == []
    assert "NEW LIVE ENTRIES BLOCKED BY THE LIVE GATE" in caplog.text


def authorised_root(tmp_path, runner, at):
    root, key = approved_repo(tmp_path, name="auth")
    write(root, payload(root, params=runner.params, dd=runner.limit, at=at, max_wallet_cap_usdt=20_000.0), key)
    return root


async def test_the_runner_opens_live_entries_only_when_the_whole_gate_passes(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    root = authorised_root(tmp_path, r, w["clock"].now)
    r.live_gate = lambda **kw: real_evaluate(**{**kw, "lock_held": True, "repo_root": root, "env": ENV,
                                                "code_identity": lambda: (CODE, True)})
    await r.run_cycle()
    assert len(positions(w["sf"])) >= 1                                         # the same setup that was refused above


async def test_each_missing_condition_stops_the_same_runner(tmp_path, monkeypatch):
    for name, tweak in {
        "switch off": dict(env={}),
        "lock not held": dict(lock_held=False),
        "different code": dict(code_identity=lambda: ("e" * 64, True)),
        "uncommitted code": dict(code_identity=lambda: (CODE, False)),
    }.items():
        sub = tmp_path / name.replace(" ", "_")
        sub.mkdir()
        w = build(sub, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
        r, _, adapter = await prepared(w)
        root = authorised_root(sub, r, w["clock"].now)
        base = {"lock_held": True, "repo_root": root, "env": ENV, "code_identity": lambda: (CODE, True)}
        r.live_gate = lambda _t=tweak, **kw: real_evaluate(**{**kw, **base, **_t})
        await r.run_cycle()
        assert positions(w["sf"]) == [], name


async def test_an_expired_authorisation_stops_new_entries(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT", "ETH/USDT:USDT"))
    r, _, adapter = await prepared(w)
    root = authorised_root(tmp_path, r, w["clock"].now - timedelta(days=40))
    r.live_gate = lambda **kw: real_evaluate(**{**kw, "lock_held": True, "repo_root": root, "env": ENV,
                                                "code_identity": lambda: (CODE, True)})
    await r.run_cycle()
    assert positions(w["sf"]) == []


async def test_the_runner_refuses_the_entry_itself_not_only_the_cycle(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, loaded, adapter = await prepared(w)
    sig = SimpleNamespace(symbol="BTC/USDT:USDT", side="buy")
    assert await r._enter(loaded, adapter, sig) is False and positions(w["sf"]) == []


async def test_an_error_inside_the_gate_is_a_refusal(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ("BTC/USDT:USDT",))
    r, loaded, _ = await prepared(w)
    r.live_gate = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    assert "failed safely" in r._live_gate_problem(loaded)


def test_the_gate_guards_new_live_entries_in_both_places_in_the_runner():
    import inspect
    from g_aegisbot.engine import runner
    for fn in (runner.TradingRunner._manage_user, runner.TradingRunner._enter):
        assert "self._live_gate_problem" in inspect.getsource(fn), fn.__name__


async def test_paper_profiles_are_not_subject_to_the_gate(tmp_path):
    from tests.test_runner import positions as paper_positions
    from tests.test_runner import world as _w  # noqa: F401
    # paper (testnet-mode) cycles are exercised with the REAL default wiring in tests/test_runner.py (gate stubbed open there by
    # conftest only for live profiles' sake); here: the runner code applies the gate to live profiles only
    import inspect
    from g_aegisbot.engine import runner
    assert inspect.getsource(runner.TradingRunner._enter).count('profile.trading_mode == "live" and (why := await asyncio.to_thread(self._live_gate_problem') == 1


# ---- the admin CLI and the API
from tests.test_admin import NOW as ADMIN_NOW  # noqa: E402
from tests.test_admin import do, env as admin_env, profile as admin_profile  # noqa: E402,F401
from tests.test_api import KEY, SEC, hdr  # noqa: E402
from tests.test_api import env as api_env  # noqa: E402,F401


def test_the_admin_cli_cannot_switch_an_account_to_live_without_authorisation(admin_env):
    env = admin_env
    do(env, "create-user", "alice", "--mode", "testnet")
    do(env, "set-keys", "alice", "--verify")
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 1 and "Cannot switch to live" in out and "G2 protocol" in out and "G3 record" in out
    assert admin_profile(env, "alice").trading_mode == "testnet"


def test_the_admin_cli_switches_to_live_only_with_a_valid_authorisation(admin_env, tmp_path, monkeypatch):
    env = admin_env
    root, key = approved_repo(tmp_path, name="auth")
    write(root, payload(root, at=ADMIN_NOW, max_wallet_cap_usdt=20_000.0), key)
    monkeypatch.setattr(lg, "evaluate", lambda **kw: real_evaluate(**{**kw, "repo_root": root, "code_identity": lambda: (CODE, True)}))
    do(env, "create-user", "alice", "--mode", "testnet")
    do(env, "set-keys", "alice", "--verify")
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 0 and admin_profile(env, "alice").trading_mode == "live", out


def test_the_api_cannot_switch_an_account_to_live_without_authorisation(api_env):
    env = api_env
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    c.put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr(email="bob@example.com", sub="uid-bob"))
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live", "confirm_real_money": True})
    assert r.status_code == 400 and "G2 protocol" in r.json()["detail"] and "G3 record" in r.json()["detail"]
    assert c.get("/api/me", headers=hdr(email="bob@example.com")).json()["trading_mode"] == "testnet"
