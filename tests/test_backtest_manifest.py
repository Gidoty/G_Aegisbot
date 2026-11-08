"""The experiment manifest (backtest/manifest.py): complete, deterministic where it should be, strict about outputs, funding
and environment, with a defined source scope and a classified command line. Offline only. --synthetic runs say nothing about
any strategy."""
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest.__main__ import build_parser, main
from g_aegisbot.backtest.data import PortfolioData, synthetic_ohlcv
from g_aegisbot.backtest.funding import FundingSchedule
from g_aegisbot.backtest.liquidation import LiquidationModel
from g_aegisbot.backtest.simulator import BacktestConfig
from tests.signing_helpers import approved_repo, sign_freeze

SYMS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]


@pytest.fixture(autouse=True)
def _closure_is_committed(monkeypatch):
    """The eligibility tests below are about OTHER conditions. Whether the working copy equals a commit is stubbed to True
    here and tested against real temporary git repositories in test_commit_match.py."""
    from g_aegisbot.backtest import manifest as _mf
    monkeypatch.setattr(_mf, "code_matches_commit", lambda *a, **k: {"matches": True, "checked": 0, "mismatched": []})


def run(tmp_path, name, *extra, seed="1", syms=SYMS):
    out = tmp_path / name
    main(["--synthetic", "--years", "0.3", "--symbols", *syms, "--seed", seed, "--out", str(out), *extra])
    return out


def manifests(out: Path) -> dict[str, dict]:
    return {"/".join(p.parent.relative_to(out).parts[1:]): json.loads(p.read_text()) for p in sorted(out.rglob("manifest.json"))}


def top(out: Path) -> tuple[Path, dict]:
    path = next(p for p in sorted(out.rglob("manifest.json")) if p.parent.name == "breakout")
    return path.parent, json.loads(path.read_text())


def frames_for(symbol="BTC/USDT:USDT", start="2024-01-01", rows=3):
    idx = pd.date_range(start, periods=rows, freq="h", tz="UTC")
    return {symbol: pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)}


def mini(**kw):
    base = dict(label="t", run_name="t", strategies=("breakout",), cfg=BacktestConfig(), frames=frames_for(), funding=None)
    base.update(kw)
    return mf.build_manifest(**base)


# ================================================================ completeness
def test_every_saved_run_has_a_complete_manifest(tmp_path):
    out = run(tmp_path, "a", "--strategies", "breakout")
    _, m = top(out)
    for key in ("manifest_version", "experiment_id", "created_at", "eligibility", "code", "environment", "strategy", "config",
                "config_sha256", "assumptions", "data", "funding", "protocol", "holdout", "liquidation", "data_policy",
                "arguments", "command_line", "outputs", "synthetic"):
        assert key in m, key
    assert "claims_performance" not in m                                   # replaced by explicit eligibility fields
    e = m["eligibility"]
    assert e["synthetic_data"] is True and e["data_integrity_passed"] is False and e["acceptance_eligible"] is False
    assert {"protocol_approved", "freeze_record_valid", "conditions", "problems"} <= set(e)
    assert m["code"]["files"] > 15 and len(m["code"]["sha256"]) == 64 and "backtest/simulator.py" in m["code"]["modules"]
    assert m["code"]["entry_points"] == list(__import__("g_aegisbot.backtest.source_closure", fromlist=["x"]).ENTRY_POINTS)
    assert set(m["code"]["external_versions"]) == {"ccxt", "cryptography", "dotenv", "numpy", "pandas"}
    assert set(m["code"]["git"]) <= {"commit", "working_tree", "error"} and set(m["code"]["committed"]) >= {"matches", "checked", "mismatched"} and "dirty" not in m["code"]["git"]
    assert set(m["data"]) == set(SYMS) and all(len(d["sha256"]) == 64 for d in m["data"].values())
    assert m["strategy"]["params"]["stop_atr_mult"] == 2.0 and m["config"]["use_breaker"] is True
    assert m["holdout"]["measured"]["start"] and m["holdout"]["measured"]["end"]
    assert set(m["outputs"]) == set(mf.REQUIRED_OUTPUTS)
    assert m["arguments"]["capital"] == 10000.0 and m["arguments"]["synthetic"] is True
    for k in ("python_implementation", "python_version", "machine", "numpy", "numpy_build", "pandas", "ccxt"):
        assert k in m["environment"], k


