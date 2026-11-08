"""Protocol approval, freeze records, and acceptance runs through the command line (offline: the download step is replaced)."""
import json

import pandas as pd
import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest.__main__ import main
from g_aegisbot.backtest.data import PortfolioData, synthetic_ohlcv
from g_aegisbot.backtest.funding import FundingSchedule
from g_aegisbot.engine.strategy import StrategyParams
from tests.signing_helpers import approved_repo, new_key, sign_freeze

SYM = "BTC/USDT:USDT"
DECL = {"by": "user", "date": "2026-11-07", "statement": "the 2019-09-08 to 2021-11-05 period was not previously examined"}


# ================================================================ freeze record
@pytest.fixture(autouse=True)
def _closure_is_committed(monkeypatch):
    """The eligibility tests below are about OTHER conditions. Whether the working copy equals a commit is stubbed to True
    here and tested against real temporary git repositories in test_commit_match.py."""
    from g_aegisbot.backtest import manifest as _mf
    monkeypatch.setattr(_mf, "code_matches_commit", lambda *a, **k: {"matches": True, "checked": 0, "mismatched": []})


def good_record(**over):
    kw = dict(protocol_sha256="a" * 64, source_sha256="b" * 64,
              candidates={"breakout": {"strategies": ["breakout"], "params_sha256": "c" * 64},
                          "trend": {"strategies": ["trend"], "params_sha256": "d" * 64}},
              universe=[SYM, "ETH/USDT:USDT"], liquidation_file_sha256="e" * 64, declaration=DECL, diversification_metric="calmar")
    kw.update(over)
    return fz.build_freeze_record(**kw)


def good_run(**over):
    run = {"protocol_sha256": "a" * 64, "source_sha256": "b" * 64, "candidate": "breakout", "strategies": ["breakout"],
           "params_sha256": "c" * 64, "symbols": ["ETH/USDT:USDT", SYM], "cost_profile_values": dict(fz.BASE_COSTS),
           "liquidation_file_sha256": "e" * 64, "measured": {"start": "2025-01-01", "end": "2025-06-01"}}
    run.update(over)
    return run


def test_a_freeze_record_records_everything_that_is_frozen():
    r = good_record()
    for key in ("protocol_sha256", "source_sha256", "candidates", "universe", "periods", "methodology", "costs", "liquidation",
                "declaration", "diversification_metric", "freeze_sha256", "confirmation_period_names"):
        assert key in r
    assert r["periods"]["confirmation"] == ["2019-09-08", "2021-11-06"] and r["methodology"]["confidence"] == 0.983
    assert r["methodology"]["min_completed_trades"] == 300 and r["methodology"]["window_days"] == 90
    assert r["costs"]["base"]["taker_fee"] == 0.0005 and r["costs"]["stress"]["slippage_scale"] == 2.0
    assert r["declaration"]["statement"] == DECL["statement"]                       # kept verbatim
    assert fz.validate_freeze_record(r) == [] and fz.validate_freeze_record(r, good_run()) == []


def test_a_freeze_record_survives_a_round_trip_through_a_file(tmp_path):
    r = good_record()
    p = fz.write_freeze_record(tmp_path / "freeze.json", r)
    assert fz.validate_freeze_record(fz.load_freeze_record(p), good_run()) == []


def test_editing_a_freeze_record_after_freezing_is_detected():
    r = good_record()
    r["methodology"]["min_completed_trades"] = 100
    assert any("own hash does not match" in p for p in fz.validate_freeze_record(r))
    r2 = good_record()
    r2["universe"].append("SOL/USDT:USDT")
    assert any("own hash does not match" in p for p in fz.validate_freeze_record(r2))


@pytest.mark.parametrize("key", ["protocol_sha256", "source_sha256", "candidates", "universe", "periods", "methodology",
                                 "diversification_metric", "costs", "liquidation", "declaration", "freeze_sha256"])
def test_a_missing_field_is_reported(key):
    r = good_record()
    del r[key]
    assert any(f"no {key}" in p for p in fz.validate_freeze_record(r))


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_a_freeze_record_that_is_not_an_object_is_invalid(bad):
    assert fz.validate_freeze_record(bad) == ["the freeze record is not an object"]


