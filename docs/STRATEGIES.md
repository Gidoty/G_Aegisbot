# Strategies

Two independent strategies share one indicator frame of CLOSED candles, one sizing function and one risk layer.
The live scanner and the backtester call the same `evaluate_symbol`, so a rule exists exactly once.
Enable them with `AEGIS_STRATEGIES` (live; default `breakout`) or `--strategies` (backtest). Parameters below are
fixed in advance in `StrategyParams` and were not tuned on any result.

## Strategy A: breakout (unchanged)
Long: close above the prior 20-candle high, close above EMA50 and EMA200, EMA50 above EMA200, RSI 50 to 70,
volume at least 1.5 times its 20-candle average. Short is the mirror (RSI 30 to 50). Initial stop 2 ATR, trailing stop
2 ATR (chandelier). High-conviction setups (volume 2.5 times or more, plus the other marks in `strategy.py`) are sized
12 to 15 percent of allocated margin, normal ones 5 to 7 percent. No discretionary exit: stop and trailing stop only.

## Strategy B: trend-following pullback
Evaluated on the close of candle `i`. Long (short is the exact mirror):

| Clause | Rule |
|---|---|
| trend | EMA50 above EMA200, close above EMA200, EMA50 higher than 24 candles ago |
| touch | in candles `i-8..i-1` at least one low reached EMA20 |
| valid | in those 8 candles no close below EMA50 and no low below EMA200 |
| depth | 20-candle high minus the pullback low is at least 1 ATR |
| confirm | close above open, above EMA20, above the previous high, RSI above 50 |
| volume | at least 1.0 times the prior 20-candle average |

Initial stop 2 ATR, trailing stop 3 ATR (wider than A, because trend trades are meant to run). Sizing is 5 to 7
percent of allocated margin, scaled by two optional marks (volume 1.5 times or more, pullback depth 2 ATR or more).
There is no high-conviction tier. **Exit:** besides the stops, the position closes at the next executable price when
a candle closes back below EMA50 (long) or above EMA50 (short). Reason recorded: `strategy-exit`.
No clause reads the signal candle's future, and the pullback window excludes the confirmation candle.

## Conflicts, one position per symbol
* No strategy signals: no trade, per-strategy reasons are kept.
* Opposite sides on the same symbol and candle: no trade, recorded as a conflict.
* Same side: one signal is kept, ranked by conviction, then volume strength, then `strategy_priority`
  (breakout before trend), then symbol name. The other is recorded as a duplicate.
* A symbol that already has a position never receives a second one, from either strategy.
* Every decision is a `SignalDecision(symbol, strategy, side, kept, reason)` that can be logged and counted.
* Positions the bot finds on the exchange without a record are tagged `adopted`; they get a stop and are managed, but
  belong to neither strategy.

## Tags
`strategy` is stored on open trades, closed trades and paper positions and appears in the API, dashboard and backtest
reports. `trail_atr_mult` is stored per open trade, so a restart keeps each position on its own trailing distance.
