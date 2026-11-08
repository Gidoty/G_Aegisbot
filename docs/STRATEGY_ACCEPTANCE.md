# Strategy acceptance protocol

**Status: DRAFT v0.2 for independent review. It is not approved and not frozen. No result may be judged against it yet.**

A protocol version is *approved* only when a person writes an approval record
(`docs/STRATEGY_ACCEPTANCE.approval.json`) that names this exact file by its SHA-256, and *frozen* when a freeze record
(section 12) is created from it. Both hashes go into every backtest manifest. Any later edit to this file makes the approval
void, creates a new version, and burns the confirmation result of every candidate that had already been evaluated (section 11).

Passing this protocol does not authorise live trading. It only allows a candidate to enter the paper soak. Live trading needs a
separate written approval.

## 1. Candidates, judged separately
1. `breakout` alone (Strategy A)
2. `trend` alone (Strategy B)
3. `breakout+trend` combined, as one portfolio with the production conflict and risk rules

Each candidate is held to the same criteria (section 7) on its own numbers. A failing candidate stays failed. The combined
portfolio has extra requirements (section 10).

## 2. Exploratory and confirmatory analysis
* **Exploratory** analysis looks at data in order to understand a strategy: development and validation runs, parameter
  sensitivity, diagnostics, cost decomposition, any run made without `--acceptance`. Its results may change what a person
  thinks, and they carry no confirmatory weight. A manifest from such a run has `acceptance_eligible: false`.
* **Confirmatory** analysis tests a question that was fixed in writing before the data was read. Only confirmation-period runs
  made with `--acceptance`, an approved protocol and a valid freeze record are confirmatory. Each candidate gets exactly one.
* Looking at confirmation performance "just to check" is a confirmatory evaluation and uses up the candidate's one attempt.
* Every run, exploratory or confirmatory, keeps its full output directory and manifest. An append-only register of all runs
  and strategy or parameter variants is part of the freeze tooling (Phase A4). Until it exists the operator keeps every
  manifest and does not delete any run. The number of variants examined is reported with every result (section 6.4).

## 3. Periods and data
Only genuine Binance USDT-M futures history is used. Synthetic data is never used to judge a strategy.

All periods are UTC, start inclusive, end exclusive.

| Period | Start | End | Days | Role |
|---|---|---|---|---|
| Development | 2021-11-06 | 2024-05-07 | 913 | exploratory diagnosis, sensitivity |
| Validation | 2024-05-07 | 2026-11-06 | 913 | supporting evidence, sensitivity, first frozen-protocol check |
| Confirmation (candidate) | 2019-09-08 | 2021-11-06 | 790 | the independent historical confirmation, read once per candidate |

* **Development and validation (2021-11-06 to 2026-11-06)** are contaminated. The original breakout baseline already ran over
  the whole window, and Strategy B was designed after that result was known. Their results are supporting evidence, never
  proof, and are reported with that caveat.
* **Confirmation (2019-09-08 to 2021-11-05 inclusive)** is a *candidate* independent confirmation. It is used on the basis of
  the following **declaration**, recorded verbatim in the freeze record: *the 2019-09-08 to 2021-11-05 period was not
  previously examined, and was not used by the user or the development team to guide any strategy decision.* This is the
  user's declaration. It has not been independently verified and cannot be proved from the repository, which documents no
  evaluation on that period (consistent with the declaration, not proof of it). If the declaration turns out to be wrong, the
  confirmation result is void.
* The confirmation data must pass the same technical integrity checks as all other data (no gap, duplicate, out-of-order or
  misaligned candle; funding complete). A failed check means INCONCLUSIVE, not a pass.
* **Universe.** The frozen universe is listed in the freeze record. A pair enters a period only from its listing date plus the
  25-day warm-up; pairs not yet listed are absent from that period. Few pairs existed in 2019 to 2020, so the confirmation
  trade count may fall below 300, which makes the outcome INCONCLUSIVE.