def test_the_manifest_records_the_protocol_document_but_the_draft_is_not_approved(tmp_path):
    _, m = top(run(tmp_path, "p", "--strategies", "breakout"))
    assert m["protocol"]["sha256"] == mf.sha256_file(Path(mf.REPO_ROOT) / mf.PROTOCOL_DOC)
    assert m["protocol"]["approved"] is False and m["eligibility"]["protocol_approved"] is False


# ================================================================ reproducibility
def test_the_same_experiment_gives_the_same_id_and_the_same_hashes(tmp_path):
    _, m1 = top(run(tmp_path, "r1", "--strategies", "breakout"))
    _, m2 = top(run(tmp_path, "r2", "--strategies", "breakout"))
    assert mf.compare_manifests(m1, m2) == []
    assert m1["experiment_id"] == m2["experiment_id"] and m1["outputs"] == m2["outputs"]


def test_a_matching_experiment_id_alone_is_not_a_reproduction(tmp_path):
    _, m1 = top(run(tmp_path, "c1", "--strategies", "breakout"))
    tampered = copy.deepcopy(m1)
    tampered["outputs"]["trades.csv"] = "0" * 64
    tampered["data"]["BTC/USDT:USDT"]["sha256"] = "1" * 64
    tampered["funding"] = {"BTC/USDT:USDT": {"sha256": "2" * 64}}
    tampered["protocol"]["freeze_sha256"] = "3" * 64
    diffs = mf.compare_manifests(m1, tampered)
    assert tampered["experiment_id"] == m1["experiment_id"]
    assert {"output trades.csv differs", "data hashes differ", "funding hashes differ", "protocol.freeze_sha256 differs"} <= set(diffs)


@pytest.mark.parametrize("extra,seed", [(["--slippage-scale", "2.0"], "1"), (["--fee-scale", "1.2"], "1"), (["--no-breaker"], "1"),
                                        ([], "2"), (["--legacy-risk"], "1"), (["--capital", "5000"], "1"), (["--leverage", "2"], "1"),
                                        (["--years", "0.35"], "1"), (["--allow-late-start"], "1"), (["--allow-partial"], "1")])
def test_a_change_to_any_result_affecting_argument_changes_the_id(tmp_path, extra, seed):
    _, base = top(run(tmp_path, "base", "--strategies", "breakout"))
    _, other = top(run(tmp_path, "other", "--strategies", "breakout", *extra, seed=seed))
    assert other["experiment_id"] != base["experiment_id"]


def test_the_symbol_set_and_the_strategy_set_change_the_id(tmp_path):
    _, base = top(run(tmp_path, "s0", "--strategies", "breakout"))
    _, fewer = top(run(tmp_path, "s1", "--strategies", "breakout", syms=SYMS[:1]))
    trend = json.loads(next(p for p in run(tmp_path, "s2", "--strategies", "trend").rglob("manifest.json") if p.parent.name == "trend").read_text())
    assert len({base["experiment_id"], fewer["experiment_id"], trend["experiment_id"]}) == 3


@pytest.mark.parametrize("extra", [["--cache-dir", "elsewhere"], ["--retries", "9"], ["--refresh"]])
def test_arguments_that_cannot_change_a_result_do_not_change_the_id(tmp_path, extra):
    _, base = top(run(tmp_path, "n0", "--strategies", "breakout"))
    _, other = top(run(tmp_path, "n1", "--strategies", "breakout", *extra))
    assert other["experiment_id"] == base["experiment_id"]


def test_compare_mode_writes_one_manifest_per_strategy_set(tmp_path):
    out = run(tmp_path, "c", "--compare")
    assert {"breakout", "trend", "breakout+trend"} <= {p.parent.name for p in out.rglob("manifest.json")}
    assert len({json.loads(p.read_text())["experiment_id"] for p in out.rglob("manifest.json")}) == 3


