"""The protocol's calculation methods (backtest/acceptance_methods.py): trade lifecycles and boundaries, windows, funding,
concentration, regimes, block bootstrap with equity-path reconstruction, and the paired comparison. Pure, offline."""

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.backtest import acceptance_methods as am

T0 = pd.Timestamp("2024-01-01", tz="UTC")


def fill(symbol, hours, qty, price, fee=0.0):
    return {"symbol": symbol, "ts": T0 + pd.Timedelta(hours=hours), "qty": qty, "price": price, "fee": fee}


# ---------------------------------------------------------------- trade lifecycles
def test_a_simple_round_trip_is_one_trade():
    t = am.aggregate_lifecycles([fill("A", 0, 2, 100, 0.1), fill("A", 5, -2, 110, 0.1)])
    assert len(t) == 1 and t[0]["status"] == "closed" and t[0]["side"] == "buy"
    assert t[0]["gross_pnl"] == pytest.approx(20) and t[0]["fees"] == pytest.approx(0.2) and t[0]["net_pnl"] == pytest.approx(19.8)
    assert t[0]["opened_at"] == T0 and t[0]["closed_at"] == T0 + pd.Timedelta(hours=5)


def test_a_short_round_trip():
    t = am.aggregate_lifecycles([fill("A", 0, -3, 100), fill("A", 2, 3, 90)])
    assert len(t) == 1 and t[0]["side"] == "sell" and t[0]["gross_pnl"] == pytest.approx(30)


def test_partial_entries_and_partial_exits_are_one_trade_with_a_weighted_average():
    fills = [fill("A", 0, 1, 100, 0.1), fill("A", 1, 3, 120, 0.3),            # scale in: average (100 + 360) / 4 = 115
             fill("A", 2, -1, 130, 0.1), fill("A", 3, -3, 100, 0.3)]           # scale out in two pieces
    t = am.aggregate_lifecycles(fills)
    assert len(t) == 1 and t[0]["fills"] == 4 and t[0]["max_abs_qty"] == 4
    assert t[0]["gross_pnl"] == pytest.approx(1 * (130 - 115) + 3 * (100 - 115))
    assert t[0]["fees"] == pytest.approx(0.8) and t[0]["status"] == "closed"


def test_entering_again_after_flat_is_a_new_trade():
    fills = [fill("A", 0, 1, 100), fill("A", 1, -1, 101), fill("A", 2, 1, 102), fill("A", 3, -1, 99)]
    t = am.aggregate_lifecycles(fills)
    assert len(t) == 2 and [round(x["gross_pnl"]) for x in t] == [1, -3]


def test_a_fill_that_crosses_through_flat_is_split_with_its_fee_in_proportion():
    t = am.aggregate_lifecycles([fill("A", 0, 2, 100, 0.0), fill("A", 1, -5, 110, 5.0)])   # closes 2, opens a 3-unit short
    assert len(t) == 2
    closed, opened = t
    assert closed["status"] == "closed" and closed["gross_pnl"] == pytest.approx(20) and closed["fees"] == pytest.approx(2.0)
    assert opened["status"] == "open" and opened["side"] == "sell" and opened["fees"] == pytest.approx(3.0)


def test_simultaneous_positions_in_different_symbols_are_separate_trades():
    fills = [fill("A", 0, 1, 10), fill("B", 1, -1, 20), fill("A", 4, -1, 11), fill("B", 5, 1, 19)]
    t = am.aggregate_lifecycles(fills)
    assert len(t) == 2 and {x["symbol"] for x in t} == {"A", "B"} and all(x["status"] == "closed" for x in t)


def test_a_lifecycle_left_open_has_no_close_and_unsorted_input_is_ordered():
    t = am.aggregate_lifecycles([fill("A", 3, -1, 11), fill("A", 0, 1, 10), fill("A", 5, 1, 12)])
    assert len(t) == 2 and t[0]["status"] == "closed" and t[1]["status"] == "open" and t[1]["closed_at"] is None


@pytest.mark.parametrize("bad", [{"qty": 0}, {"qty": float("nan")}, {"price": float("inf")}, {"fee": -1.0}, {"fee": float("nan")}])
def test_malformed_fills_are_rejected_not_skipped(bad):
    with pytest.raises(ValueError):
        am.aggregate_lifecycles([{**fill("A", 0, 1, 100), **bad}])