* **Survivorship.** The universe consists of pairs that are still large today. Every report says so.
* **Forward paper trading is operational validation** (order handling, reconciliation, uptime, signal parity). It is not an
  independent historical confirmation and is never described as one.

## 4. Cost and execution model (fixed)
* Fees: Binance standard taker 0.05% on every order. No VIP tier, no BNB discount. Fee stress: 0.06%.
* Slippage: 3 bps (BTC, ETH), 6 bps (other pairs), 12 bps (FET, 1000PEPE, RENDER), stop orders 1.5 times. Stress: 2 times.
* Funding: real historical funding only. The run aborts on any gap. `approx` and `none` are exploratory only.
* **Liquidation: actual Binance leverage-bracket data is required for final validation.** The flat 0.5% default is
  exploratory only and cannot satisfy C11. Limitation, stated openly: a bracket file reflects the tiers at the time it was
  obtained, and Binance has changed tiers over the years, so brackets for 2019 to 2021 are an approximation. The manifest
  records the file hash and marks the run `liquidation.kind: exact` only when every symbol is covered by file data.
* Fills: signals on closed candles, entries and strategy exits at the next bar's open, stops tested against the bar's high and
  low, gaps fill at the open, and inside a bar the stop is assumed to trigger first.
* Production risk limits (portfolio limits, per-trade risk, 4 positions, 3x multiplier, 4% daily breaker) are always on.
  `--legacy-risk`, `--no-breaker` and `--allow-partial` are never used for acceptance. Risk settings are never loosened to
  improve a result.

## 5. What counts as a trade (B)
* **One trade is one completed position lifecycle**, from the fill that moves a symbol's position away from flat to the fill
  that returns it to flat. It is never one fill.
* **Partial entries and exits** are aggregated into their lifecycle (`aggregate_lifecycles`): the average entry is the
  quantity-weighted mean, gross PnL is realised on every reduction, fees are summed. A fill that crosses through flat is split
  at flat, the fee in proportion to quantity.
* **Repeated entries:** entering again after the position went flat is a new trade. The strategies never add to an open
  position (one position per symbol), so scale-in only arises in exchange-fill accounting.
* **Simultaneous positions** in different symbols are separate trades, each counted once, even when they overlap in time.
* **Evaluation boundaries** (`split_by_period`), for a measured period [start, end):
  * opened before `start`: excluded from that period (the backtester makes no entries before `start`; warm-up replays
    candles only);
  * opened at or after `end`: excluded;
  * opened inside and closed by the strategy inside: **completed**. Only completed trades count for the sample-size
    criterion and per-trade statistics;
  * opened inside and still open at `end`, closed by the simulator at the period's last close (`end-of-data`): **truncated**.
    Not counted as completed, but their PnL stays in every PnL total, so nothing is hidden. The number is reported.
* Each evaluation period (confirmation, validation) is its own run from its own measured start. Walk-forward windows (section
  8) are slices of one run: a trade is attributed to the window containing its exit time, with no state reset at window edges. The simulator stamps an
  exit with the close time of its bar, so an exit stamped exactly on a boundary belongs to the window (and calendar day) that ends there.

## 6. Statistical methodology (C)
### 6.1 Confidence level and its limits
The provisional level is **98.3% one-sided** (alpha 0.017, close to 0.05 divided by the 3 candidates). The lower bound of a
statistic is the (1 - 0.983) quantile of its bootstrap distribution. Limits, which a reader must keep in mind:
* A correction for three candidates does not cover repeated tuning, additional strategy or parameter variants, many metrics, or
  repeated looking at validation data. Each of those inflates the chance of a false pass.
* The criteria are combined by "all must hold", which limits false passes from the number of metrics. It does not remove
  selection effects from earlier choices.
* The bootstrap resamples days of one history. It cannot create regimes the history did not contain.
* 0.983 is a convention, not a derivation. It is retained provisionally and will be revisited if the variant register shows more
  than three confirmation candidates.

### 6.2 Daily equity path
Daily returns come from the marked-to-market equity curve: the last equity value of each UTC day, each day's return against
the previous day's last value (first day against starting capital); days without a row carry the previous value forward.

