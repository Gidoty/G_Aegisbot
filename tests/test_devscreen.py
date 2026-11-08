"""The development screen applies the pre-declared promotion rules mechanically. Runs here are tiny synthetic result folders;
what is under test is the rule arithmetic, its boundaries and the refusals, not any strategy."""
import json

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest import devscreen as ds
from g_aegisbot.backtest.manifest import digest
from g_aegisbot.backtest.simulator import BacktestConfig
from g_aegisbot.costs import TAKER_FEE
from g_aegisbot.engine.strategy import StrategyParams

START, END = "2021-11-06 00:00:00+00:00", "2024-05-07 00:00:00+00:00"


def make_run(tmp_path, name="run", *, candidate="S-B4", n_wins=100, win=78.0, n_losses=500, loss=-12.0,
             wins_total=None, losses_total=None, slippage=1.0, fee=TAKER_FEE, period=(START, END), peak_fall=0.10, **manifest_over):
    """n_wins + n_losses completed trades whose net PnL sums to wins_total / losses_total (explicit totals win over per-trade)."""
    strategies, tf = ds.CANDIDATES[candidate]
    wins_total = n_wins * win if wins_total is None else wins_total
    losses_total = n_losses * loss if losses_total is None else losses_total
    pnl = [wins_total / n_wins] * n_wins + [losses_total / n_losses] * n_losses
    t0 = pd.Timestamp("2021-12-01", tz="UTC")
    rows = [{"symbol": "BTC/USDT:USDT", "side": "buy", "strategy": strategies[0], "entry_time": t0 + pd.Timedelta(hours=4 * i),
             "exit_time": t0 + pd.Timedelta(hours=4 * i + 3), "pnl": p, "gross_pnl": p, "fees": 0.0, "funding": 0.0,
             "reason": "trailing-stop", "initial_stop": 1.0, "amount": 1.0, "entry_price": 1.0} for i, p in enumerate(pnl)]
    d = tmp_path / name
    d.mkdir()
    pd.DataFrame(rows).to_csv(d / "trades.csv", index=False)
    days = pd.date_range("2021-11-06", "2024-05-06", freq="D", tz="UTC")
    eq = np.full(len(days), 10_000.0)
    eq[len(days) // 3:] = 10_000.0 * (1 + 0.5)                            # up 50%
    eq[len(days) // 2:] = 10_000.0 * (1 + 0.5) * (1 - peak_fall)            # then a fall of peak_fall
    pd.Series(eq, index=days, name="equity").to_csv(d / "equity.csv", index_label="timestamp")
    summary = {"metrics": {"start_equity": 10_000.0}, "notes": {"measurement_period": {"start": period[0], "end": period[1]}}}
    (d / "summary.json").write_text(json.dumps(summary))
    manifest = {"synthetic": False, "experiment_id": "e1", "strategy": {"enabled": list(strategies), "params_sha256": ds.registered_params_sha256(candidate)},
                "config": {"funding_mode": "historical", "slippage_scale": slippage, "taker_fee": fee, "use_breaker": True},
                "eligibility": {"data_integrity_passed": True},
                "code": {"committed": {"matches": True}, "git": {"commit": "abc123"}, "sha256": "f" * 64}}
    manifest.update(manifest_over)
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def rules(res):
    return {r["rule"].split()[0]: r for r in res["rules"]}


# ------------------------------------------------------------------------------------------------ the rules and their edges
def test_a_run_that_meets_every_development_rule_needs_the_stress_run(tmp_path):
    res = ds.screen(make_run(tmp_path, wins_total=7800.0, losses_total=-6000.0), "S-B4")        # PF 1.30, 600 trades, positive
    assert all(r["pass"] for r in res["rules"]) and res["verdict"] == "STRESS_RUN_NEEDED"


def test_p1_boundary_599_fails_600_passes(tmp_path):
    ok = ds.screen(make_run(tmp_path, "a", n_wins=100, n_losses=500, wins_total=9000.0, losses_total=-5000.0), "S-B4")
    short = ds.screen(make_run(tmp_path, "b", n_wins=100, n_losses=499, wins_total=9000.0, losses_total=-5000.0), "S-B4")
    assert rules(ok)["P1"]["pass"] is True
    assert rules(short)["P1"]["pass"] is False and short["verdict"] == "INFEASIBLE_UNDER_C1"


def test_p2_zero_expectancy_is_not_positive(tmp_path):
    res = ds.screen(make_run(tmp_path, wins_total=6000.0, losses_total=-6000.0), "S-B4")
    assert rules(res)["P2"]["pass"] is False and res["verdict"] == "REJECT"


@pytest.mark.parametrize("wins,ok", [(7800.0, True), (7799.0, False)])                   # losses 6000: PF 1.30 passes, 1.2998 fails
def test_p3_profit_factor_boundary(tmp_path, wins, ok):
    res = ds.screen(make_run(tmp_path, wins_total=wins, losses_total=-6000.0), "S-B4")
    assert rules(res)["P3"]["pass"] is ok


@pytest.mark.parametrize("fall,ok", [(0.25, True), (0.26, False)])
def test_p4_drawdown_boundary_on_daily_equity(tmp_path, fall, ok):
    res = ds.screen(make_run(tmp_path, wins_total=9000.0, losses_total=-6000.0, peak_fall=fall), "S-B4")
    assert rules(res)["P4"]["pass"] is ok and rules(res)["P4"]["value"] == pytest.approx(fall, abs=1e-9)


def test_truncated_end_of_data_trades_are_not_counted_as_completed(tmp_path):
    d = make_run(tmp_path, wins_total=9000.0, losses_total=-6000.0)
    t = pd.read_csv(d / "trades.csv")
    t.loc[:9, "reason"] = "end-of-data"
    t.to_csv(d / "trades.csv", index=False)
    st = ds.screen(d, "S-B4")["statistics"]
    assert st["completed_trades"] == 590 and st["truncated_trades"] == 10


# ------------------------------------------------------------------------------------------------ stress rule P5
def good(tmp_path, name="base", **kw):
    return make_run(tmp_path, name, wins_total=9000.0, losses_total=-6000.0, **kw)


def stress(tmp_path, wins, losses, name="stress"):
    return make_run(tmp_path, name, wins_total=wins, losses_total=losses, slippage=2.0, fee=TAKER_FEE * 1.2)


def test_p5_promotes_only_when_the_stress_run_stays_profitable(tmp_path):
    res = ds.screen(good(tmp_path), "S-B4", stress(tmp_path, 6600.0, -6000.0))
    assert res["verdict"] == "PROMOTE_TO_VALIDATION" and rules(res)["P5"]["pass"] is True


@pytest.mark.parametrize("wins,losses", [(6000.0, -6000.0), (5000.0, -6000.0)])           # profit factor 1.0 or below
def test_p5_rejects_a_stress_run_that_is_not_above_1(tmp_path, wins, losses):
    res = ds.screen(good(tmp_path), "S-B4", stress(tmp_path, wins, losses))
    assert res["verdict"] == "REJECT" and rules(res)["P5"]["pass"] is False


def test_a_failed_base_rule_never_reaches_the_stress_run(tmp_path):
    res = ds.screen(make_run(tmp_path, wins_total=6000.0, losses_total=-6000.0), "S-B4", tmp_path / "does_not_exist")
    assert res["verdict"] == "REJECT"


# ------------------------------------------------------------------------------------------------ refusals
def refused(tmp_path, match, candidate="S-B4", **kw):
    d = good(tmp_path, name=f"run{len(list(tmp_path.iterdir()))}", **kw)
    with pytest.raises(ds.ScreenError, match=match):
        ds.screen(d, candidate)


def test_a_run_outside_the_development_period_is_refused(tmp_path):
    refused(tmp_path, "development period", period=("2024-05-07 00:00:00+00:00", "2026-11-06 00:00:00+00:00"))
    refused(tmp_path, "development period", period=("2019-09-08 00:00:00+00:00", "2021-11-06 00:00:00+00:00"))   # confirmation


def test_a_run_whose_parameters_differ_from_the_registered_candidate_is_refused(tmp_path):
    other = digest(StrategyParams(timeframe="4h", strategies=("breakout",), stop_atr_mult=3.0))
    refused(tmp_path, "parameter hash", strategy={"enabled": ["breakout"], "params_sha256": other})


def test_the_wrong_strategy_or_timeframe_is_refused(tmp_path):
    refused(tmp_path, "enabled", strategy={"enabled": ["trend"], "params_sha256": ds.registered_params_sha256("S-B4")})
    refused(tmp_path, "parameter hash", strategy={"enabled": ["breakout"], "params_sha256": ds.registered_params_sha256("S-B1D")})


@pytest.mark.parametrize("over,match", [
    (dict(synthetic=True), "synthetic"),
    (dict(eligibility={"data_integrity_passed": False}), "integrity"),
    (dict(code={"committed": {"matches": False}, "git": {"commit": "x"}, "sha256": "y"}), "committed"),
    (dict(config={"funding_mode": "approx", "slippage_scale": 1.0, "taker_fee": TAKER_FEE, "use_breaker": True}), "historical funding"),
    (dict(config={"funding_mode": "historical", "slippage_scale": 1.0, "taker_fee": TAKER_FEE, "use_breaker": False}), "breaker"),
    (dict(config={"funding_mode": "historical", "slippage_scale": 1.5, "taker_fee": TAKER_FEE, "use_breaker": True}), "slippage"),
])
def test_runs_that_are_not_the_registered_experiment_are_refused(tmp_path, over, match):
    refused(tmp_path, match, **over)


def test_the_stress_run_must_really_be_a_stress_run(tmp_path):
    with pytest.raises(ds.ScreenError, match="stress"):
        ds.screen(good(tmp_path), "S-B4", make_run(tmp_path, "s", wins_total=9000.0, losses_total=-6000.0))   # base costs, not stress


def test_an_unknown_candidate_is_refused(tmp_path):
    with pytest.raises(ds.ScreenError, match="unknown candidate"):
        ds.check_identity({"manifest": {}, "summary": {}}, "S-X", stress=False)


# ------------------------------------------------------------------------------------------------ the register is real
def test_registered_hashes_equal_what_the_manifest_records_for_a_real_run_configuration():
    from g_aegisbot.backtest import manifest as mf
    from tests.test_protected_paths import frames
    for cand, (strategies, tf) in ds.CANDIDATES.items():
        cfg = BacktestConfig(params=StrategyParams(timeframe=tf, strategies=strategies))
        m = mf.build_manifest(label="t", run_name="t", strategies=strategies, cfg=cfg, frames=frames(), funding=None)
        assert m["strategy"]["params_sha256"] == ds.registered_params_sha256(cand), cand


def test_the_register_has_four_base_candidates_and_two_conditional_combined_ones():
    assert set(ds.CANDIDATES) == {"S-B4", "S-T4", "S-B1D", "S-T1D", "S-C4", "S-C1D"}
    assert ds.CANDIDATES["S-C4"][0] == ("breakout", "trend") and ds.CANDIDATES["S-T1D"][1] == "1d"


def test_the_thresholds_are_the_declared_ones():
    assert (ds.MIN_TRADES, ds.MIN_PROFIT_FACTOR, ds.MAX_DRAWDOWN) == (600, 1.30, 0.25)
    assert (ds.STRESS_SLIPPAGE_SCALE, ds.STRESS_FEE_SCALE) == (2.0, 1.2)
    assert ds.DEV_START == pd.Timestamp("2021-11-06", tz="UTC") and ds.DEV_END == pd.Timestamp("2024-05-07", tz="UTC")


def test_the_command_line_prints_the_verdict_and_refuses_cleanly(tmp_path, capsys):
    d = good(tmp_path)
    assert ds.main(["--candidate", "S-B4", "--run", str(d)]) == 0
    assert "STRESS_RUN_NEEDED" in capsys.readouterr().out
    assert ds.main(["--candidate", "S-B1D", "--run", str(d)]) == 2
    assert "REFUSED" in capsys.readouterr().err