# ---------------------------------------------------------------- evaluation boundaries
def sim_trade(entry, exit_, reason="trailing-stop", pnl=1.0):
    return {"symbol": "A", "entry_time": pd.Timestamp(entry, tz="UTC"), "exit_time": pd.Timestamp(exit_, tz="UTC"), "reason": reason,
            "pnl": pnl, "gross_pnl": pnl, "funding": 0.0}


def test_boundary_rules_for_simulator_trades():
    start, end = "2024-02-01", "2024-03-01"
    trades = [sim_trade("2024-01-31 23:00", "2024-02-02"),                       # opened before the period: excluded
              sim_trade("2024-02-01 00:00", "2024-02-03"),                       # opened exactly at start: inside, completed
              sim_trade("2024-02-10", "2024-02-11"),                             # completed
              sim_trade("2024-02-29 23:00", "2024-03-01 00:00"),                 # normal stop stamped at the period end: still completed
              sim_trade("2024-02-20", "2024-03-01 00:00", reason=am.END_OF_DATA),    # forced close at the end: truncated
              sim_trade("2024-03-01 00:00", "2024-03-02")]                       # opened exactly at end: excluded
    r = am.split_by_period(trades, start, end)
    assert [len(r[k]) for k in ("excluded_before", "completed", "truncated", "excluded_after")] == [1, 3, 1, 1]


def test_boundary_rules_for_fill_lifecycles():
    fills = [fill("A", 0, 1, 100), fill("A", 10, -1, 101),                       # closed inside
             fill("B", 0, 1, 100), fill("B", 500, -1, 101),                      # closed after the period end
             fill("C", 600, 1, 100)]                                             # opened after the end
    lifecycles = am.aggregate_lifecycles(fills + [fill("D", 20, 1, 5)])          # D stays open
    r = am.split_by_period(lifecycles, T0, T0 + pd.Timedelta(hours=100))
    assert {x["symbol"] for x in r["completed"]} == {"A"}
    assert {x["symbol"] for x in r["truncated"]} == {"B", "D"}
    assert {x["symbol"] for x in r["excluded_after"]} == {"C"}


def test_a_position_opened_before_the_period_is_excluded_even_if_it_closes_inside():
    t = am.aggregate_lifecycles([fill("A", -5, 1, 100), fill("A", 3, -1, 105)])
    assert am.split_by_period(t, T0, T0 + pd.Timedelta(days=1))["excluded_before"] == t


# ---------------------------------------------------------------- walk-forward windows
def test_the_confirmation_windows_are_nine_with_a_seventy_day_remainder():
    w = am.walk_forward_windows("2019-09-08", "2021-11-06")
    assert len(w) == 9 and sum(x["complete"] for x in w) == 8
    assert w[0]["start"] == pd.Timestamp("2019-09-08", tz="UTC") and w[7]["end"] == pd.Timestamp("2021-08-28", tz="UTC")
    assert not w[8]["complete"] and (w[8]["end"] - w[8]["start"]).days == 70 and w[8]["end"] == pd.Timestamp("2021-11-06", tz="UTC")


def test_the_validation_windows_are_eleven_with_a_thirteen_day_remainder():
    w = am.walk_forward_windows("2024-05-07", "2026-11-06")
    assert len(w) == 11 and sum(x["complete"] for x in w) == 10 and (w[10]["end"] - w[10]["start"]).days == 13


def test_exact_multiples_and_short_periods():
    assert all(x["complete"] for x in am.walk_forward_windows("2024-01-01", "2024-03-31"))           # exactly 90 days
    only = am.walk_forward_windows("2024-01-01", "2024-02-01")
    assert len(only) == 1 and not only[0]["complete"]


def test_trades_are_attributed_by_exit_time_and_a_boundary_exit_belongs_to_the_window_that_ends_there():
    w = am.walk_forward_windows("2024-01-01", "2024-07-01")
    b = pd.Timestamp("2024-03-31", tz="UTC")
    trades = [sim_trade("2024-03-30 20:00", "2024-03-31 00:00", pnl=5.0),        # exits exactly on the W1/W2 boundary: W1
              sim_trade("2024-03-30", "2024-04-02", pnl=7.0),                    # opened in W1, exits in W2: W2
              sim_trade("2024-01-02", "2024-01-03", pnl=-2.0)]
    res = am.window_results(trades, w)
    assert w[0]["end"] == b and res[0]["net"] == pytest.approx(3.0) and res[1]["net"] == pytest.approx(7.0)
    assert sum(r["trades"] for r in res) == 3


