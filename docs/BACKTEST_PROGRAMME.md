# Backtest programme

Run these on the VPS: they need Binance public market data, which the build sandbox cannot reach. Nothing here places
an order or uses API keys. Run from the project directory with the existing virtual environment active.

Rules that apply to every run
* Signals use closed candles and fill at the next open. Warm-up (500 candles plus 25 days) is excluded from results.
* `--funding historical` downloads real funding rates and aborts if any are missing. `--funding approx` is a constant
  scenario and must be labelled as such. Neither fills gaps with invented values.
* A symbol that fails to download aborts the run unless `--allow-partial` is given, and then the report says so.
* Do not tune parameters to the results. Strategy B's parameters are fixed in advance.
* Test 1 uses the 11 pairs that existed for the whole window. They are the survivors that are still large today, so
  results carry **survivorship bias** (pairs that were delisted or collapsed are absent). Say so wherever you quote them.

## Test 1: 11 pairs, 6 Nov 2021 to 6 Nov 2026
```bash
python -m g_aegisbot.backtest --start 2021-11-06 --end 2026-11-06 --compare --split 0.5,0.25,0.25 --windows 5 \
  --symbols BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT BNB/USDT:USDT XRP/USDT:USDT LINK/USDT:USDT ADA/USDT:USDT \
  AVAX/USDT:USDT NEAR/USDT:USDT DOGE/USDT:USDT DOT/USDT:USDT --out backtests/test1
```
`--compare` writes breakout alone, trend alone and both combined on identical data. `--split` adds chronological
development, validation and out-of-sample reports. The out-of-sample slice is read once, last.

## Test 2: all 15 live pairs, 21 Aug 2024 to 6 Nov 2026
```bash
python -m g_aegisbot.backtest --start 2024-08-21 --end 2026-11-06 --compare --split 0.5,0.25,0.25 --out backtests/test2
```

## Test 3: SUI, FET, 1000PEPE, RENDER individually
```bash
python -m g_aegisbot.backtest --individually --from-listing --end 2026-11-06 --compare \
  --symbols SUI/USDT:USDT FET/USDT:USDT 1000PEPE/USDT:USDT RENDER/USDT:USDT --out backtests/test3
```
Each pair starts 25 days after its own listing and needs 500 candles of its own before its first signal.

## Test 4: slow-timeframe study (4h and 1d), development period only
Pre-registered in `docs/STRATEGY_ACCEPTANCE.md` section 6.5. Run from the tag `prereg-slow-timeframe-v1` (`git fetch --tags &&
git checkout prereg-slow-timeframe-v1`, then check `git rev-parse HEAD`). One run per candidate, no re-runs except after a crash.
```bash
SYMS="BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT BNB/USDT:USDT XRP/USDT:USDT LINK/USDT:USDT ADA/USDT:USDT AVAX/USDT:USDT NEAR/USDT:USDT DOGE/USDT:USDT DOT/USDT:USDT"
for tf in 4h 1d; do for s in breakout trend; do
  python -m g_aegisbot.backtest --start 2021-11-06 --end 2024-05-07 --timeframe $tf --strategies $s --funding historical \
    --allow-late-start --out backtests/slow/$tf-$s --symbols $SYMS
done; done
python -m g_aegisbot.backtest.devscreen --candidate S-B4 --run <folder holding summary.json of the 4h breakout run>
```
Only candidates whose base screen says `STRESS_RUN_NEEDED` get a stress run (`--slippage-scale 2.0 --fee-scale 1.2`, same other
options) and then `devscreen ... --stress-run <that folder>`. Do not look at any other period.

## Stress and sanity runs
```bash
python -m g_aegisbot.backtest --start 2024-08-21 --slippage-scale 2.0 --out backtests/stress
python -m g_aegisbot.backtest --start 2024-08-21 --liq-brackets brackets.json --out backtests/liquidation
python -m g_aegisbot.backtest --synthetic --years 1   # pipeline check only; the numbers mean nothing
```
`brackets.json` holds Binance's maintenance-margin tiers per symbol (see `g_aegisbot/backtest/liquidation.py`). Without
it a flat 0.5% maintenance rate is used and the report states that.

## What changed since the baseline below (so old and new numbers are not mixed up)
Strict strategy priority, the EMA50 trend entry filter, time-based closed-candle handling and the entry price guard
(`max_entry_chase_atr`, entries skipped when the open ran away from the signal close) all alter results. Any run made before
those changes is not comparable with a run made after them. Combined runs now also report per-strategy equity, drawdown,
Sharpe and sortino in `by_strategy` (`summary.json`); the strategies' attributed equity adds up exactly to the portfolio.

## What each run writes
`summary.json` (metrics, per-symbol, per-strategy, per-exit-reason, assumptions), `trades.csv`, `equity.csv`, and `manifest.json`
(code hash, library versions, full parameters and configuration, per-symbol data and funding hashes, the acceptance-protocol
hash, and the hashes of the three output files; `experiment_id` is identical for identical experiments). Judge results only
against `docs/STRATEGY_ACCEPTANCE.md`. Do not use `--split` for decision runs until the holdout lock-box lands. Read the
assumptions block before the numbers. Compare the gross and net columns: if gross is positive and net negative, costs
are the problem; if both are negative, the signal has no edge.

## Baseline that must stay visible
The original breakout-only run (11 pairs, 1h, 6 Nov 2021 to 6 Nov 2026, $10,000, 3x) ended at $835.22 (-91.65%),
max drawdown -92.98%, 4,695 trades, win rate 33.53%, profit factor 0.789, fees $4,315.44, every symbol negative. It was
produced with the earlier simulator, so re-run it with the current one and report both.