### 6.3 Block bootstrap (path reconstruction)
* **Resampling unit:** circular moving blocks of `L` consecutive days, starting at uniformly random days, wrapping around the end;
  `ceil(n / L)` blocks per resample, truncated to `n` days. Reference `L = 10`. 10,000 resamples, fixed seed 1.
* **Equity-path reconstruction:** the resampled daily returns are chained in order, `equity[t] = start x product(1 + r*)`.
  Each resample is a real compounding equity curve with its own peaks and troughs.
* **Maximum drawdown** is measured on that reconstructed path (largest peak-to-trough fall, the running peak including the
  start). It is never derived from a statistic of the resampled returns. The reported value is the 95th percentile over the
  resamples (C4).
* **Trade statistics** (net expectancy per trade, profit factor): whole calendar days with the trades that exited on them are
  resampled in the same blocks, and each statistic is recomputed from the trades in the resample. Days with no trades are kept
  so blocks keep their real spacing.
* **Sensitivity to the block length (C13):** the drawdown verdict is recomputed for `L` in {5, 10, 20, 30}, and so are the
  C2, C3 and C4 verdicts. If any verdict changes with `L`, the outcome is INCONCLUSIVE.

### 6.4 Variant register
Every run is registered with its experiment id, candidate, parameter hash, period and purpose. The report states: the number of
distinct strategy and parameter variants examined on development and validation data, how many were promoted to confirmation
candidates, and that parameters were not chosen from results. A variant promoted after looking at data is a new candidate and
raises the number used in the confidence level.

## 7. Acceptance criteria (D) with exact calculations
All hold on the **confirmation** run unless stated. Calculations use only completed trades unless stated (section 5).

| # | Criterion and exact calculation |
|---|---|
| C1 | At least **300** completed trades in the confirmation run (200 in validation). Fewer is **INCONCLUSIVE**, never PASS. |
| C2 | Net expectancy per trade = mean net PnL (gross minus fees minus funding) of completed trades, above 0. The 98.3% bootstrap lower bound above 0. Validation point estimate above 0. |
| C3 | Profit factor = sum of winning trades' net PnL / absolute sum of losing trades' net PnL, at least **1.30**. Bootstrap lower bound above 1.0. |
| C4 | Maximum drawdown of the run's daily marked-to-market equity at most **25%**. The bootstrap 95th percentile of the reconstructed-path maximum drawdown at most 35%. |
| C5 | Walk-forward (section 8): profitable complete windows at least `ceil(0.60 x complete windows)`; no single window above 50% of total positive window PnL. |
| C6 | Cost stress: with slippage at 2 times and taker fee 20% higher (0.06%), net expectancy still above 0 and profit factor above 1.0. |
| C7 | Funding rule (section 9). |
| C8 | Concentration rule (section 9). |
| C9 | Regime rule (section 9). |
| C10 | Sensitivity rule (section 9), on development and validation only. |
| C11 | Manifest eligibility: real data, data integrity passed, historical funding, production risk limits, exact liquidation brackets, approved protocol, valid freeze record. |
| C12 | Reproducibility rule (section 9). |
| C13 | Bootstrap block-length stability (section 6.3). |

Outcomes: **PASS** (every criterion met), **FAIL** (any criterion clearly missed), **INCONCLUSIVE** (too few trades, a data or
integrity problem, an under-sampled criterion that cannot be evaluated, or a verdict that flips with the block length).
Gross expectancy is reported as a diagnostic: positive before costs and negative after them fails C2, and the report says costs
are the cause.

## 8. Walk-forward windows (E1)
Windows are 90 days, consecutive, non-overlapping, starting at 00:00 UTC on the period's start date. The final remainder,
shorter than 90 days, is an **incomplete window**: it is reported, excluded from the share of profitable windows, and its PnL still
counts in every total. A window is profitable only if its net PnL (completed and truncated trades exiting in it, after all costs)
is strictly above 0; a window with no trades is not profitable.