def test_the_profitable_window_rule_uses_ceil_counts_only_complete_windows_and_empty_is_not_profitable():
    def res(nets, last_incomplete=False):
        return [{"complete": not (last_incomplete and i == len(nets) - 1), "net": n} for i, n in enumerate(nets)]
    assert am.profitable_window_check(res([1] * 5 + [-1] * 3))["passes"]                      # 5 of 8 = 62.5%
    assert not am.profitable_window_check(res([1] * 4 + [-1] * 4))["passes"]                  # 4 of 8 = 50%
    assert not am.profitable_window_check(res([1, 1, 1, 1, 1, 0, 0, 0]))["needed"] == 0        # a zero-trade window is not profitable
    assert am.profitable_window_check(res([1] * 5 + [0, 0, 0]))["profitable"] == 5
    inc = am.profitable_window_check(res([1, 1, 1, -1, -1, -1000], last_incomplete=True))      # incomplete window ignored
    assert inc["complete_windows"] == 5 and inc["profitable"] == 3 and inc["needed"] == 3 and inc["passes"]
    assert am.profitable_window_check([])["passes"] is False


def test_one_window_supplying_most_of_the_profit_is_reported():
    c = am.profitable_window_check([{"complete": True, "net": 90.0}, {"complete": True, "net": 10.0}, {"complete": True, "net": -5.0}])
    assert c["largest_window_share"] == pytest.approx(0.9)


# ---------------------------------------------------------------- funding
def ft(gross, funding):
    return {"gross_pnl": gross, "funding": funding}


def test_funding_ratio_rules():
    assert am.funding_ratio_check([ft(100, 20)])["passes"] and am.funding_ratio_check([ft(100, 20)])["ratio"] == pytest.approx(0.2)
    assert not am.funding_ratio_check([ft(100, 30)])["passes"]
    assert am.funding_ratio_check([ft(100, -10)])["passes"] and am.funding_ratio_check([ft(100, -10)])["ratio"] == 0.0   # income


@pytest.mark.parametrize("gross,funding", [(0.0, 0.0), (-50.0, 5.0), (-50.0, -5.0), (0.0, -10.0)])
def test_zero_or_negative_gross_profit_never_passes_the_funding_criterion(gross, funding):
    r = am.funding_ratio_check([ft(gross, funding)])
    assert r["passes"] is False and r["ratio"] is None and "not positive" in r["reason"]


# ---------------------------------------------------------------- concentration
def ct(symbol, pnl):
    return {"symbol": symbol, "pnl": pnl}


def test_concentration_recomputes_without_replacing_removed_trades():
    trades = [ct("A", 50), ct("A", 40)] + [ct("B", 10)] * 5 + [ct("C", 5)] * 5      # total 165; 12 trades; k = ceil(0.6) = 1
    r = am.concentration_check(trades)
    assert r["total_net"] == pytest.approx(165) and r["best_pair"] == "A" and r["best_pair_share"] == pytest.approx(90 / 165)
    assert r["net_without_best_pair"] == pytest.approx(75) and r["removed_top_trades"] == 1
    assert r["net_without_top_trades"] == pytest.approx(115)                           # 165 minus the single best trade, nothing added back
    assert r["passes"] is False                                                        # 54.5% share exceeds 40%


def test_concentration_passes_when_spread_and_fails_on_the_other_conditions():
    spread = [ct(s, 10) for s in "ABCDE" for _ in range(4)]
    assert am.concentration_check(spread)["passes"] is True
    top_heavy = [ct("A", 1000), ct("B", -300), ct("C", -300), ct("D", -300)]
    r = am.concentration_check(top_heavy)
    assert r["total_net"] == pytest.approx(100) and r["removed_top_trades"] == 1
    assert r["net_without_top_trades"] == pytest.approx(-900) and r["passes"] is False          # removing the best trade turns it negative