def test_the_declaration_the_metric_and_the_bracket_hash_are_required():
    assert any("declaration" in p for p in fz.validate_freeze_record(good_record(declaration={"by": "user"})))
    assert any("declaration" in p for p in fz.validate_freeze_record(good_record(declaration={"by": " ", "date": "d", "statement": "s"})))
    assert any("diversification metric" in p for p in fz.validate_freeze_record(good_record(diversification_metric="profit")))
    assert any("liquidation-bracket" in p for p in fz.validate_freeze_record(good_record(liquidation_file_sha256=None)))


@pytest.mark.parametrize("change,why", [
    (dict(protocol_sha256="0" * 64), "protocol document differs"),
    (dict(source_sha256="0" * 64), "source code differs"),
    (dict(candidate="nope", strategies=["x"]), "not in the freeze record"),
    (dict(strategies=["breakout", "trend"]), "strategy set differs"),
    (dict(params_sha256="0" * 64), "parameters differ"),
    (dict(symbols=[SYM]), "universe differs"),
    (dict(symbols=[SYM, "ETH/USDT:USDT", "SOL/USDT:USDT"]), "universe differs"),
    (dict(cost_profile_values={**fz.BASE_COSTS, "slippage_scale": 1.5}), "neither the frozen base nor the frozen stress"),
    (dict(cost_profile_values={**fz.BASE_COSTS, "funding_mode": "approx"}), "neither the frozen base"),
    (dict(cost_profile_values={**fz.BASE_COSTS, "breaker_on": False}), "neither the frozen base"),
    (dict(liquidation_file_sha256="0" * 64), "brackets differ"),
    (dict(liquidation_file_sha256=None), "brackets differ"),
    (dict(measured={"start": "2026-10-01", "end": "2026-12-01"}), "not inside any frozen period"),
    (dict(measured={"start": "2021-10-01", "end": "2021-12-01"}), "not inside any frozen period"),
    (dict(measured={"start": "garbage", "end": "2021-12-01"}), "no readable measured period"),
    (dict(measured=None), "no readable measured period"),
])
def test_a_run_must_match_the_freeze_record(change, why):
    assert any(why in p for p in fz.validate_freeze_record(good_record(), good_run(**change)))


def test_the_frozen_stress_profile_and_each_frozen_period_are_accepted():
    r = good_record()
    assert fz.validate_freeze_record(r, good_run(cost_profile_values=dict(fz.STRESS_COSTS))) == []
    for a, b in fz.PERIODS.values():
        assert fz.validate_freeze_record(r, good_run(measured={"start": a, "end": b})) == []


def test_the_freeze_record_defines_the_confirmation_period_used_for_exposure():
    r = good_record()
    assert [n for n, _, _ in fz.confirmation_periods(r)] == ["confirmation"]
    assert fz.holdout_exposure("2020-01-01", "2020-02-01", r)["exposed"] is True
    moved = good_record(periods={"confirmation": ["2020-06-01", "2020-12-01"], "development": ["2021-01-01", "2022-01-01"],
                                 "validation": ["2022-01-01", "2023-01-01"]})
    assert fz.holdout_exposure("2020-01-01", "2020-02-01", moved)["exposed"] is False
    assert fz.holdout_exposure("2020-07-01", "2020-08-01", moved)["exposed"] is True


# ================================================================ acceptance through the command line
@pytest.fixture
def cli(monkeypatch, tmp_path):
    import g_aegisbot.backtest.__main__ as c
    state = {}

    def prepare(first="2024-11-01", symbols=(SYM,), days=210):
        frames = {s: synthetic_ohlcv(days * 24, i + 1, start=first) for i, s in enumerate(symbols)}
        reports = {s: {"candles": len(f), "duplicates": 0, "out_of_order": 0, "missing_candles": 0, "source": "cache"} for s, f in frames.items()}
        start_ms = int(pd.Timestamp("2024-09-01", tz="UTC").timestamp() * 1000)
        funding = {s: FundingSchedule(list(range(start_ms, start_ms + 400 * 86_400_000, 8 * 3_600_000)), [0.0001] * (400 * 3))
                   for s in symbols}

        async def fetch(a, symbols_, w, funding_start_ms, root):
            return PortfolioData(frames, reports, {}), (funding if a.funding == "historical" else None)
        monkeypatch.setattr(c, "_fetch_group", fetch)
        return frames

    brackets = tmp_path / "brackets.json"
    brackets.write_text(json.dumps([{"notional_cap": 100000, "mmr": 0.004, "cum": 0}, {"notional_cap": None, "mmr": 0.01, "cum": 300}]))
    root, key = approved_repo(tmp_path, name="repo")                 # throw-away key, pinned only in this temporary repository
    approval = fz.validate_protocol_approval(root)
    assert approval["approved"] is True
    real_sig = fz.freeze_signature_problems
    monkeypatch.setattr(fz, "validate_protocol_approval", lambda *a, **k: approval)
    monkeypatch.setattr(fz, "freeze_signature_problems", lambda record, repo_root=None, now=None: real_sig(record, root, now))
    state.update(prepare=prepare, brackets=brackets, approval=approval, tmp=tmp_path, monkeypatch=monkeypatch, key=key, root=root)
    return state


