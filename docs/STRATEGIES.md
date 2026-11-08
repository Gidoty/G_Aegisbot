# Strategies

Two independent strategies share one indicator frame of CLOSED candles, one sizing function and one risk layer.
The live scanner and the backtester call the same `evaluate_symbol`, so a rule exists exactly once.
Enable them with `AEGIS_STRATEGIES` (live; default `breakout`) or `--strategies` (backtest). Parameters below are
fixed in advance in `StrategyParams` and were not tuned on any result.

## Strategy A: breakout (unchanged)
Long: close above the prior 20-candle high, close above EMA50 and EMA200, EMA50 above EMA200, RSI 50 to 70,
volume at least 1.5 times its 20-candle average. Short is the mirror (RSI 30 to 50). Initial stop 2 ATR, trailing stop
2 ATR (chandelier). A setup is high conviction when at least 3 of 4 marks hold (volume 2.5 times or more, close 0.5 ATR
beyond the level, EMA50 rising with a 2% gap to EMA200, RSI in the 55 to 65 band for longs or 35 to 45 for shorts); such
setups are sized 12 to 15 percent of allocated margin, normal ones 5 to 7 percent. No discretionary exit: stop and
trailing stop only.

## Strategy B: trend-following pullback
Evaluated on the close of candle `i`. Long (short is the exact mirror):

| Clause | Rule |
|---|---|
| trend | EMA50 above EMA200, close above EMA200, EMA50 higher than 24 candles ago |
| ema50 | the signal candle closes strictly above EMA50 (short: strictly below). A close equal to EMA50 does not qualify |
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

**EMA50 entry confirmation.** The `ema50` clause is judged on the signal candle itself, the candle whose close triggers the
entry, never on the candle after it (the order fills at that next candle's open, and what happens there cannot cancel the
signal). The exit uses the same line from the other side: a close strictly below EMA50 (long) or strictly above (short)
exits, so a close exactly on EMA50 neither enters nor exits.

## Strategy priority, conflicts, one position per symbol
`StrategyParams.strategy_priority` (default `("breakout", "trend")`) is the primary ranking criterion. One function,
`strategy.signal_rank_key`, ranks competing signals for the per-symbol choice, the live scanner, the live runner and the
backtester, and the priority is always passed to it explicitly (it has no default, so no path can ignore the setting).

Ranking order:
1. **Strategy priority.** Every signal of a strategy listed earlier outranks every signal of a strategy listed later, on
   any symbol, whatever its conviction or volume. Breakout outranks trend whenever both qualify; trend is chosen when
   breakout has no eligible signal. Reverse the tuple and the outcome reverses. A strategy missing from the tuple ranks
   after all listed ones.
2. **Conviction**, within the same strategy: high before normal. (Trend has no high tier, so all its signals tie here.)
3. **Volume ratio**, within the same strategy: the larger first.
4. Symbol, side and strategy name, so the same data always gives the same order.

Rules around the ranking:
* No strategy signals: no trade, per-strategy reasons are kept.
* Opposite sides on the same symbol and candle: **no trade**, recorded as a conflict. Priority never breaks this tie.
* Same side on the same symbol and candle: exactly one signal is kept (the higher-ranked); the other is recorded as a duplicate.
* **One position per symbol.** A symbol that already has a position never receives a second one, from either strategy.
  Strict priority decides which signal is taken first, not how many positions a symbol may hold.
* Across symbols the ranking decides who gets the limited slots (4 positions, 3 per side, aggregate risk, gross notional),
  which are shared by both strategies.
* Every decision is a `SignalDecision(symbol, strategy, side, kept, reason)` that can be logged and counted.
* Positions the bot finds on the exchange without a record are tagged `adopted`; they get a stop and are managed, but
  belong to neither strategy.

## Closed candles in the live scan
A candle counts as closed once its time is over (`open + timeframe <= now`), not because it is not the last row, so the
scan works whether or not the exchange has already listed the new forming candle. If the newest closed candle is not the
one that just ended, the symbol is rejected as `stale candle data` and nothing is evaluated: a signal on an old bar is
never traded as if it were current. This relies on the server clock being accurate (use NTP).

## Tags
`strategy` is stored on open trades, closed trades and paper positions and appears in the API, dashboard and backtest
reports. `trail_atr_mult` is stored per open trade, so a restart keeps each position on its own trailing distance.