def test_concentration_fails_when_total_is_not_positive():
    r = am.concentration_check([ct("A", -5), ct("B", 4)])
    assert r["passes"] is False and r["best_pair_share"] is None
    assert am.concentration_check([])["passes"] is False


def test_the_top_five_percent_removes_ceil_of_the_count():
    trades = [ct("A", 1)] * 41 + [ct("B", 1)] * 40                                      # n = 81, k = ceil(4.05) = 5
    assert am.concentration_check(trades)["removed_top_trades"] == 5


# ---------------------------------------------------------------- regimes
def btc(n=700, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02 * (1 + np.sin(np.arange(n) / 40) ** 2), n))), index=idx)


def test_regime_labels_use_only_information_up_to_that_day():
    full = btc(700)
    part = full.iloc[:500]
    lf, lp = am.regime_labels(full), am.regime_labels(part)
    assert (lf.iloc[:500].values == lp.values).all()                  # appending the future changes no earlier label


def test_regime_labels_have_an_unclassified_warmup_and_all_six_regimes_appear_later():
    lab = am.regime_labels(btc(1500, seed=5))
    assert (lab.iloc[:200] == "unclassified").all()
    assert {"up-low", "up-mid", "up-high", "down-low", "down-mid", "down-high"} <= set(lab.unique())


def test_every_regime_is_reported_and_an_undersampled_one_is_inconclusive_not_omitted():
    lab = am.regime_labels(btc(700))
    entry = lab.index[400] + pd.Timedelta(days=1)
    trades = [{"symbol": "A", "entry_time": entry, "pnl": 5.0}] * 3
    table = am.regime_table(trades, lab)
    assert set(table) >= {"up-low", "up-mid", "up-high", "down-low", "down-mid", "down-high", "unclassified"}
    assert all(v["status"] == "INCONCLUSIVE" for v in table.values())                     # three trades: nothing reaches 30
    used = lab.iloc[400]
    assert table[used]["trades"] == 3 and table[used]["net_expectancy"] == pytest.approx(5.0)
    assert sum(v["trades"] for v in table.values()) == 3


def test_a_trade_takes_the_label_of_the_previous_day_and_enough_trades_make_a_regime_evaluated():
    lab = am.regime_labels(btc(700))
    d = lab.index[450]
    trades = [{"symbol": "A", "entry_time": d + pd.Timedelta(hours=7), "pnl": -1.0}] * 30
    table = am.regime_table(trades, lab)
    assert table[lab.loc[d - pd.Timedelta(days=1)]]["trades"] == 30 and table[lab.loc[d - pd.Timedelta(days=1)]]["status"] == "EVALUATED"


# ---------------------------------------------------------------- daily returns
def test_daily_returns_take_the_last_value_of_each_day_and_carry_forward_gaps():
    idx = pd.to_datetime(["2024-01-01 06:00", "2024-01-01 23:00", "2024-01-02 12:00", "2024-01-04 01:00"], utc=True)
    r = am.daily_returns(pd.Series([100.0, 110.0, 99.0, 120.0], index=idx), initial=100.0)
    assert r == pytest.approx([0.10, -0.10, 0.0, 120 / 99 - 1])      # day 3 has no row: carried forward, return 0
    naive = pd.Series([100.0, 105.0], index=pd.to_datetime(["2024-01-01 01:00", "2024-01-02 01:00"]))
    assert am.daily_returns(naive) == pytest.approx([0.0, 0.05])


# ---------------------------------------------------------------- block bootstrap: indices
def test_block_indices_are_circular_blocks_of_consecutive_days():
    idx = am.block_indices(23, 5, 200, seed=7)
    assert idx.shape == (200, 23) and idx.min() >= 0 and idx.max() < 23
    first = idx[:, :5]
    assert (np.diff(first, axis=1) % 23 == 1).all()                         # inside a block the days are consecutive, wrapping at the end
    assert (am.block_indices(23, 5, 200, 7) == idx).all() and not (am.block_indices(23, 5, 200, 8) == idx).all()
    assert (idx == 22).any() and ((np.diff(idx[:, :5], axis=1)) < 0).any()   # wrap-around happens


@pytest.mark.parametrize("n,L", [(10, 0), (10, 11), (5, 6)])
def test_an_invalid_block_length_is_rejected(n, L):
    with pytest.raises(ValueError):
        am.block_indices(n, L, 10, 1)