def make_freeze(cli, path, **over):
    params = StrategyParams(timeframe="1h", strategies=("breakout",))
    kw = dict(protocol_sha256=cli["approval"]["protocol_sha256"], source_sha256=mf.source_tree_sha256()["sha256"],
              candidates={"breakout": {"strategies": ["breakout"], "params_sha256": mf.digest(params)}}, universe=[SYM],
              liquidation_file_sha256=mf.sha256_file(cli["brackets"]), declaration=DECL, diversification_metric="sharpe")
    unsigned, signer = over.pop("unsigned", False), over.pop("signer", None)
    kw.update(over)
    record = fz.build_freeze_record(**kw)
    return fz.write_freeze_record(path, record if unsigned else sign_freeze(record, signer or cli["key"]))


ACC = ["--symbols", SYM, "--strategies", "breakout", "--start", "2025-01-01", "--end", "2025-03-01", "--funding", "historical"]


def acceptance_args(cli, freeze_path, *extra, out="out"):
    return [*ACC, "--acceptance", "--freeze-record", str(freeze_path), "--liq-brackets", str(cli["brackets"]),
            "--out", str(cli["tmp"] / out), *extra]


def test_a_fully_qualified_acceptance_run_writes_an_eligible_manifest(cli):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "freeze.json")
    main(acceptance_args(cli, freeze))
    path = next((cli["tmp"] / "out").rglob("manifest.json"))
    m = json.loads(path.read_text())
    e = m["eligibility"]
    assert e["acceptance_eligible"] is True and e["freeze_record_valid"] and e["protocol_approved"] and e["data_integrity_passed"], e
    assert m["liquidation"]["kind"] == "exact" and m["protocol"]["freeze_sha256"] == json.loads(freeze.read_text())["freeze_sha256"]
    assert m["holdout"]["exposed"] is False and mf.verify_outputs(path.parent) == []


def test_the_frozen_stress_profile_is_an_acceptance_run_too(cli):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "freeze.json")
    main(acceptance_args(cli, freeze, "--slippage-scale", "2.0", "--fee-scale", "1.2"))
    m = json.loads(next((cli["tmp"] / "out").rglob("manifest.json")).read_text())
    assert m["eligibility"]["acceptance_eligible"] is True and m["config"]["taker_fee"] == pytest.approx(0.0006)


