"""Reconciling an entry order whose outcome is unknown, from exchange evidence.

Policy (Phase 1.2). An unresolved entry is released AUTOMATICALLY only when the exchange itself shows a terminal outcome
for that exact order:

  filled    the order is terminal with a fill. Released once the resulting position is flat or confirmed protected by a
            valid full-size stop (the caller checks that).
  unfilled  the order is terminal (canceled, expired, rejected) with zero cumulative fill, AND the position is flat, AND
            the account trade history is complete and shows no fill for the order or anywhere in the request window.

Everything else keeps the entry blocked. In particular none of these releases anything, alone or together:
  * elapsed time;
  * "no such order" answers, however many;
  * an empty open-orders listing;
  * an error, timeout, unavailable endpoint, incomplete page set or inconsistent answer (an error is never an empty list).

"No such order" with every other source complete and clean is reported as `not_seen`. That is NOT a terminal outcome: the
data cannot prove the order was never accepted. Only an operator can accept it (admin resolve-entry), with the evidence
shown, an explicit confirmation and a reason, and it is audited. Evidence that is incomplete or contradictory is refused
even then.

Nothing here claims more about Binance's data consistency than the answers it actually returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from g_aegisbot.engine.protection import assess_stops
from g_aegisbot.engine.strategy import classify_entry
from g_aegisbot.exchange.binance_futures import EvidenceIncomplete, PositionDataError, PositionModeError, parse_position_qty

log = logging.getLogger("aegis.reconcile")

WINDOW_LIMIT = timedelta(days=7)        # the order-history and trade-history endpoints serve at most 7 days per query
DEFAULT_MARGIN = timedelta(seconds=60)  # look this far before the recorded send time (clock differences)


@dataclass
class Evidence:
    symbol: str
    client_id: str
    since_ms: int
    until_ms: int
    ledger: bool = False                       # a paper ledger: the database IS the exchange
    create_rejected: bool = False              # the create call itself came back as an explicit exchange rejection
    mode_state: str = "unsupported"            # oneway | hedge | error | unsupported (the account's position mode)
    lookup_state: str = "unsupported"          # found | not_found | error | unsupported
    order: dict | None = None
    history_state: str = "unsupported"         # complete | incomplete | error | unsupported
    history_orders: int = 0
    history_match: dict | None = None
    trades_state: str = "unsupported"          # complete | incomplete | error | unsupported
    trades: list = field(default_factory=list)
    trades_for_order: int = 0
    open_state: str = "unsupported"            # complete | error | unsupported
    open_count: int = 0
    open_match: bool = False
    conditional_state: str = "unsupported"
    conditional_count: int = 0
    conditional_match: bool = False
    position_state: str = "error"              # ok | error
    position_qty: float = 0.0
    position_side: str | None = None
    position_modes: list = field(default_factory=list)      # raw positionSide values seen (BOTH, LONG, SHORT)
    stops_state: str = "unsupported"           # ok | error | unsupported
    stops_valid: bool = False
    stops_count: int = 0
    errors: dict = field(default_factory=dict)

    @property
    def hedge(self) -> bool:
        return any(str(m).upper() in ("LONG", "SHORT") for m in self.position_modes)

    @property
    def flat(self) -> bool:
        return self.position_state == "ok" and self.position_qty == 0

    def summary(self) -> str:
        status = (self.order or {}).get("status")
        look = f"{self.lookup_state}({status})" if self.lookup_state == "found" else self.lookup_state
        pos = ("flat" if self.flat else f"{self.position_qty:g}@{self.position_side}") if self.position_state == "ok" else "unread"
        return (f"order={look} history={self.history_state}/{self.history_orders} trades={self.trades_state}/{len(self.trades)} "
                f"open={self.open_state}/{self.open_count} conditional={self.conditional_state}/{self.conditional_count} "
                f"mode={self.mode_state} position={pos} stops={self.stops_state}/{self.stops_count} sha={self.digest()}")

    def digest(self) -> str:
        """Short fingerprint of what the exchange said, so an audit row can be matched to the evidence shown."""
        basis = {k: getattr(self, k) for k in (
            "symbol", "client_id", "lookup_state", "history_state", "history_orders", "trades_state", "trades_for_order",
            "open_state", "open_count", "conditional_state", "conditional_count", "position_state", "position_qty",
            "position_side", "stops_state", "stops_count", "mode_state", "create_rejected", "stops_valid")}
        basis["modes"] = sorted(map(str, self.position_modes))
        basis["order"] = {k: (self.order or {}).get(k) for k in ("id", "status", "filled", "amount")}
        basis["trades"] = sorted(str(t.get("id")) for t in self.trades)
        return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def lines(self) -> list[str]:
        """Human-readable evidence for the operator."""
        o = self.order or {}
        out = [f"symbol {self.symbol}   client order id {self.client_id}",
               f"window {datetime.fromtimestamp(self.since_ms / 1000):%Y-%m-%d %H:%M:%S} UTC "
               f"to {datetime.fromtimestamp(self.until_ms / 1000):%Y-%m-%d %H:%M:%S} UTC" + ("   [paper ledger]" if self.ledger else "")]
        if self.lookup_state == "found":
            out.append(f"order lookup:   FOUND id={o.get('id')} status={o.get('status')} filled={o.get('filled')} of {o.get('amount')}")
        else:
            out.append(f"order lookup:   {self.lookup_state.upper()}" + (f" ({self.errors['lookup']})" if "lookup" in self.errors else ""))
        out.append(f"order history:  {self.history_state.upper()} ({self.history_orders} orders in window; "
                   f"{'contains' if self.history_match else 'does not contain'} this id)"
                   + (f" ({self.errors['history']})" if "history" in self.errors else ""))
        out.append(f"account trades: {self.trades_state.upper()} ({len(self.trades)} in window, {self.trades_for_order} for this order)"
                   + (f" ({self.errors['trades']})" if "trades" in self.errors else ""))
        out.append(f"open orders:    {self.open_state.upper()} ({self.open_count}; {'MATCH' if self.open_match else 'no match'})"
                   + (f" ({self.errors['open']})" if "open" in self.errors else ""))
        out.append(f"conditional:    {self.conditional_state.upper()} ({self.conditional_count}; {'MATCH' if self.conditional_match else 'no match'})"
                   + (f" ({self.errors['conditional']})" if "conditional" in self.errors else ""))
        if self.position_state == "ok":
            out.append("position:       " + ("FLAT" if self.flat else f"{self.position_qty:g} {self.position_side} "
                                             f"(position mode {','.join(map(str, self.position_modes)) or 'n/a'})"))
        else:
            out.append(f"position:       UNREAD ({self.errors.get('position', '')})")
        out.append(f"position mode:  {self.mode_state.upper()}" + (f" ({self.errors['mode']})" if "mode" in self.errors else ""))
        out.append(f"protective:     {self.stops_state.upper()} ({self.stops_count} stop orders; "
                   f"{'a valid full-size stop exists' if self.stops_valid else 'no valid full-size stop'})")
        out.append(f"evidence digest {self.digest()}")
        return out


@dataclass
class Decision:
    outcome: str            # filled | unfilled | not_seen | blocked
    reasons: list[str]

    @property
    def auto(self) -> bool:
        """May the runner release this on its own? Only a terminal outcome of the order itself."""
        return self.outcome in ("filled", "unfilled")


class OrderIdentityError(Exception):
    pass


def _check_identity(order, symbol: str, client_id: str) -> None:
    """The order handed back for a client-id lookup must BE that order. Whatever the adapter did, check again here: a
    mismatching answer is an error and can never support a release."""
    if not isinstance(order, dict):
        raise OrderIdentityError("malformed")
    info = order.get("info") or {}
    ids = [v for v in (order.get("clientOrderId"), info.get("clientOrderId"), info.get("clientAlgoId")) if v]
    if not ids and order:
        raise OrderIdentityError("no client order id")
    if any(str(v) != client_id for v in ids):
        raise OrderIdentityError("client order id mismatch")
    if order.get("symbol") != symbol:                      # a missing symbol is as unproven as a wrong one
        raise OrderIdentityError("symbol mismatch")


def _err(exc: Exception) -> str:
    return type(exc).__name__          # the type only: never a message that could echo a request, key or account detail


async def gather_evidence(adapter, symbol: str, client_id: str, submitted_at: datetime, now: datetime,
                          margin: timedelta = DEFAULT_MARGIN, create_rejected: bool = False) -> Evidence:
    """Ask every available source about this order. Never raises: each failure is recorded as a state, and a failed or
    incomplete read is never turned into an empty result."""
    since = submitted_at - margin
    until = now + margin
    ev = Evidence(symbol=symbol, client_id=client_id, since_ms=int(since.timestamp() * 1000),
                  until_ms=int(until.timestamp() * 1000), ledger=bool(getattr(adapter, "ledger", False)),
                  create_rejected=create_rejected)

    # --- the order itself, by its client id
    lookup = getattr(adapter, "fetch_order_by_client_id", None)
    if lookup is not None:
        try:
            order = await lookup(symbol, client_id)
            if order is not None:
                _check_identity(order, symbol, client_id)
            ev.order, ev.lookup_state = order, ("not_found" if order is None else "found")
        except Exception as exc:
            ev.lookup_state, ev.errors["lookup"] = "error", _err(exc)

    # --- order history and trade history over the window (paged; an incomplete page set is not an answer)
    for attr, method, state_attr in (("history", "fetch_orders_history", "history_state"),
                                     ("trades", "fetch_account_trades", "trades_state")):
        fn = getattr(adapter, method, None)
        if fn is None:
            continue
        try:
            rows = await fn(symbol, ev.since_ms, ev.until_ms)
            setattr(ev, state_attr, "complete")
        except EvidenceIncomplete as exc:
            setattr(ev, state_attr, "incomplete")
            ev.errors[attr] = str(exc)[:120]
            continue
        except Exception as exc:
            setattr(ev, state_attr, "error")
            ev.errors[attr] = _err(exc)
            continue
        if attr == "history":
            ev.history_orders = len(rows)
            ev.history_match = next((o for o in rows if o.get("clientOrderId") == client_id), None)
        else:
            ev.trades = list(rows)

    # --- outstanding regular and conditional orders
    for attr, method, state_attr, count_attr, match_attr in (
            ("open", "fetch_open_orders", "open_state", "open_count", "open_match"),
            ("conditional", "fetch_conditional_orders", "conditional_state", "conditional_count", "conditional_match")):
        fn = getattr(adapter, method, None)
        if fn is None:
            continue
        try:
            rows = await fn(symbol)
            setattr(ev, state_attr, "complete")
            setattr(ev, count_attr, len(rows))
            setattr(ev, match_attr, any(o.get("clientOrderId") == client_id for o in rows))
        except Exception as exc:
            setattr(ev, state_attr, "error")
            ev.errors[attr] = _err(exc)

    # --- the account's position mode, the same check the runner makes when it builds its adapter
    mode_fn = getattr(adapter, "assert_one_way_mode", None)
    if mode_fn is not None:
        try:
            await mode_fn()
            ev.mode_state = "oneway"
        except PositionModeError:
            ev.mode_state = "hedge"
        except Exception as exc:
            ev.mode_state, ev.errors["mode"] = "error", _err(exc)

    # --- the position (every mode value seen is kept, so hedge mode cannot hide). A malformed entry is an error, never flat.
    try:
        qty, side = 0.0, None
        for p in await adapter.fetch_open_positions([symbol]):
            size = parse_position_qty(p)                  # raises PositionDataError on None / NaN / inconsistent data
            if p.get("symbol") != symbol or size == 0:
                continue
            qty += size
            side = p.get("side")
            mode = (p.get("info") or {}).get("positionSide") or p.get("positionSide")
            if mode:
                ev.position_modes.append(mode)
        ev.position_state, ev.position_qty, ev.position_side = "ok", qty, side
    except Exception as exc:
        ev.position_state, ev.errors["position"] = "error", _err(exc)

    # --- protective orders for whatever position exists
    stops_fn = getattr(adapter, "fetch_stop_orders", None)
    if stops_fn is not None:
        try:
            stops = await stops_fn(symbol)
            ev.stops_state, ev.stops_count = "ok", len(stops)
            if ev.position_state == "ok" and ev.position_qty > 0 and ev.position_side in ("long", "short"):
                ev.stops_valid = assess_stops(stops, position_side=ev.position_side,
                                              position_qty=ev.position_qty).best is not None
        except Exception as exc:
            ev.stops_state, ev.errors["stops"] = "error", _err(exc)

    # --- attribute trades to the order when its exchange id is known
    oid = str((ev.order or ev.history_match or {}).get("id") or "")
    if oid:
        ev.trades_for_order = sum(1 for t in ev.trades if str(t.get("order") or (t.get("info") or {}).get("orderId")) == oid)
    return ev


def decide(ev: Evidence) -> Decision:
    """What the evidence allows. See the module docstring for the policy."""
    if ev.symbol in ("", "?") or not ev.client_id:
        return Decision("blocked", ["the journal record is damaged (symbol or client order id missing)"])
    if ev.position_state != "ok":
        return Decision("blocked", [f"the exchange position could not be read ({ev.errors.get('position', 'error')})"])
    if ev.hedge:
        return Decision("blocked", ["a hedge-mode position (LONG/SHORT) exists; the bot only supports one-way mode"])
    if not ev.ledger:
        if ev.mode_state != "oneway":
            why = {"hedge": "the account is in hedge mode; the bot only supports one-way mode",
                   "error": f"the account's position mode could not be read ({ev.errors.get('mode', 'error')})"}.get(
                ev.mode_state, "the account's position mode cannot be confirmed as one-way")
            return Decision("blocked", [why])
        if ev.position_qty > 0 and not ev.position_modes:
            return Decision("blocked", ["a position exists but its position mode is not reported, so the mode is ambiguous"])

    if ev.ledger:                                   # paper: positions live in our own database, fills are atomic
        if ev.position_qty > 0:
            return Decision("filled", ["a ledger position exists for this symbol"])
        return Decision("not_seen", ["paper ledger shows no position; only an operator may accept that"])

    if ev.lookup_state in ("unsupported", "error"):
        why = "this adapter cannot look orders up by client id" if ev.lookup_state == "unsupported" \
            else f"the order lookup failed ({ev.errors.get('lookup')})"
        return Decision("blocked", [why])

    if ev.lookup_state == "found":
        kind = classify_entry(ev.order)[0]
        if kind == "pending":
            return Decision("blocked", [f"the order is not terminal (status {(ev.order or {}).get('status')!r})"])
        if kind in ("filled", "partial_closed"):
            return Decision("filled", [f"order is terminal with a fill ({kind})"])
        reasons = []                                 # kind == "unfilled": prove it, do not assume it
        if not ev.flat:
            reasons.append(f"the order shows nothing filled but a position of {ev.position_qty:g} exists")
        if ev.trades_state != "complete":
            reasons.append(f"trade history is {ev.trades_state}" + (f" ({ev.errors['trades']})" if "trades" in ev.errors else ""))
        elif ev.trades_for_order or ev.trades:
            reasons.append(f"trade history shows {len(ev.trades)} fill(s) in the window, {ev.trades_for_order} for this order, "
                           "yet the order reports no fill")
        return Decision("blocked", reasons) if reasons else Decision("unfilled", ["terminal with zero fill, flat, no trades"])

    # lookup_state == "not_found": cannot establish a terminal outcome; at most "all sources clean"
    reasons = []
    if ev.history_state != "complete":
        reasons.append(f"order history is {ev.history_state}" + (f" ({ev.errors['history']})" if "history" in ev.errors else ""))
    elif ev.history_match is not None:
        reasons.append("order history contains this id although the lookup said not found (inconsistent)")
    if ev.trades_state != "complete":
        reasons.append(f"trade history is {ev.trades_state}" + (f" ({ev.errors['trades']})" if "trades" in ev.errors else ""))
    elif ev.trades:
        reasons.append(f"{len(ev.trades)} fill(s) on this symbol in the window that no known order explains")
    if ev.open_state != "complete":
        reasons.append(f"the open-orders listing is {ev.open_state}")
    elif ev.open_match:
        reasons.append("the order is listed as open")
    if ev.conditional_state != "complete":
        reasons.append(f"the conditional-orders listing is {ev.conditional_state}")
    elif ev.conditional_match:
        reasons.append("the order is listed as a conditional order")
    if not ev.flat:
        reasons.append(f"a position of {ev.position_qty:g} exists")
    if reasons:
        return Decision("blocked", reasons)
    if ev.create_rejected:
        return Decision("unfilled", ["the exchange answered the create request with an explicit rejection AND every source "
                                     "(lookup, order history, trades, open and conditional orders, position) shows nothing"])
    return Decision("not_seen", ["the exchange has no record of this order in any source it can be asked; that is not "
                                 "proof it was never accepted, so only an operator can accept it"])