# ---------------------------------------------------------------- block bootstrap: equity paths and drawdown
def test_max_drawdown_of_known_paths():
    assert am.max_drawdown(np.array([1.0, 1.2, 0.9, 1.0, 0.6, 1.1]))[0] == pytest.approx(0.5)    # (1.2 - 0.6) / 1.2
    assert am.max_drawdown(np.array([1.0, 1.1, 1.3, 1.6]))[0] == 0.0                              # only rising
    assert am.max_drawdown(np.array([1.0, 0.9, 0.7, 0.5]))[0] == pytest.approx(0.5)               # only falling: the start is the peak
    two = am.max_drawdown(np.array([[1.0, 2.0, 1.0], [1.0, 1.0, 1.0]]))
    assert two == pytest.approx([0.5, 0.0])


def test_reconstructed_paths_chain_the_resampled_returns_in_order():
    r = np.array([0.10, -0.05, 0.02, 0.03, -0.20, 0.04, 0.01])
    paths = am.reconstruct_paths(r, block_len=3, n_boot=50, seed=4, initial=100.0)
    idx = am.block_indices(len(r), 3, 50, 4)
    assert paths.shape == (50, len(r) + 1) and (paths[:, 0] == 100.0).all()
    assert paths[:, 1:] == pytest.approx(100.0 * np.cumprod(1 + r[idx], axis=1))                  # compounding, not summing
    assert paths[:, -1] != pytest.approx(100.0 * (1 + r.sum()))


def test_the_original_order_gives_the_original_path_and_a_different_order_gives_a_different_drawdown():
    r = np.array([-0.20, 0.10, 0.10, 0.10, -0.30, 0.05])
    same = am.reconstruct_paths(r, n_boot=1, indices=np.arange(len(r))[None, :])
    direct = np.concatenate([[1.0], np.cumprod(1 + r)])
    assert same[0] == pytest.approx(direct) and am.max_drawdown(same)[0] == pytest.approx(am.max_drawdown(direct)[0])
    swapped = am.reconstruct_paths(r, n_boot=1, indices=np.array([[1, 2, 3, 0, 4, 5]]))
    assert am.max_drawdown(swapped)[0] != pytest.approx(am.max_drawdown(same)[0])            # the path, not the returns, sets the drawdown


def test_constant_returns_give_the_compounded_path_in_every_resample():
    paths = am.reconstruct_paths(np.full(20, 0.01), 5, 10, 1)
    assert paths == pytest.approx(np.tile(1.01 ** np.arange(21), (10, 1)))


@pytest.mark.parametrize("bad", [np.array([]), np.array([0.1, np.nan]), np.array([0.1, np.inf]), np.array([0.1, -1.0]), np.array([[0.1]])])
def test_invalid_return_series_are_rejected(bad):
    with pytest.raises(ValueError):
        am.reconstruct_paths(bad, 1, 5, 1)


def test_bounds_are_quantiles_of_the_bootstrap_distribution():
    x = np.arange(1001, dtype=float)
    assert am.lower_bound(x, 0.983) == pytest.approx(np.quantile(x, 0.017)) and am.upper_bound(x, 0.983) == pytest.approx(np.quantile(x, 0.983))


# ---------------------------------------------------------------- block bootstrap: trade statistics
def day_trades(pnls_by_day, start="2024-01-01"):
    out = []
    for d, pnls in pnls_by_day.items():
        for p in pnls:
            out.append({"symbol": "A", "pnl": p, "exit_time": pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=d, hours=12)})
    return out


def test_trade_day_aggregates_keep_empty_days_and_attribute_midnight_exits_to_the_day_that_ended():
    trades = day_trades({0: [10.0, -4.0], 3: [6.0]})
    trades.append({"symbol": "A", "pnl": 2.0, "exit_time": pd.Timestamp("2024-01-03", tz="UTC")})       # stamped at midnight: day 2
    a = am.trade_day_aggregates(trades, "2024-01-01", "2024-01-06")
    assert len(a["n"]) == 5 and list(a["n"]) == [2, 1, 0, 1, 0]          # the midnight exit of 3 Jan counts on 2 Jan (index 1)
    assert list(a["pnl"]) == [6.0, 2.0, 0.0, 6.0, 0.0] and list(a["win"]) == [10.0, 2.0, 0.0, 6.0, 0.0] and list(a["loss"])[0] == 4.0