# ================================================================ command-line audit
def test_every_argument_of_the_parser_is_classified():
    dests = {a.dest for a in build_parser()._actions if a.dest != "help"}
    assert dests == set(mf.ARG_CLASSIFICATION), (dests ^ set(mf.ARG_CLASSIFICATION))
    for dest, (kind, reason) in mf.ARG_CLASSIFICATION.items():
        assert kind in ("identity", "excluded") and len(reason) > 15, dest
    excluded = {d for d, (k, _) in mf.ARG_CLASSIFICATION.items() if k == "excluded"}
    assert excluded == {"compare", "split", "windows", "cache_dir", "out", "refresh", "retries"}


# ================================================================ holdout exposure
def test_exposure_is_decided_from_timestamps_with_end_exclusive():
    e = lambda a, b: fz.holdout_exposure(a, b)["exposed"]
    assert not e("2021-11-06", "2022-01-01") and not e("2019-01-01", "2019-09-08")             # touching boundaries: no overlap
    assert e("2019-09-08", "2019-09-09") and e("2021-11-05", "2021-11-06") and e("2018-01-01", "2023-01-01")
    o = fz.holdout_exposure("2021-10-01", "2021-12-01")
    assert o["exposed"] and o["overlaps"][0]["days"] == 36 and o["overlaps"][0]["period"] == "confirmation"
    assert fz.holdout_exposure("2022-03-01", "2022-04-01", data_first="2021-11-01", data_last="2022-04-01")["data_overlap"] is True
    assert fz.holdout_exposure("2023-03-01", "2023-04-01", data_first="2023-01-01", data_last="2023-04-01")["data_overlap"] is False


@pytest.fixture
def fake_real_data(monkeypatch):
    """Offline stand-in for the download step: synthetic candles dated as the test asks, reported as clean real-data reports."""
    import g_aegisbot.backtest.__main__ as cli

    def install(first_candle: str, symbols=("BTC/USDT:USDT",), days=420):
        frames = {s: synthetic_ohlcv(days * 24, i + 1, start=first_candle) for i, s in enumerate(symbols)}
        reports = {s: {"candles": len(f), "first": str(f.index[0]), "last": str(f.index[-1]), "duplicates": 0, "out_of_order": 0,
                       "missing_candles": 0, "late_start_candles": 0, "source": "cache"} for s, f in frames.items()}

        async def fetch(a, symbols_, w, funding_start_ms, root):
            return PortfolioData(frames, reports, {}), None
        monkeypatch.setattr(cli, "_fetch_group", fetch)
    return install


def real_run(tmp_path, name, *args):
    out = tmp_path / name
    main(["--symbols", "BTC/USDT:USDT", "--funding", "approx", "--strategies", "breakout", "--out", str(out), *args])
    return out


