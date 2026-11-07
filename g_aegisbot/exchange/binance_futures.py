"""Async CCXT adapter for Binance USDT-margined futures (mainnet or testnet)."""
from __future__ import annotations

import logging
import math

import ccxt.async_support as ccxt
import pandas as pd

from g_aegisbot.config import MAX_LEVERAGE, MIN_LEVERAGE

log = logging.getLogger("aegis.binance")

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class EvidenceIncomplete(Exception):
    """A history query could not be answered completely (window too long, too many pages, a page that did not advance).
    The caller must treat the evidence as missing, never as an empty result."""


EVIDENCE_PAGE = 1000
EVIDENCE_MAX_PAGES = 20
EVIDENCE_MAX_WINDOW_MS = 7 * 24 * 3600 * 1000 - 3600 * 1000   # the history endpoints serve at most 7 days per query


class OrderDataError(Exception):
    """An order payload is malformed or ambiguous (missing raw fields, contradictory fields, unknown status). It is never
    guessed at: the caller treats the exchange state as unreadable."""


class OrderIdentityMismatch(OrderDataError):
    """The exchange returned an order that is not the one that was asked for (client id or symbol differs)."""


class PositionDataError(Exception):
    """A position payload is malformed or inconsistent. It must never be read as 'flat'."""


class CancelNotConfirmed(Exception):
    """A cancel was attempted but the order listings still show the order, or could not be read. The order is not known
    to be gone."""


