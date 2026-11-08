# Strategy research log

A dated record of what was tested, what was decided in advance, and what came out. Entries are never edited after the fact; a
later correction is a new entry. Numbers are quoted from the run outputs, not recomputed.

## 1. Test 1: breakout, trend and combined on 1h (exploratory, development and validation periods)
Window 2021-11-06 to 2026-11-06, 11 pairs, 1h, 3x, historical funding, flat 0.5% maintenance margin, $10,000.

| Candidate | Return | Max DD | Sharpe | PF | Trades |
|---|---:|---:|---:|---:|---:|
| Breakout | -89.02% | -90.28% | -1.87 | 0.78 | 4,118 |
| Trend | -77.56% | -78.83% | -0.93 | 0.87 | 4,095 |
| Breakout + trend | -89.58% | -91.07% | -1.31 | 0.85 | 5,781 |

Scale-free decomposition (R = profit or loss divided by the amount risked at the initial stop):

| | Gross R | Fees R | Funding R | Net R |
|---|---:|---:|---:|---:|
| Breakout | -0.033 | 0.054 | 0.002 | -0.088 |
| Trend | -0.007 | 0.052 | 0.003 | -0.062 |

Net R was negative in almost every calendar year for both (trend 2022: +0.009R was the only positive year). Trend's gross
edge was indistinguishable from zero; breakout's was slightly negative. Costs of about 0.055R per trade turned a roughly
flat gross result into a steady loss. The window is contaminated for any confirmatory claim.

## 2. Slow-timeframe study (pre-registered; protocol v0.3 section 6.5; commit 75b99af3cbef00883f73599f5c8062973b3de65a)
Question fixed in advance: do the same rules, with every parameter unchanged, work on 4h and 1d bars, where costs in R are
about halved and about a fifth? Four candidates, one run each, development period 2021-11-06 to 2024-05-07 only, 11 pairs,
3x, historical funding. The confirmation period (2019-09-08 to 2021-11-05) was not accessed.

| Candidate | Trades | Net return | PF | Max DD (daily) | Gross $ / net $ | Verdict (`devscreen`) |
|---|---:|---:|---:|---:|---:|---|
| S-B4 breakout 4h | 463 | -5.73% | 0.97 | 35.8% | +648 / -573 | INFEASIBLE_UNDER_C1 (also fails P2, P3, P4) |
| S-T4 trend 4h | 552 | +0.62% | 1.00 | 32.6% | +1,389 / +62 | INFEASIBLE_UNDER_C1 (also fails P3, P4) |
| S-B1D breakout 1d | 35 | +8.95% | 1.52 | 6.7% | +1,035 / +895 | INFEASIBLE_UNDER_C1 |
| S-T1D trend 1d | 69 | +0.31% | 1.01 | 21.2% | +343 / +31 | INFEASIBLE_UNDER_C1 (also fails P3) |

Observations (recorded, not acted on):
* Slower bars moved gross expectancy from negative (1h) to slightly positive in all four. At 4h that gain was consumed by
  fees and funding, leaving net results at about zero.
* The daily breakout result rests on 35 trades, 15 of them winners, carried by a few pairs. At about 1.2 trades a month it
  would yield roughly 30 trades on the confirmation period, far short of the 300 that C1 requires, so it could never PASS.
* The EMA50 strategy exit lost money in every sample (1h: -$8,064 on 455 trades; 4h: -$2,586 on 49; 1d: -$936 on 10).
  Changing it now would be a rescue variant chosen after seeing the data, which the stopping rule forbids.

## 3. Decision (owner, accepted in writing)
By the registered stopping rule, no candidate was promoted, so **the breakout and trend rule family is rejected as a basis
for live trading**. No criterion was weakened and no variant was added to rescue it. The confirmation period is unused and
remains available to a future, separately pre-registered hypothesis. Variant register after this study: three 1h variants
(Test 1) plus the original breakout baseline, plus four slow-timeframe variants.

Services and live trading stay off. Any live use of these strategies requires a new passed acceptance run first.
