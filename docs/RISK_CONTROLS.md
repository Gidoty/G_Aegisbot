# Risk controls

All controls apply to the combined portfolio, whichever strategy opened the position. None were loosened for this
release. New controls are additive.

| Control | Value | Where |
|---|---|---|
| Risk per trade at the initial stop | 1.5% of allocated capital (size is cut to fit) | `compute_size` |
| Open positions | 4 | `compute_size` |
| Aggregate open risk (all stops, current) | 4% of allocated capital (new) | `portfolio_state` |
| Positions on one side | 3 (new) | `portfolio_state` |
| Gross open notional | 2x allocated capital (new) | `portfolio_state` |
| Margin headroom | free margin must cover margin plus 5% | `compute_size` |
| Leverage | fixed per profile, isolated margin, one-way mode | adapter |
| Daily drawdown breaker | 4% from the UTC-day start equity, pauses new entries until 00:00 UTC | `risk.py` |
| Kill switch | admin pause or flatten, global or per user | `controls.py` |
| Lapsed subscription | manage-only: stops and exits continue, no new entries | runner |
| Max stop distance | 15% of price | strategies |

## Fail-safe rules
* A position with no known stop counts as 3% of notional in the risk budget, never zero.
* **What counts as a stop.** Only a reduce-only (or close-position) STOP_MARKET on the closing side, triggering on the
  mark price, whose quantity covers the **whole** open position. A wrong-side, undersized, stop-limit, trailing,
  last-price-trigger or unknown-quantity order is not protection. Several valid stops: the tightest is kept. A stale or
  invalid stop is removed only **after** a valid replacement is visible on the exchange.
* **Every symbol is checked on its own.** A failure on one symbol never stops the others from being verified or repaired.
  All failures are reported together, and any unresolved failure marks the account **unverified**: no new entry opens
  until a later check passes. This is logged at CRITICAL.
* **Entry outcomes.** Only a terminal canceled, expired or rejected status with zero fill is "definitely unfilled". An
  open, new, partially filled or unknown status, a closed order that reports no fill, or an order the exchange cannot
  find after a timeout is **uncertain**. The entry is never resent. It is looked up by its client order id
  (`aeg-<strategy>-<symbol>-<unix time>-<8 random hex>`, never reused: the journal refuses a repeat), any remainder is cancelled by that id, and positions and open orders
  are re-read. Until reconciled, new entries are blocked and the rest of that cycle's signals are not processed.
* **When an unresolved entry order is released.** Only when the exchange shows a **terminal outcome for that exact
  order** (looked up by client order id):
  (1) terminal **with a fill**: released once the position is flat or confirmed protected by a valid full-size stop;
  (2) terminal (canceled, expired, rejected) **with zero fill**, a flat position, and a complete account-trade history
  with no fill for the order or anywhere in the request window.
  Nothing else releases it, alone or in any combination: not elapsed time, not any number of "no such order" answers,
  not an empty open-orders listing, and never an error, timeout, unavailable endpoint, incomplete page set or
  inconsistent answer (an error is never treated as an empty result). An order that is still working is cancelled by
  client id and must then be seen terminal. A hedge-mode position blocks. When every source is complete and clean but none
  shows the order ("not seen"), the data cannot prove it was never accepted, so only an operator can accept it
  (`resolve-entry`, see `docs/OPERATOR_RECOVERY.md`).
* **Restart and crash.** Every entry is written to a journal (the audit table, append-only, no schema change, nothing
  ever deletes or updates it) and committed **before** it can be sent; if that write fails nothing is sent. The
  resolution is a separate later row, so the original intent is never altered. A new process reads every open intent
  (there is no age limit; a damaged record blocks instead of being skipped) before it may open anything, re-reads all
  positions and stops, and applies the release rules above. A journal that cannot be read blocks entries and is retried.
  SQLite commits are `synchronous=FULL`, so a committed intent survives power loss.
* **Fills.** The stop is sized from the position the exchange reports (falling back to the order's reported fill only
  when the position cannot be read). A partly filled order gets a stop for the filled part immediately.
* **If the stop cannot be placed** the entry is closed with a reduce-only order, and the exchange position is re-read.
  Only a confirmed zero position counts as closed. Otherwise the failure is a protection failure: the account is
  unverified, nothing else opens that cycle, and the next checks keep trying to protect or close.
* **Exits keep their stop.** A strategy exit, flatten or kill switch sends ONE reduce-only market close while the existing
  stop stays on the exchange, then re-reads the position. Flat: the stops are cancelled and the cancellation is verified,
  and only then is the trade recorded as closed. Partly closed or not closed (including a timeout or an error, which is
  never retried blind): full stop coverage for what remains is verified or restored and the trade stays open. A stop
  that fires during the close is recognised by the position reading zero. A position that cannot be read changes
  nothing and blocks entries. `_on_closed` itself refuses to record a close while the exchange still shows a position.

## What is not covered
Exchange outages with no network path, a Binance account-level liquidation under cross margin (the bot uses isolated),
gaps through a stop (the stop-market fills at the next price), and bugs in code that has not yet run against a live
exchange. How the exchange labels an order (status names, `origClientOrderId` lookups, `workingType`) is based on ccxt's normal
mapping and has not been observed against Binance yet. See `docs/TESTNET_CHECKLIST.md`.

## Phase 1.3 corrections
* **Stops are recognised from the raw Binance fields** (`type`/`orderType`, `reduceOnly`/`closePosition`, `stopPrice`/`triggerPrice`,
  `positionSide`, `workingType`, status), from both the regular and the conditional (algo) listings. A market order is never a stop.
* **Cancels are route aware and proved.** A conditional stop is cancelled on the algo endpoint. After every cancel the listings are
  read again; an order that is still listed, or a listing that cannot be read, means the cancel is not confirmed, the account is
  unverified and entries stay blocked. "Not found" is not success.
* **A stop that was accepted is not protection until it is visible** on the exchange, with the right side, reduce-only and a
  quantity that covers the whole position. Otherwise the entry is treated as unprotected.
* **Positions that cannot be parsed** (missing, NaN, infinite, malformed, or inconsistent with the raw `positionAmt`) are
  unreadable, never flat. They block release of pending orders and new entries.
* **Failed entries use the same release policy as restarts.** A cancelled or rejected entry is released only when the order,
  trade history, open and conditional listings, position and position mode all agree.
* **Resolutions are bound** to the user, client id, original intent row and symbol, and are written at most once.
* **One runner per database.** A second runner exits with code 3 before touching anything.
* **`--dry-run` is read-only on the exchange**: it places and cancels nothing, moves no stops and writes no journal entries.
* **ccxt is pinned to 4.5.92**, the version the adapter was audited against.