CONDITIONAL_TYPES = {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
TAKE_PROFIT_TYPES = {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}
ACTIVE_STATUS = {"NEW"}
INACTIVE_STATUS = {"TRIGGERING", "TRIGGERED", "FINISHED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FILLED",
                   "EXPIRED_IN_MATCH"}


def _finite(value, what: str, exc=OrderDataError) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise exc(f"{what} is missing or not a number")
    if not math.isfinite(x):
        raise exc(f"{what} is not finite")
    return x


def _flag(value, what: str):
    """True / False for a boolean or 'true'/'false', None when absent, error for anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise OrderDataError(f"{what} is not a boolean ({value!r})")


def parse_position_qty(p: dict, *, require_raw: bool = False) -> float:
    """Absolute size of one ccxt position, or PositionDataError.

    Anything malformed is an error, never zero: a missing, None, NaN, infinite, negative or non-numeric `contracts`; a raw
    `info.positionAmt` that is not finite or disagrees with `contracts`; a non-zero size without a long/short side; and,
    in one-way mode (positionSide BOTH), a side that contradicts the sign of positionAmt."""
    if not isinstance(p, dict):
        raise PositionDataError("a position entry is not an object")
    qty = _finite(p.get("contracts"), "position quantity", PositionDataError)
    if qty < 0:
        raise PositionDataError("position quantity is negative")
    info = p.get("info") or {}
    amt = None
    if require_raw and info.get("positionAmt") is None:
        # without the raw amount ccxt derives a quantity from notional and price: plausible, imprecise, never to be trusted
        raise PositionDataError("the raw positionAmt is missing, so the normalised quantity cannot be cross-checked")
    if info.get("positionAmt") is not None:
        amt = _finite(info["positionAmt"], "raw positionAmt", PositionDataError)
        factor = _finite(p["contractSize"], "contract size", PositionDataError) if p.get("contractSize") else 1.0
        if abs(abs(amt) - qty * factor) > 1e-9 * max(1.0, abs(amt)):
            raise PositionDataError(f"normalised quantity {qty:g} disagrees with the raw positionAmt {amt:g}")
    if qty != 0:
        if not isinstance(p.get("symbol"), str) or not p["symbol"]:
            raise PositionDataError("a non-zero position has no symbol")
        if p.get("side") not in ("long", "short"):
            raise PositionDataError("a non-zero position has no long/short side")
        if amt is not None and str(info.get("positionSide") or "").upper() == "BOTH" and amt != 0:
            if (amt > 0) != (p["side"] == "long"):
                raise PositionDataError("the position side contradicts the sign of the raw positionAmt")
    return qty


class PositionModeError(Exception):
    """The Binance account is in hedge mode; the bot only supports one-way mode."""


class PositionModeUnreadable(Exception):
    """The exchange's answer about the position mode is missing or malformed, so one-way mode is NOT confirmed."""


class BinanceFuturesAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        testnet: bool = True,
        recv_window_ms: int = 10_000,
    ):
        self.testnet = testnet
        self.exchange = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                    "recvWindow": recv_window_ms,
                },
            }
        )
        if testnet:
            self.exchange.set_sandbox_mode(True)

    @classmethod
    def from_profile(cls, profile, api_key: str, api_secret: str) -> "BinanceFuturesAdapter":
        """Build an adapter for a UserProfile. Only 'live' (production) and 'testnet' (sandbox) are accepted; any other
        value raises instead of falling through to one of them."""
        mode = profile.trading_mode
        if mode == "live":
            return cls(api_key, api_secret, testnet=False)
        if mode == "testnet":
            return cls(api_key, api_secret, testnet=True)
        raise ValueError(f"unknown trading mode {mode!r}: expected 'live' or 'testnet'")

    async def __aenter__(self) -> "BinanceFuturesAdapter":
        await self.exchange.load_markets()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self.exchange.close()

    # ---- market data ----
    async def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Return the symbols that are NOT tradable linear perpetuals on this endpoint."""
        if not self.exchange.markets:
            await self.exchange.load_markets()
        markets = self.exchange.markets
        return [
            s for s in symbols
            if s not in markets or not markets[s].get("swap") or not markets[s].get("linear")
        ]

    async def fetch_ohlcv_df(
        self, symbol: str, timeframe: str = "15m", limit: int = 500, since: int | None = None
    ) -> pd.DataFrame:
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    # ---- account ----
    async def fetch_usdt_balance(self) -> dict[str, float]:
        bal = await self.exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        return {
            "total": float(usdt.get("total") or 0),
            "free": float(usdt.get("free") or 0),
            "used": float(usdt.get("used") or 0),
        }

    async def fetch_equity(self) -> float:
        """Margin balance = wallet balance + unrealized PnL, from Binance's account endpoint."""
        acct = await self.exchange.fapiPrivateV2GetAccount()
        return float(acct["totalMarginBalance"])

    async def fetch_last_price(self, symbol: str) -> float:
        return float((await self.exchange.fetch_ticker(symbol))["last"])

    async def fetch_open_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Non-flat positions. A malformed or inconsistent entry raises PositionDataError: it is never dropped, because a
        dropped entry reads as 'flat'."""
        try:
            positions = await self.exchange.fetch_positions(symbols)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:     # ccxt could not parse the payload
            raise PositionDataError(f"the positions response could not be parsed ({type(exc).__name__})") from exc
        if not isinstance(positions, list):
            raise PositionDataError("the positions response is not a list")
        return [p for p in positions if parse_position_qty(p, require_raw=True) != 0]

    # ---- account setup ----
    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        """Set isolated margin and leverage. Leverage is clamped to the platform range."""
        if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
            raise ValueError(f"leverage must be {MIN_LEVERAGE}-{MAX_LEVERAGE}x, got {leverage}")
        try:
            await self.exchange.set_margin_mode("isolated", symbol)
        except ccxt.ExchangeError as exc:
            if "No need to change margin type" not in str(exc):  # already isolated
                raise
        await self.exchange.set_leverage(leverage, symbol)

    # ---- orders ----
    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False,
                           client_id: str | None = None, strategy: str | None = None,
                           reason: str | None = None) -> dict:
        """`client_id` makes the order idempotent: the exchange refuses a second order with the same id."""
        params = {"reduceOnly": True} if reduce_only else {}
        if client_id:
            params["newClientOrderId"] = client_id
        return await self.exchange.create_order(symbol, "market", side, amount, None, params)

    def _market_id(self, symbol: str) -> str | None:
        try:
            return self.exchange.market(symbol)["id"]
        except Exception:
            return None

    async def fetch_order_by_client_id(self, symbol: str, client_id: str) -> dict | None:
        """The order sent with this client id, or None if the exchange answered that it has no such order. Network errors
        propagate. The returned order must be the one asked for: its client order id (normalised AND raw) and symbol must
        match exactly, otherwise OrderIdentityMismatch, which is an error and never supports a release."""
        try:
            order = await self.exchange.fetch_order(None, symbol, {"origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            return None
        self._check_identity(order, symbol, client_id)
        return order

    def _check_identity(self, order, symbol: str, client_id: str) -> None:
        if not isinstance(order, dict) or not order:
            raise OrderIdentityMismatch("the order lookup returned an empty or malformed answer")
        info = order.get("info") or {}
        ids = [v for v in (order.get("clientOrderId"), info.get("clientOrderId"), info.get("clientAlgoId")) if v]
        if not ids:
            raise OrderIdentityMismatch("the returned order carries no client order id")
        if any(str(v) != client_id for v in ids):
            raise OrderIdentityMismatch("the returned order has a different client order id than the one requested")
        if order.get("symbol") != symbol:
            raise OrderIdentityMismatch("the returned order is for a different symbol than the one requested")
        market_id = self._market_id(symbol)
        if info.get("symbol") and market_id and str(info["symbol"]) != market_id:
            raise OrderIdentityMismatch("the returned order's raw symbol differs from the one requested")

    async def stop_market(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        """Exchange-side protective stop. Survives a bot crash. Always reduce-only."""
        return await self.exchange.create_order(
            symbol, "STOP_MARKET", side, amount, None,
            {"stopPrice": stop_price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )

    def _stop_row(self, o: dict, symbol: str, route: str) -> dict | None:
        """One protective-stop candidate from a ccxt order, judged from the RAW exchange fields (ccxt's unified `type`
        turns STOP_MARKET into plain 'market' and cannot be used). Returns None for an order that is not an active
        protective stop (an ordinary market or limit order, a take-profit, a triggered or finished order, a conditional
        order that is not reduce-only). Raises OrderDataError for anything malformed or ambiguous."""
        info = o.get("info")
        if not isinstance(info, dict) or not info:
            raise OrderDataError("an order came back without its raw exchange fields")
        types = {str(info[k]).upper() for k in ("orderType", "origType", "type") if info.get(k)}
        if not types:
            raise OrderDataError("the raw order type is missing")
        conditional = types & CONDITIONAL_TYPES
        if route == "algo":
            if not conditional or types - CONDITIONAL_TYPES:
                raise OrderDataError(f"the conditional listing returned an unexpected order type {sorted(types)}")
        elif not conditional or types - CONDITIONAL_TYPES:
            return None               # LIMIT, MARKET, or a stop that has already turned into a market order
        if len(conditional) != 1:
            raise OrderDataError(f"ambiguous raw order type {sorted(types)}")
        kind = next(iter(conditional))
        if kind in TAKE_PROFIT_TYPES:
            return None               # reduce-only, but it never limits a loss
        status = str((info.get("algoStatus") if route == "algo" else info.get("status")) or "").upper()
        if not status:
            raise OrderDataError("the raw order status is missing")
        if status in INACTIVE_STATUS:
            return None
        if status not in ACTIVE_STATUS:
            raise OrderDataError(f"unknown raw order status {status!r}")
        side = str(info.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise OrderDataError("the raw order side is missing or unknown")
        if str(o.get("side") or "").upper() != side:
            raise OrderDataError("the normalised side contradicts the raw side")
        if o.get("symbol") != symbol:
            raise OrderDataError("an order for a different symbol appeared in this symbol's listing")
        market_id = self._market_id(symbol)
        if info.get("symbol") and market_id and str(info["symbol"]) != market_id:
            raise OrderDataError("the raw symbol differs from the one requested")
        position_side = str(info.get("positionSide") or "").upper()
        if position_side not in ("BOTH", "LONG", "SHORT"):
            raise OrderDataError("the raw position side is missing or unknown")
        reduce_only, close_position = _flag(info.get("reduceOnly"), "reduceOnly"), _flag(info.get("closePosition"), "closePosition")
        if reduce_only is None and close_position is None:
            raise OrderDataError("neither reduceOnly nor closePosition is present")
        if not (reduce_only or close_position):
            return None               # a conditional order that can open a position is not protection
        trigger = _finite(info.get("triggerPrice") if route == "algo" else info.get("stopPrice"), "trigger price")
        if trigger <= 0:
            raise OrderDataError("the trigger price is not positive")
        qty = _finite(info.get("quantity") if route == "algo" else info.get("origQty"), "order quantity")
        if qty < 0 or (qty == 0 and not close_position):
            raise OrderDataError("the order quantity is not positive")
        oid = str(o.get("id") or "")
        if not oid:
            raise OrderDataError("an order has no id")
        return {"id": oid, "route": route, "stopPrice": trigger, "side": side.lower(), "amount": qty,
                "reduceOnly": bool(reduce_only), "closePosition": bool(close_position), "type": kind.lower(),
                "workingType": info.get("workingType"), "positionSide": position_side, "status": status,
                "symbol": symbol, "clientOrderId": o.get("clientOrderId") or info.get("clientOrderId") or info.get("clientAlgoId")}

    async def fetch_stop_orders(self, symbol: str) -> list[dict]:
        """Active reduce-only protective stops for a symbol, from BOTH the regular and the conditional (algo) listings.

        Rows carry `route` ('regular' or 'algo') because cancelling uses a different endpoint for each. Any listing
        error propagates and any malformed or ambiguous order raises OrderDataError: a failed or unreadable listing is
        never reported as 'no stops' (that would make the bot place another stop every minute)."""
        found: dict[tuple[str, str], dict] = {}
        for route, params in (("regular", {}), ("algo", {"trigger": True})):
            try:
                orders = await self.exchange.fetch_open_orders(symbol, params=params)
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:     # ccxt could not parse the payload
                raise OrderDataError(f"the {route} open-orders response could not be parsed ({type(exc).__name__})") from exc
            if not isinstance(orders, list):
                raise OrderDataError(f"the {route} open-orders response is not a list")
            for o in orders:
                row = self._stop_row(o, symbol, route)
                if row is not None:
                    found[(route, row["id"])] = row
        return list(found.values())

    async def fetch_open_orders(self, symbol: str) -> list[dict]:
        """Every outstanding order for a symbol (entries, exits, stops): [{'id', 'clientOrderId', 'type', 'side', 'status'}].
        Network errors propagate, so a caller never mistakes 'could not look' for 'nothing there'."""
        orders = await self.exchange.fetch_open_orders(symbol)
        return [{"id": str(o.get("id")), "clientOrderId": o.get("clientOrderId"), "type": o.get("type"),
                 "side": o.get("side"), "status": o.get("status"), "amount": o.get("amount"),
                 "filled": o.get("filled"), "reduceOnly": o.get("reduceOnly")} for o in orders]

    async def fetch_conditional_orders(self, symbol: str) -> list[dict]:
        """Outstanding conditional (trigger) orders such as protective stops. Any error propagates: a failed listing is
        never an empty one."""
        orders = await self.exchange.fetch_open_orders(symbol, params={"trigger": True})
        return [{"id": str(o.get("id")), "clientOrderId": o.get("clientOrderId"), "type": o.get("type"),
                 "side": o.get("side"), "status": o.get("status"), "amount": o.get("amount")} for o in orders]

    async def _paged_history(self, fetch, symbol: str, since_ms: int, until_ms: int) -> list[dict]:
        """Every row of an exchange history endpoint for [since, until], or EvidenceIncomplete. A full page means there
        may be more, so the next page starts after the newest row; running out of pages, a page that does not advance, or a
        window longer than the endpoint serves are all 'incomplete', not 'done'."""
        if until_ms - since_ms > EVIDENCE_MAX_WINDOW_MS:
            raise EvidenceIncomplete("the window is longer than the 7 days the history endpoints serve")
        rows: dict[str, dict] = {}
        cursor = since_ms
        for _ in range(EVIDENCE_MAX_PAGES):
            batch = await fetch(symbol, cursor, EVIDENCE_PAGE, {"until": until_ms})
            for r in batch:
                rows[str(r.get("id"))] = r
            if len(batch) < EVIDENCE_PAGE:
                return list(rows.values())
            newest = max(int(r.get("timestamp") or 0) for r in batch)
            if newest < cursor:
                raise EvidenceIncomplete("a history page did not advance")
            cursor = newest + 1
        raise EvidenceIncomplete(f"more than {EVIDENCE_MAX_PAGES} pages of history")

    async def fetch_orders_history(self, symbol: str, since_ms: int, until_ms: int) -> list[dict]:
        """All orders (any status) for the symbol in the window, via the all-orders endpoint."""
        rows = await self._paged_history(self.exchange.fetch_orders, symbol, since_ms, until_ms)
        return [{"id": str(o.get("id")), "clientOrderId": o.get("clientOrderId"), "status": o.get("status"),
                 "filled": o.get("filled"), "amount": o.get("amount"), "timestamp": o.get("timestamp")} for o in rows]

    async def fetch_account_trades(self, symbol: str, since_ms: int, until_ms: int) -> list[dict]:
        """All of the account's fills for the symbol in the window, via the user-trades endpoint."""
        rows = await self._paged_history(self.exchange.fetch_my_trades, symbol, since_ms, until_ms)
        return [{"id": str(t.get("id")), "order": str(t.get("order")) if t.get("order") is not None else None,
                 "side": t.get("side"), "amount": t.get("amount"), "timestamp": t.get("timestamp")} for t in rows]

    async def cancel_order_by_client_id(self, symbol: str, client_id: str) -> None:
        """Cancel the order that was sent with this client id. Already finished or unknown is not an error."""
        try:
            await self.exchange.cancel_order(None, symbol, {"origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            pass

    async def assert_one_way_mode(self) -> None:
        """Refuse hedge mode: reduce-only orders and our position tracking assume one-way mode.

        One-way mode must be CONFIRMED by an explicit `dualSidePosition` of false / "false". True / "true" is hedge mode
        (PositionModeError). A missing field, None, any other string, or any other type is PositionModeUnreadable: an
        unknown value is never read as one-way."""
        resp = await self.exchange.fapiPrivateGetPositionSideDual()
        flag = resp.get("dualSidePosition") if isinstance(resp, dict) else None
        if isinstance(flag, str):
            flag = {"true": True, "false": False}.get(flag.lower())      # anything else becomes None: not understood
        if not isinstance(flag, bool):
            raise PositionModeUnreadable("the exchange's position-mode answer is missing or not understood, so one-way "
                                         "mode is not confirmed")
        if flag:
            raise PositionModeError("This Binance account uses Hedge Mode. Switch it to One-way Mode "
                                    "(Futures > Preferences > Position Mode) with no open positions, then restart.")

    async def _open_ids(self, symbol: str, route: str) -> set[str]:
        rows = await self.exchange.fetch_open_orders(symbol, params={"trigger": True} if route == "algo" else {})
        if not isinstance(rows, list):
            raise OrderDataError(f"the {route} open-orders response is not a list")
        return {str(o.get("id")) for o in rows}

    async def cancel_order(self, symbol: str, order_id: str, route: str | None = None) -> None:
        """Cancel one order and PROVE it is gone. `route` is 'regular' or 'algo' (a stop row from fetch_stop_orders says
        which); without it the open-order listings decide, and an id found on both routes is ambiguous and refused.

        The cancel call's own answer proves nothing, and a not-found error is not success: afterwards the relevant
        listing is read again and the order must be absent. Still open, ambiguous, or unreadable raises
        CancelNotConfirmed (the caller must treat the account as unverified)."""
        order_id = str(order_id)
        try:
            if route is None:
                where = [r for r in ("regular", "algo") if order_id in await self._open_ids(symbol, r)]
                if len(where) > 1:
                    raise CancelNotConfirmed(f"order id {order_id} appears in both the regular and conditional listings")
                if not where:
                    return                    # the listings were read successfully and it is not there
                route = where[0]
            elif route not in ("regular", "algo"):
                raise CancelNotConfirmed(f"unknown order route {route!r}")
        except CancelNotConfirmed:
            raise
        except Exception as exc:
            raise CancelNotConfirmed(f"could not read the open-order listings ({type(exc).__name__})") from exc
        sent = "sent"
        try:
            await self.exchange.cancel_order(order_id, symbol, {"trigger": True} if route == "algo" else {})
        except ccxt.OrderNotFound:
            sent = "not found"
        except Exception as exc:
            sent = type(exc).__name__
        try:
            still_open = order_id in await self._open_ids(symbol, route)
        except Exception as exc:
            raise CancelNotConfirmed(f"cancel of {order_id} ({sent}) could not be verified: listing unreadable "
                                     f"({type(exc).__name__})") from exc
        if still_open:
            raise CancelNotConfirmed(f"order {order_id} is still open on the {route} route after the cancel ({sent})")

    async def cancel_all(self, symbol: str) -> None:
        """Cancel every regular AND conditional order on the symbol, then read both listings: any order left raises
        CancelNotConfirmed. The cancel calls' answers are not trusted."""
        for params in ({}, {"trigger": True}):
            try:
                await self.exchange.cancel_all_orders(symbol, params)
            except Exception as exc:
                log.warning("cancel-all (%s) on %s answered %s", "conditional" if params else "regular", symbol, type(exc).__name__)
        left = []
        for route in ("regular", "algo"):
            try:
                left += [f"{route}:{i}" for i in await self._open_ids(symbol, route)]
            except Exception as exc:
                raise CancelNotConfirmed(f"cancel-all on {symbol} could not be verified: {route} listing unreadable "
                                         f"({type(exc).__name__})") from exc
        if left:
            raise CancelNotConfirmed(f"orders still open on {symbol} after cancel-all: {', '.join(sorted(left))}")