def refused(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    return str(exc.value)


def test_acceptance_is_refused_before_any_work_without_the_required_inputs(cli):
    cli["prepare"]()
    base = [*ACC, "--acceptance", "--out", str(cli["tmp"] / "o")]
    assert "needs --freeze-record" in refused(base)
    assert "needs --liq-brackets" in refused([*base, "--freeze-record", "nope.json"])
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    ok = acceptance_args(cli, freeze)
    for flag, text in (("--legacy-risk", "--legacy-risk"), ("--no-breaker", "--no-breaker"), ("--allow-partial", "--allow-partial")):
        assert text in refused([*ok, flag])
    assert "synthetic data can never" in refused([*ok, "--synthetic"])
    approx = [a if a != "historical" else "approx" for a in ok]
    assert "needs --funding historical" in refused(approx)
    assert not list((cli["tmp"] / "out").rglob("manifest.json"))                      # nothing was run or written


def test_acceptance_is_refused_for_an_unreadable_or_edited_freeze_record(cli):
    cli["prepare"]()
    assert "cannot be read" in refused(acceptance_args(cli, cli["tmp"] / "missing.json"))
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    data = json.loads(freeze.read_text())
    data["methodology"]["min_completed_trades"] = 1
    freeze.write_text(json.dumps(data))
    assert "own hash does not match" in refused(acceptance_args(cli, freeze))


def test_acceptance_is_refused_while_the_protocol_is_a_draft(cli, monkeypatch):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    monkeypatch.setattr(fz, "validate_protocol_approval", lambda *a, **k: {"approved": False, "protocol_sha256": "x",
                                                                          "problems": ["there is no approval record: the protocol is still a draft"]})
    assert "still a draft" in refused(acceptance_args(cli, freeze))


def test_a_run_whose_costs_match_neither_frozen_profile_is_refused_before_it_runs(cli):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    msg = refused(acceptance_args(cli, freeze, "--slippage-scale", "1.5"))
    assert "cannot be an acceptance run" in msg and "neither the frozen base" in msg
    assert not list((cli["tmp"] / "out").rglob("manifest.json"))


def test_settings_that_are_not_frozen_do_not_invalidate_a_run(cli):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    main(acceptance_args(cli, freeze, "--capital", "5000"))             # the starting capital is not part of the freeze
    assert json.loads(next((cli["tmp"] / "out").rglob("manifest.json")).read_text())["eligibility"]["acceptance_eligible"] is True


def test_a_different_universe_parameters_or_brackets_are_refused(cli):
    cli["prepare"]()
    other_universe = make_freeze(cli, cli["tmp"] / "u.json", universe=[SYM, "ETH/USDT:USDT"])
    assert "universe differs" in refused(acceptance_args(cli, other_universe))
    wrong_params = make_freeze(cli, cli["tmp"] / "p.json",
                               candidates={"breakout": {"strategies": ["breakout"], "params_sha256": "0" * 64}})
    assert "parameters differ" in refused(acceptance_args(cli, wrong_params))
    wrong_brackets = make_freeze(cli, cli["tmp"] / "b.json", liquidation_file_sha256="0" * 64)
    assert "brackets differ" in refused(acceptance_args(cli, wrong_brackets))


def test_exploratory_runs_stay_possible_without_an_approved_protocol(cli, monkeypatch):
    cli["prepare"]()
    monkeypatch.setattr(fz, "validate_protocol_approval", lambda *a, **k: {"approved": False, "protocol_sha256": None, "problems": ["draft"]})
    main(["--symbols", SYM, "--strategies", "breakout", "--start", "2025-01-01", "--end", "2025-03-01", "--funding", "approx",
          "--out", str(cli["tmp"] / "x")])
    m = json.loads(next((cli["tmp"] / "x").rglob("manifest.json")).read_text())
    assert m["eligibility"]["acceptance_eligible"] is False and m["eligibility"]["protocol_approved"] is False
    assert m["eligibility"]["synthetic_data"] is False and m["eligibility"]["data_integrity_passed"] is True


def test_the_fee_scale_must_be_positive(cli):
    cli["prepare"]()
    for bad in ("0", "-1", "nan"):
        assert "--fee-scale must be a positive number" in refused([*ACC, "--fee-scale", bad, "--out", str(cli["tmp"] / "z")])


def test_an_unsigned_or_wrongly_signed_freeze_record_is_refused_before_any_work(cli):
    cli["prepare"]()
    unsigned = make_freeze(cli, cli["tmp"] / "u.json", unsigned=True)
    assert "no valid signature block" in refused(acceptance_args(cli, unsigned))
    stranger = make_freeze(cli, cli["tmp"] / "s.json", signer=new_key())
    assert "not trusted" in refused(acceptance_args(cli, stranger))
    assert not list((cli["tmp"] / "out").rglob("manifest.json"))


def test_an_edited_and_rehashed_freeze_record_fails_on_its_signature(cli):
    cli["prepare"]()
    freeze = make_freeze(cli, cli["tmp"] / "f.json")
    data = json.loads(freeze.read_text())
    data["methodology"]["min_completed_trades"] = 1
    data["freeze_sha256"] = mf.digest({k: v for k, v in data.items() if k not in ("freeze_sha256", "signature")})
    freeze.write_text(json.dumps(data))
    assert fz.validate_freeze_record(data) == []                               # the self-hash alone is satisfied...
    assert "does not match" in refused(acceptance_args(cli, freeze))           # ...the signature is not


@pytest.mark.parametrize("bad", [None, "x", 5, [], {"freeze_version": 1}])
def test_malformed_freeze_values_never_crash_the_validator(bad):
    rec = good_record()
    for key in ("candidates", "universe", "periods", "methodology", "costs", "liquidation", "declaration", "confirmation_period_names",
                "diversification_metric", "created_at", "freeze_version", "protocol_sha256", "freeze_sha256"):
        broken = {**rec, key: bad}
        problems = fz.validate_freeze_record(broken, good_run())
        assert problems and isinstance(problems, list), key