@pytest.mark.parametrize("start,end,exposed", [
    ("2020-03-01", "2020-05-01", True),        # wholly inside the confirmation period
    ("2021-10-01", "2021-12-15", True),        # straddles its end
    ("2019-08-01", "2019-10-15", True),        # straddles its start (explicit --start/--end)
    ("2022-02-01", "2022-04-01", False),       # development period
    ("2025-02-01", "2025-04-01", False),       # validation period
])
def test_explicit_start_and_end_report_exposure_correctly(tmp_path, fake_real_data, start, end, exposed):
    first = (pd.Timestamp(start) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    fake_real_data(first)
    out = real_run(tmp_path, "x", "--start", start, "--end", end)
    _, m = top(out)
    assert m["holdout"]["exposed"] is exposed
    assert m["holdout"]["measured"]["start"].startswith(start) and m["holdout"]["measured"]["end"][:7] == end[:7]
    assert bool(m["holdout"]["overlaps"]) is exposed


def test_the_warm_up_overlap_is_reported_separately_from_exposure(tmp_path, fake_real_data):
    fake_real_data("2021-10-01")                                           # warm-up candles lie in the confirmation period
    _, m = top(real_run(tmp_path, "w", "--start", "2021-12-01", "--end", "2022-02-01"))
    assert m["holdout"]["exposed"] is False and m["holdout"]["data_overlap"] is True


def test_segments_are_judged_by_their_own_dates_not_their_names(tmp_path, fake_real_data):
    fake_real_data("2019-10-01", days=500)                                 # a run entirely inside the confirmation period
    out = real_run(tmp_path, "s", "--start", "2019-11-15", "--end", "2020-09-01", "--split", "0.5,0.25,0.25")
    ms = manifests(out)
    segs = {k.split("/")[-1]: v for k, v in ms.items() if "/" in k and k.count("/") == 1 or k.count("/") == 2}
    assert segs and all(v["holdout"]["exposed"] for k, v in ms.items())
    dev = next(v for k, v in ms.items() if k.endswith("/development"))
    assert dev["protocol"]["segment"]["name"] == "development" and dev["holdout"]["exposed"] is True    # "development" yet exposed


def test_a_segment_named_out_of_sample_outside_the_confirmation_period_is_not_an_exposure(tmp_path, fake_real_data):
    fake_real_data("2024-01-01", days=700)
    out = real_run(tmp_path, "o", "--start", "2024-03-01", "--end", "2025-09-01", "--split", "0.5,0.25,0.25")
    ms = manifests(out)
    oos = next(v for k, v in ms.items() if k.endswith("/out-of-sample"))
    assert oos["protocol"]["segment"]["name"] == "out-of-sample" and oos["holdout"]["exposed"] is False


# ================================================================ required outputs
def make_dir(tmp_path, names=mf.REQUIRED_OUTPUTS):
    d = tmp_path / "run"
    d.mkdir()
    for n in names:
        (d / n).write_text(f"content of {n}")
    return d


def test_manifest_creation_fails_when_any_required_output_is_missing(tmp_path):
    for missing in mf.REQUIRED_OUTPUTS:
        d = tmp_path / missing
        d.mkdir()
        for n in mf.REQUIRED_OUTPUTS:
            if n != missing:
                (d / n).write_text("x")
        with pytest.raises(mf.ManifestIntegrityError, match=missing):
            mf.attach_outputs({}, d)


def test_manifest_creation_rejects_an_unexpected_file_but_allows_segment_subdirectories(tmp_path):
    d = make_dir(tmp_path)
    (d / "development").mkdir()
    (d / "development" / "trades.csv").write_text("segment output")
    assert sorted(mf.attach_outputs({}, d)["outputs"]) == sorted(mf.REQUIRED_OUTPUTS)
    (d / "notes.txt").write_text("stray")
    with pytest.raises(mf.ManifestIntegrityError, match="notes.txt"):
        mf.attach_outputs({}, d)


def test_a_manifest_must_list_exactly_the_three_required_outputs(tmp_path):
    d = make_dir(tmp_path)
    with pytest.raises(mf.ManifestIntegrityError):
        mf.write_manifest(d, {"outputs": {"trades.csv": "x"}})
    with pytest.raises(mf.ManifestIntegrityError):
        mf.write_manifest(d, {"outputs": {**dict.fromkeys(mf.REQUIRED_OUTPUTS, "x"), "extra.csv": "y"}})


def test_verification_catches_every_kind_of_output_problem(tmp_path):
    d, _ = top(run(tmp_path, "v", "--strategies", "breakout"))
    assert mf.verify_outputs(d) == []
    original = json.loads((d / "manifest.json").read_text())

    def reset():
        (d / "manifest.json").write_text(json.dumps(original))

    (d / "trades.csv").write_text((d / "trades.csv").read_text() + "\n")
    assert any("trades.csv has changed" in p for p in mf.verify_outputs(d))
    (d / "equity.csv").unlink()
    assert any("equity.csv is missing from the directory" in p for p in mf.verify_outputs(d))
    broken = copy.deepcopy(original)
    del broken["outputs"]["summary.json"]
    (d / "manifest.json").write_text(json.dumps(broken))
    assert any("summary.json is not listed in the manifest" in p for p in mf.verify_outputs(d))
    odd = copy.deepcopy(original)
    odd["outputs"]["surprise.txt"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(odd))
    assert any("unexpected output surprise.txt" in p for p in mf.verify_outputs(d))
    reset()
    (d / "stray.log").write_text("x")
    assert any("unexpected file stray.log" in p for p in mf.verify_outputs(d))
    (d / "manifest.json").write_text("{not json")
    assert any("cannot be read" in p for p in mf.verify_outputs(d))


# ================================================================ environment identity
ENV = {"python_implementation": "CPython", "python_version": "3.13.1", "python_compiler": "GCC 13", "system": "Linux",
       "machine": "x86_64", "byteorder": "little", "platform": "Linux-6.1-x86_64", "numpy": "2.5.4",
       "numpy_build": {"blas": {"name": "openblas", "version": "0.3.30"}}, "pandas": "3.1.1", "ccxt": "4.5.92"}


@pytest.mark.parametrize("key,value", [("python_implementation", "PyPy"), ("python_version", "3.13.2"), ("python_compiler", "Clang 20"),
                                       ("system", "Darwin"), ("machine", "aarch64"), ("byteorder", "big"), ("numpy", "2.5.5"),
                                       ("numpy_build", {"blas": {"name": "mkl", "version": "2025"}}), ("pandas", "3.1.2"),
                                       ("ccxt", "4.5.93")])
def test_a_result_affecting_environment_component_changes_the_id(key, value):
    base = mini(env=dict(ENV))["experiment_id"]
    assert mini(env={**ENV, key: value})["experiment_id"] != base


def test_the_kernel_release_text_does_not_change_the_id_but_is_recorded():
    a, b = mini(env=dict(ENV)), mini(env={**ENV, "platform": "Linux-7.0-x86_64"})
    assert a["experiment_id"] == b["experiment_id"] and b["environment"]["platform"] == "Linux-7.0-x86_64"


def test_the_real_environment_reports_numeric_build_information():
    env = mf.environment()
    assert env["python_implementation"] == "CPython" and env["numpy_build"] and set(mf.IDENTITY_ENVIRONMENT_KEYS) <= set(env)


# ================================================================ funding validation
def sched(ts, rate):
    return FundingSchedule(ts, rate)


def test_valid_funding_including_numpy_arrays_is_inventoried_with_the_same_hash():
    a = mf.funding_inventory({"X": sched([1000, 2000, 3000], [0.0001, -0.0002, 0.0003])})["X"]
    b = mf.funding_inventory({"X": sched(np.array([1000, 2000, 3000]), np.array([0.0001, -0.0002, 0.0003]))})["X"]
    c = mf.funding_inventory({"X": sched(np.array([1000.0, 2000.0, 3000.0]), np.array([0.0001, -0.0002, 0.0003]))})["X"]
    assert a == b == c and a["events"] == 3 and a["first_ms"] == 1000 and a["last_ms"] == 3000
    assert mf.funding_inventory(None) is None


@pytest.mark.parametrize("ts,rate,why", [
    ([1, 2, 3], [0.1, 0.2], "3 timestamps but 2 rates"),
    ([1, 2], [0.1, 0.2, 0.3], "2 timestamps but 3 rates"),
    ([], [], "no events"),
    ([1, 2], [0.1, float("nan")], "non-finite"),
    ([1, 2], [0.1, float("inf")], "non-finite"),
    ([1.0, float("nan")], [0.1, 0.2], "non-finite"),
    ([1.0, float("inf")], [0.1, 0.2], "non-finite"),
    ([1, 2, 2], [0.1, 0.2, 0.3], "strictly increasing"),
    ([3, 2, 1], [0.1, 0.2, 0.3], "strictly increasing"),
    ([1.5, 2.5], [0.1, 0.2], "fractional"),
    (np.array([[1, 2], [3, 4]]), np.array([[0.1, 0.2], [0.3, 0.4]]), "one-dimensional"),
    (["a", "b"], [0.1, 0.2], "non-numeric"),
    ([True, False], [0.1, 0.2], "non-numeric"),
    ([1, 2], ["x", "y"], "non-numeric"),
    (np.array([1, 2, 3]), np.array([0.1, 0.2]), "3 timestamps but 2 rates"),
])
def test_malformed_funding_is_an_integrity_failure_never_truncated(ts, rate, why):
    with pytest.raises(mf.ManifestIntegrityError, match=why):
        mf.funding_inventory({"X": sched(ts, rate)})


def test_a_malformed_schedule_stops_manifest_creation():
    with pytest.raises(mf.ManifestIntegrityError):
        mini(funding={"BTC/USDT:USDT": sched([1, 2, 3], [0.1])})


# ================================================================ eligibility
GOOD_INTEGRITY = {"BTC/USDT:USDT": {"duplicates": 0, "out_of_order": 0, "missing_candles": 0}}
SYM = "BTC/USDT:USDT"


def exact_liq():
    return LiquidationModel(source="brackets from brackets.json")


def acceptance_setup(tmp_path, name="repo", **over):
    """A temporary repo with a pinned throw-away key, a validly SIGNED approval and a SIGNED freeze record for one run;
    returns kwargs for build_manifest."""
    root, key = approved_repo(tmp_path, name=name)
    cfg = BacktestConfig(funding_mode="historical", liquidation=exact_liq())
    frames = frames_for(SYM, "2025-01-01", rows=5)
    sch = FundingSchedule([1000, 2000], [0.0001, 0.0002])
    code = mf.source_tree_sha256()
    freeze = sign_freeze(fz.build_freeze_record(
        protocol_sha256=mf.sha256_file(root / mf.PROTOCOL_DOC), source_sha256=code["sha256"],
        candidates={"breakout": {"strategies": ["breakout"], "params_sha256": mf.digest(cfg.params)}}, universe=[SYM],
        liquidation_file_sha256="f" * 64, diversification_metric="sharpe",
        declaration={"by": "user", "date": "2026-11-07", "statement": "period not previously examined"}), key)
    kw = dict(label="t", run_name="t", strategies=("breakout",), cfg=cfg, frames=frames, funding={SYM: sch}, repo_root=root,
              data_integrity=GOOD_INTEGRITY, acceptance=True, freeze_record=freeze, liquidation_file_sha256="f" * 64,
              measured={"start": "2025-01-01", "end": "2025-03-01"})
    kw.update(over)
    return kw


def test_a_fully_qualified_run_is_acceptance_eligible(tmp_path):
    kw = acceptance_setup(tmp_path)
    m = mf.build_manifest(**kw)
    e = m["eligibility"]
    assert e["acceptance_eligible"] is True and e["problems"] == [], e
    assert e["synthetic_data"] is False and e["data_integrity_passed"] and e["protocol_approved"] and e["freeze_record_valid"]
    assert m["protocol"]["freeze_sha256"] == kw["freeze_record"]["freeze_sha256"] and m["liquidation"]["kind"] == "exact"


def test_real_data_alone_is_not_an_acceptance_run(tmp_path):
    kw = acceptance_setup(tmp_path, acceptance=False, freeze_record=None)
    e = mf.build_manifest(**kw)["eligibility"]
    assert e["synthetic_data"] is False and e["data_integrity_passed"] is True and e["protocol_approved"] is True
    assert e["freeze_record_valid"] is False and e["acceptance_eligible"] is False
    only_data = mf.build_manifest(**acceptance_setup(tmp_path, name="repo2", acceptance=False))
    assert only_data["eligibility"]["acceptance_eligible"] is False                   # even with a valid freeze: not requested


@pytest.mark.parametrize("name,change", [
    ("synthetic", dict(synthetic=True)),
    ("integrity: a missing candle", dict(data_integrity={SYM: {"duplicates": 0, "out_of_order": 0, "missing_candles": 1}})),
    ("integrity: a duplicate", dict(data_integrity={SYM: {"duplicates": 1, "out_of_order": 0, "missing_candles": 0}})),
    ("integrity: no report", dict(data_integrity={})),
    ("integrity: an excluded symbol", dict(excluded_symbols={"ETH/USDT:USDT": "failed to download"})),
    ("no freeze record", dict(freeze_record=None)),
    ("measured outside the frozen periods", dict(measured={"start": "2026-10-01", "end": "2026-12-01"})),
    ("measured period straddles two frozen periods", dict(measured={"start": "2021-10-01", "end": "2021-12-01"})),
    ("liquidation file differs", dict(liquidation_file_sha256="e" * 64)),
    ("acceptance not requested", dict(acceptance=False)),
])
def test_every_missing_condition_makes_the_run_ineligible(tmp_path, name, change):
    assert mf.build_manifest(**acceptance_setup(tmp_path, **change))["eligibility"]["acceptance_eligible"] is False, name


def test_ineligibility_from_configuration_and_protocol(tmp_path):
    from dataclasses import replace

    good = acceptance_setup(tmp_path)
    cfg = good["cfg"]
    cases = {
        "approximate funding": replace(cfg, funding_mode="approx"),
        "flat liquidation": replace(cfg, liquidation=LiquidationModel()),
        "breaker off": replace(cfg, use_breaker=False),
        "funding gaps allowed": replace(cfg, funding_allow_gaps=True),
        "legacy risk": replace(cfg, params=replace(cfg.params, max_aggregate_risk_pct=0.0)),
        "slippage scale 1.5": replace(cfg, slippage_scale=1.5),
        "changed parameters": replace(cfg, params=replace(cfg.params, stop_atr_mult=2.5)),
    }
    for name, c in cases.items():
        assert mf.build_manifest(**{**good, "cfg": c})["eligibility"]["acceptance_eligible"] is False, name
    stress = replace(cfg, slippage_scale=2.0, taker_fee=0.0005 * 1.2)                  # the frozen stress profile is allowed
    assert mf.build_manifest(**{**good, "cfg": stress})["eligibility"]["acceptance_eligible"] is True
    doc = Path(good["repo_root"]) / mf.PROTOCOL_DOC
    doc.write_text("edited after approval")
    e = mf.build_manifest(**good)["eligibility"]
    assert e["protocol_approved"] is False and e["acceptance_eligible"] is False
    doc.unlink()
    assert mf.build_manifest(**good)["eligibility"]["protocol_approved"] is False


def test_funding_is_required_for_eligibility(tmp_path):
    good = acceptance_setup(tmp_path)
    assert mf.build_manifest(**{**good, "funding": None})["eligibility"]["acceptance_eligible"] is False


# ================================================================ helpers
def test_the_protocol_hash_and_the_freeze_hash_are_part_of_the_experiment_identity(tmp_path):
    good = acceptance_setup(tmp_path)
    base = mf.build_manifest(**good)["experiment_id"]
    other_freeze = copy.deepcopy(good["freeze_record"])
    other_freeze["declaration"]["statement"] = "a different statement"        # edited AND rehashed: still a different identity
    other_freeze["freeze_sha256"] = fz.digest({k: v for k, v in other_freeze.items() if k not in ("freeze_sha256", "signature")})
    assert mf.build_manifest(**{**good, "freeze_record": other_freeze})["experiment_id"] != base
    (Path(good["repo_root"]) / mf.PROTOCOL_DOC).write_text("a different protocol")
    assert mf.build_manifest(**good)["experiment_id"] != base


def test_canonical_json_is_stable_and_handles_infinity_and_numpy_scalars():
    a = {"b": [1, (2, 3)], "a": float("inf"), "c": {"y": np.float64(1.5), "x": float("nan")}, "d": np.int64(3)}
    assert mf.canonical(a) == mf.canonical(dict(reversed(list(a.items()))))
    assert json.loads(mf.canonical(a))["a"] == "inf" and json.loads(mf.canonical(a))["d"] == 3


def test_the_acceptance_protocol_defines_every_criterion_and_the_required_rules():
    text = (Path(mf.REPO_ROOT) / mf.PROTOCOL_DOC).read_text().lower()
    for needle in [f"| c{i} |" for i in range(1, 14)] + [
            "draft v0.2", "declaration", "has not been independently verified", "2019-09-08", "2021-11-06", "forward paper trading is operational",
            "one trade is one completed position lifecycle", "partial entries", "truncated", "98.3%", "alpha 0.017",
            "equity-path reconstruction", "variant register", "block-length", "paired block bootstrap", "inconclusive",
            "never a pass", "not replaced", "unclassified", "actual binance leverage-bracket", "burns", "acceptance_eligible",
            "freeze_record_valid", "compare_manifests", "incomplete window", "ceil(0.60", "exploratory and confirmatory"]:
        assert needle in text, needle
