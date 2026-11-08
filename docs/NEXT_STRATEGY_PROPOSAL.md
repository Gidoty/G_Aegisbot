# Proposed second strategy: funding-rate carry (spot long, perpetual short)

**Status: PROPOSED. Nothing is pre-registered, nothing has been run, no code exists for it.** It becomes a candidate only after the
owner approves this document and a protocol amendment (section 5) is committed before any run. The directional strategies
(breakout, trend) are rejected (`docs/RESEARCH_LOG.md`) and are not reused.

## 1. Why this one
Breakout and trend try to predict price direction, and across 1h, 4h and 1d they showed no edge worth its costs. Funding carry does not
predict price. It collects a payment that perpetual-futures traders make to each other: when the market is net long, longs pay shorts
every 8 hours. Holding the same amount of the coin on the spot market (long) and short on the perpetual leaves roughly zero price
exposure (delta-neutral) and earns the funding paid to the short. It is a structural payment, not a forecast, and it fits the
goal of a sustainable, low-volatility return.

**Expected size, stated plainly.** The baseline funding rate is 0.01% per 8 hours, about 11% a year on the hedged notional, and it
varies: higher in bull phases, near zero or negative in bear phases. The money is deployed on two legs, so the return on total
capital is roughly half of that before costs. Plausible net returns are single-digit to low-double-digit percent a year, with small
drawdowns. This is not a high-return strategy. It may also fail the tests below, and then it is rejected like the others.

## 2. Risks that decide whether it is acceptable
* **Funding turns negative** and the short leg pays. Mitigation: an exit rule on trailing funding (below). Residual loss is small per period.
* **Liquidation of the short leg** in a sharp rally, while the spot gain sits in another wallet. Mitigation: the perpetual leg is
  held at 1x notional with a margin buffer well above the exchange minimum, and a rebalance rule.
* **Basis risk.** Spot and perpetual prices can diverge for a while. The backtest cannot measure this (section 4).
* **Counterparty and operational risk.** Both legs are on one exchange; transfers between the spot and futures wallets need an
  API permission that the current key policy (reading and futures only, withdrawals off) does not grant. This is a real
  engineering and security decision, listed in section 6.
* **Costs.** A round trip costs about 0.3% of notional (spot fee 0.1% each way plus the perpetual fee) plus slippage. At 11% a year it takes
  roughly a month of funding to repay, so entries must be rare and held for weeks.

## 3. Rule (every number fixed in advance, none tuned)
Instruments: BTC and ETH only (the most liquid; one pair each of spot and USDT-margined perpetual).
* **Signal** each funding settlement from the mean of the last 21 settlements (7 days) of that instrument's funding rate.
* **Enter** (open both legs together, equal notional) when that mean is at least 0.010% per 8 hours (the baseline).
* **Exit** (close both legs) when the mean falls below 0.005% per 8 hours, or when the spot-to-perpetual basis exceeds 2% against the position.
* **Size:** each instrument gets at most 25% of allocated capital as notional per leg, perpetual at 1x with its margin kept
  separate and topped up when the margin ratio falls below 50% (an operational rule, not a return rule).
* No stops on the hedge as a pair; the risk controls are the margin buffer, the exit rule and the drawdown breaker.

## 4. What the backtest can and cannot show (known before running)
Data the VPS already holds from the earlier runs: perpetual candles (hourly from October 2021, daily from mid-2020) and complete historical
funding for the 11 pairs, plus the cost model.
Spot prices are **not** held. The first test therefore assumes the spot leg tracks the perpetual exactly (basis ignored) and charges
a basis-slippage haircut of 0.10% of notional per round trip. Funding payments use the actual settlement rates on the actual
notional. That overstates the return slightly, so a positive result is necessary but not sufficient: a second test with real spot
candles (downloadable the same way, public data) must confirm it before any paper trading.

## 5. Pre-registration needed before any run (protocol amendment, requires the owner's approval)
The existing criteria count completed trades (C1 needs 300), which does not fit a strategy that holds for weeks. The amendment would:
1. Define the candidate (section 3) with its parameter hash, universe, periods and costs.
2. Replace per-trade criteria by daily-return criteria, with the same bootstrap and confidence level (98.3%, one-sided, tighter if
   more candidates are promoted): the lower bound of mean daily net return above 0; net annualised return at least 4% after all
   costs on the capital used; maximum drawdown at most 10%; at least 60% of complete 90-day windows profitable; positive net
   return with costs doubled and with funding in the worst quarter of the history applied to the whole period.
3. Keep the same period discipline: development 2021-11-06 to 2024-05-07 only, validation once, confirmation (2019-09-08 to
   2021-11-05) only after approval, signing and a freeze. One variant, counted in the register.
4. Keep the same stopping rule: if it fails, it is rejected. No tuning of the 0.010%, 0.005%, 21 or 2% settings.

## 6. Engineering needed if it passes (not started)
Spot order support and a spot wallet view in the exchange adapter; a two-leg order protocol that handles one leg failing (close the other
at once, never hold a naked leg); margin and basis monitoring; funding-payment accounting; paper-mode ledger for two legs; a decision on the
transfer permission (or keeping fixed balances in both wallets and no automatic transfers, which is safer); and new tests for each.
That is a substantial project, which is why section 4 and 5 come first and cost almost nothing.

## 7. Alternatives considered and set aside
More directional variants on the same pairs (already rejected, and more tries mean more false positives); mean reversion and pairs
trading (costs and capacity concerns, no stronger prior than the strategies that failed); machine-learned prediction (the strongest risk
of fitting noise, hardest to audit); grid trading (loses in trends, which is the market regime that mattered most here).