* **Confirmation (790 days): 9 windows, 8 complete.** W1 2019-09-08 to 2019-12-07, W2 to 2020-03-06, W3 to 2020-06-04,
  W4 to 2020-09-02, W5 to 2020-12-01, W6 to 2021-03-01, W7 to 2021-05-30, W8 to 2021-08-28, W9 2021-08-28 to 2021-11-06
  (incomplete, 70 days). C5 needs at least 5 of the 8 complete windows profitable.
* **Validation (913 days): 11 windows, 10 complete.** W1 2024-05-07 to 2024-08-05, W2 to 2024-11-03, W3 to 2025-02-01,
  W4 to 2025-05-02, W5 to 2025-07-31, W6 to 2025-10-29, W7 to 2026-01-27, W8 to 2026-04-27, W9 to 2026-07-26, W10 to
  2026-10-24, W11 2026-10-24 to 2026-11-06 (incomplete, 13 days). Needs at least 6 of 10; reported as supporting evidence.
Windows are fixed here, before any result, and are part of the freeze record.

## 9. Criterion details (E)
**Funding (C7).** Funding is charged per funding event on the position's notional at the bar open: longs pay a positive rate,
shorts receive it, so income is possible. For the counted trades: G = sum of gross PnL (before fees and funding); F = funding
paid minus funding received.
* G at or below 0: the criterion **fails** (there is no profit for funding to be a share of). It is never a pass.
* F at or below 0 (net income): ratio 0, passes.
* otherwise passes only if F / G is at most 0.25.

**Concentration (C8).** On the counted trades, total net PnL T must be above 0 (else the criterion fails). The best pair is the
pair with the largest net PnL; its share is its net PnL / T and must be at most 40%. Net PnL without the best pair
(T minus that pair's net) must stay above 0. Net PnL without the best 5% of trades (k = ceil(0.05 x n) best trades by net PnL,
T minus their sum) must stay above 0. Removed trades are **not replaced**: the total is recomputed from the remaining trades.

**Regimes (C9).** Computed from information known when a trade is opened (`regime_labels`): daily BTC close against its 200-day
EMA gives `up` or `down`; BTC's realised volatility (standard deviation of the last 30 daily log returns) is placed in a tercile
judged against its own *earlier* values only (expanding history, at least 90 earlier values). Labels are `up-low` to
`down-high`; days without enough history are `unclassified`. A trade opened on date D takes the label of D minus one day. Every
regime is reported, including `unclassified`. A regime with fewer than 30 trades is reported as **INCONCLUSIVE**, never omitted
and never counted as a pass. C9 fails if a regime holding at least 20% of trades has a negative net expectancy whose bootstrap
upper bound is below 0. If any regime holding at least 20% of trades is INCONCLUSIVE, C9 is INCONCLUSIVE.

**Parameter sensitivity (C10), diagnostic only.** Each parameter is moved one step either side of its frozen value, one at a time,
on development and validation only, never on confirmation data. Results are used to see fragility, never to choose or change a
parameter. C10 passes when at least 70% of the neighbours keep a positive net expectancy on validation.

| Candidate | Parameter | Steps |
|---|---|---|
| breakout | `breakout_lookback` | 15, 20, 25 |
| breakout | `vol_mult` | 1.25, 1.5, 1.75 |
| breakout | `stop_atr_mult` | 1.5, 2.0, 2.5 |
| breakout | `trail_atr_mult` | 1.5, 2.0, 2.5 |
| trend | `pullback_window` | 6, 8, 10 |
| trend | `min_pullback_atr` | 0.75, 1.0, 1.25 |
| trend | `trend_stop_atr_mult` | 1.5, 2.0, 2.5 |
| trend | `trend_trail_atr_mult` | 2.5, 3.0, 3.5 |
| combined | all of the above, one at a time | 16 neighbours |

**Reproducibility (C12).** A matching experiment id alone is not enough. A rerun must agree on every one of: the code hash,
configuration hash, parameter hash, protocol hash, freeze-record hash, liquidation-file hash, every per-symbol candle hash, every
per-symbol funding hash, and the hashes of `summary.json`, `trades.csv` and `equity.csv` (`compare_manifests` returns no
difference, and `verify_outputs` returns no problem on both).