def test_bootstrap_trade_statistics_edge_cases():
    winners = am.bootstrap_trade_stats(day_trades({d: [5.0] for d in range(30)}), "2024-01-01", "2024-01-31", 5, 200, 1)
    assert np.isinf(winners["profit_factor"]).all() and winners["expectancy"] == pytest.approx(5.0)
    none = am.bootstrap_trade_stats([], "2024-01-01", "2024-01-31", 5, 50, 1)
    assert (none["expectancy"] == 0).all() and (none["profit_factor"] == 0).all()


def test_bootstrap_expectancy_brackets_the_true_mean():
    rng = np.random.default_rng(2)
    pnl = rng.normal(2.0, 10.0, 600)
    trades = day_trades({d: [float(pnl[d * 2]), float(pnl[d * 2 + 1])] for d in range(300)})
    s = am.bootstrap_trade_stats(trades, "2024-01-01", "2024-10-27", 10, 2000, 3)
    pf = s["profit_factor"]
    assert am.lower_bound(s["expectancy"]) < pnl.mean() < am.upper_bound(s["expectancy"])
    assert np.isfinite(pf).all() and 0 < am.lower_bound(pf) < am.upper_bound(pf)


# ---------------------------------------------------------------- block length sensitivity
def test_block_length_sensitivity_reports_every_length_and_flags_a_flipping_verdict():
    r = np.random.default_rng(1).normal(0.0005, 0.01, 400)
    ok = am.block_length_sensitivity(r, n_boot=500, dd_limit=0.99)
    assert set(ok["by_block_length"]) == {5, 10, 20, 30} and ok["stable"] is True
    p95 = [v["dd_p95"] for v in ok["by_block_length"].values()]
    flips = am.block_length_sensitivity(r, n_boot=500, dd_limit=(min(p95) + max(p95)) / 2)
    assert flips["stable"] is False and len({v["within_limit"] for v in flips["by_block_length"].values()}) == 2
    assert am.block_length_sensitivity(r[:8], n_boot=50)["by_block_length"].keys() == {5}      # longer blocks than data are skipped


# ---------------------------------------------------------------- paired comparison
def test_a_clear_paired_improvement_is_distinguishable_and_an_identical_series_is_not():
    rng = np.random.default_rng(4)
    comp = rng.normal(0.0003, 0.02, 500)
    better = comp + 0.002                                                   # same days, same noise, a steady edge
    assert am.paired_improvement(better, comp, "sharpe", n_boot=2000)["distinguishable"] is True
    assert am.paired_improvement(better, comp, "calmar", n_boot=2000)["point"] > 0
    same = am.paired_improvement(comp, comp, "sharpe", n_boot=500)
    assert same["point"] == 0 and same["distinguishable"] is False


def test_drawdown_reduction_needs_ten_percent_and_a_positive_lower_bound():
    n = 600
    comp = np.tile([0.02, 0.02, -0.06], n // 3)                              # a component that keeps drawing down
    smooth = comp * 0.4
    r = am.paired_improvement(smooth, comp, "drawdown_reduction", n_boot=1000)
    assert r["point"] >= 0.10 and r["distinguishable"] is True
    none = am.paired_improvement(comp * 0.99, comp, "drawdown_reduction", n_boot=1000)
    assert none["point"] < 0.10 and none["distinguishable"] is False


def test_paired_comparison_rejects_unpaired_input_and_unknown_metrics():
    a = np.zeros(10) + 0.001
    with pytest.raises(ValueError):
        am.paired_improvement(a, a[:9], "sharpe")
    with pytest.raises(ValueError):
        am.paired_improvement(a, a, "profit")
    with pytest.raises(ValueError):
        am.paired_improvement(a.reshape(2, 5), a.reshape(2, 5), "sharpe")


def test_paired_resampling_uses_the_same_days_for_both_series(monkeypatch):
    seen = []
    real = am.block_indices
    monkeypatch.setattr(am, "block_indices", lambda *a, **k: seen.append(a) or real(*a, **k))
    am.paired_improvement(np.full(50, 0.001), np.full(50, 0.0005), "sharpe", n_boot=20)
    assert len(seen) == 1                                                   # one index matrix drives both series