## 10. The combined portfolio (F)
`breakout+trend` must independently satisfy C1 to C13. It does not have to beat the best component's absolute return. In addition:
1. **Both components** must show a positive net expectancy on validation and on confirmation (point estimates), else the
   combination is not accepted.
2. **Diversification benefit** against the better component, judged on the confirmation run's daily returns. Exactly one metric is
   named in the freeze record **before** any confirmation result is seen, from: Sharpe (daily, annualised on 365 days), Calmar
   (annualised return over maximum drawdown), or maximum-drawdown reduction (relative, at least 10%).
3. **Paired block bootstrap** (`paired_improvement`): the portfolio's and the better component's daily returns, for the same days,
   are resampled with the **same** block indices (reference `L = 10`, 10,000 resamples). The improvement must be distinguishable
   from noise: the one-sided 98.3% lower bound of the metric difference must be above 0, and for drawdown reduction the point
   estimate must also be at least 10%.
Which component is "better" is decided on the validation run's metric named in the freeze record, before confirmation.

## 11. Holdout protection (H)
1. This protocol is approved (approval record) and frozen (freeze record) before any confirmation data is read.
2. One confirmatory evaluation per candidate, with the freeze record present.
3. Any change to strategy code, parameters, universe, data methodology, cost assumptions or this protocol **burns** the
   confirmation result of the affected candidates: it cannot serve again as a holdout for them.
4. No interim inspection of confirmation performance. Looking is evaluating.
5. A failed candidate is not retried against the same data. A rerun is allowed only for a documented data or tooling error with
   no strategy change.
6. All attempts and variants are recorded (section 2, section 6.4).
7. Holdout exposure is decided from timestamps: a run's **measured** start and end are compared with the frozen confirmation
   period. Segment names and options do not matter. Every manifest records the measured period, the overlap, and whether any
   candle read (warm-up included) lies in the confirmation period.

## 12. Freeze record and manifest eligibility
The freeze record is a JSON file with its own hash. It contains: the protocol hash; the source-code hash; each candidate with
its strategies and parameter hash; the universe; all period boundaries and the confirmation period name; the statistical
methodology (confidence, block lengths, resamples, seed, windows, thresholds); the diversification metric; the base and stress
cost profiles; the required liquidation-bracket file hash; and the user's declaration.

Each manifest carries `eligibility`:
* `synthetic_data`
* `data_integrity_passed` (real data, every symbol's report clean, no excluded symbol, funding valid)
* `protocol_approved`
* `freeze_record_valid` (internally consistent and matching this run: code, parameters, universe, costs, brackets, period)
* `acceptance_eligible`: true only if all of the above hold and also: real data, historical funding, production risk limits, exact
  liquidation brackets, and `--acceptance` was requested. Real data alone never makes a run an acceptance run.

## 13. Report contents
Every candidate report states: the experiment id and manifest hashes; the periods; the declaration and its status as unverified;
the contamination and survivorship caveats; gross, fees, slippage, funding and net; completed and truncated trade counts;
maximum drawdown and exposure; trade distribution by month, pair and hour; every window; bootstrap intervals and the
block-length check; the sensitivity table; the regime table including INCONCLUSIVE regimes; the variant count; and one line per
criterion C1 to C13 with PASS, FAIL or INCONCLUSIVE. A candidate that did not pass is never called profitable.

## 14. Items still for the reviewer
* The exact diversification metric to name in the freeze record (Sharpe, Calmar, or drawdown reduction), and the rule for
  "better component".
* The minimum regime sample (30 trades) and the 20% materiality threshold in C9.
* How the actual bracket data is obtained. Binance's bracket endpoint needs a signed (key) request, which this project has not
  authorised, so the file may have to be transcribed from Binance's published tables. Historical bracket changes are not modelled.
* Whether 0.983 stays, or the exact 1 - 0.05/3 is used.
* The append-only register of runs and variants is described but not yet built (Phase A4).
