"""Entry execution: failed, filled, partial and uncertain orders; no duplicate orders; stops always cover what filled."""
import ccxt.async_support as ccxt
import pytest

from g_aegisbot.engine.strategy import (
    OrderNotFilled, OrderUncertain, ProtectionFailure, StrategyParams, TradePlan, classify_entry, execute_plan,
    new_client_order_id,
)

SYM = "BTC/USDT:USDT"


def plan(strategy="breakout", side="buy", amount=1.0):
    return TradePlan(symbol=SYM, side=side, amount=amount, entry_price=100.0, stop_price=98.0, leverage=3,
                     margin=amount * 100.0 / 3, notional=amount * 100.0, risk_usdt=2.0 * amount, conviction="normal",
                     size_pct=0.06, strategy=strategy)


class Adapter:
    """Scriptable exchange. `lookups` is the sequence of answers to fetch_order_by_client_id (the last one repeats);
    the exchange position follows the order unless `position` says otherwise, and a reduce-only close flattens it."""

    def __init__(self, *, entry=None, entry_error=None, lookup=None, lookups=None, lookup_errors=0, stop_error=None,
                 position="auto", close_error=None, positions_error=None, cancel_error=None, cancel_to=None):
        self.entry, self.entry_error = entry, entry_error
        self.lookup_script = list(lookups) if lookups is not None else [lookup]
        self.lookup_errors, self.stop_error = lookup_errors, stop_error
        self.position, self.close_error, self.positions_error = position, close_error, positions_error
        self.cancel_error, self.cancel_to = cancel_error, cancel_to
        self.market_calls, self.stop_calls, self.cancels, self.lookups = [], [], [], 0
        self.flat = False
        self.listed_stops: list[dict] = []

    @property
    def lookup_result(self):
        return self.lookup_script[min(self.lookups, len(self.lookup_script) - 1)]

    async def configure_symbol(self, symbol, leverage):
        pass

    async def market_order(self, symbol, side, amount, reduce_only=False, client_id=None, strategy=None, reason=None):
        self.market_calls.append(dict(side=side, amount=amount, reduce_only=reduce_only, client_id=client_id))
        if reduce_only:
            if self.close_error:
                raise self.close_error
            self.flat = True
            return {"filled": amount, "status": "closed"}
        if self.entry_error:
            raise self.entry_error
        return self.entry

    async def fetch_order_by_client_id(self, symbol, client_id):
        if self.lookup_errors:
            self.lookup_errors -= 1
            self.lookups += 1
            raise ccxt.NetworkError("down")
        result = self.lookup_result
        self.lookups += 1
        return result

    async def cancel_order_by_client_id(self, symbol, client_id):
        self.cancels.append(client_id)
        if self.cancel_error:
            raise self.cancel_error
        if self.cancel_to is not None:
            self.lookup_script = [self.cancel_to]
            self.lookups = 0

    async def fetch_open_positions(self, symbols=None):
        if self.positions_error:
            raise self.positions_error
        if self.flat:
            return []
        if self.position == "auto":
            order = next((o for o in (self.lookup_script[-1], self.entry) if o and (o.get("filled") or 0) > 0), None)
            qty = float(order["filled"]) if order else 0.0
        else:
            qty = float(self.position)
        return [{"symbol": SYM, "side": "long", "contracts": qty, "entryPrice": 100.0}] if qty else []

    async def stop_market(self, symbol, side, amount, stop_price):
        self.stop_calls.append((side, amount, stop_price))
        if self.stop_error:
            raise self.stop_error
        self.listed_stops.append({"id": "stop-1", "symbol": symbol, "side": side, "type": "stop_market", "reduceOnly": True,
                                  "closePosition": False, "positionSide": "BOTH", "workingType": "MARK_PRICE",
                                  "stopPrice": stop_price, "amount": amount, "status": "open"})
        return {"id": "stop-1"}

    async def fetch_stop_orders(self, symbol):
        return list(self.listed_stops)


class NoLookupAdapter(Adapter):
    fetch_order_by_client_id = None            # an adapter that cannot look orders up by client id


async def no_sleep(_):
    pass


async def test_clean_fill_places_a_stop_for_the_filled_quantity():
    a = Adapter(entry={"filled": 1.0, "status": "closed"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 1.0 and not out["partial"] and a.stop_calls == [("sell", 1.0, 98.0)]
    assert a.market_calls[0]["client_id"] == out["client_order_id"]


async def test_partial_fill_protects_only_what_filled():
    a = Adapter(entry={"filled": 0.4, "status": "closed"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["partial"] and a.stop_calls == [("sell", 0.4, 98.0)]


async def test_zero_fill_raises_not_filled_and_places_no_stop():
    a = Adapter(entry={"filled": 0, "status": "canceled"})
    with pytest.raises(OrderNotFilled):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert a.stop_calls == []


async def test_network_error_then_order_found_continues_without_resending():
    a = Adapter(entry_error=ccxt.RequestTimeout("t"), lookup={"filled": 1.0, "status": "closed"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert len([c for c in a.market_calls if not c["reduce_only"]]) == 1          # exactly one entry order ever sent
    assert a.lookups == 1 and out["filled"] == 1.0 and a.stop_calls


async def test_network_error_and_order_absent_is_not_filled_and_not_resent():
    a = Adapter(entry_error=ccxt.NetworkError("x"), lookup=None)
    with pytest.raises(OrderNotFilled):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert len(a.market_calls) == 1 and a.stop_calls == []


async def test_network_error_and_lookup_keeps_failing_is_uncertain():
    a = Adapter(entry_error=ccxt.NetworkError("x"), lookup_errors=99)
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert a.lookups == 3 and len(a.market_calls) == 1


async def test_no_lookup_available_is_uncertain():
    a = NoLookupAdapter(entry_error=ccxt.NetworkError("x"))
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert len(a.market_calls) == 1


async def test_failed_stop_closes_the_position_and_reraises():
    a = Adapter(entry={"filled": 0.7, "status": "closed"}, stop_error=RuntimeError("stop rejected"))
    with pytest.raises(RuntimeError):
        await execute_plan(a, plan(), sleep=no_sleep)
    close = a.market_calls[-1]
    assert close["reduce_only"] and close["side"] == "sell" and close["amount"] == 0.7


def test_client_order_ids_are_unique_legal_and_carry_the_strategy():
    ids = {new_client_order_id(plan("trend")) for _ in range(50)}
    assert len(ids) == 50
    for i in ids:
        assert len(i) <= 36 and all(c.isalnum() or c in "-_" for c in i) and "-tre-" in i


# ------------------------------------------------------------------ what an order status proves (C4)
@pytest.mark.parametrize("order,kind", [
    ({"status": "canceled", "filled": 0}, "unfilled"),
    ({"status": "expired", "filled": 0.0}, "unfilled"),
    ({"status": "rejected", "filled": 0}, "unfilled"),
    ({"status": "canceled", "filled": 0.4}, "partial_closed"),
    ({"status": "expired", "filled": 0.4}, "partial_closed"),
    ({"status": "closed", "filled": 1.0}, "filled"),
    ({"status": "open", "filled": 0}, "pending"),
    ({"status": "new", "filled": 0}, "pending"),
    ({"status": "open", "filled": 0.4}, "pending"),
    ({"status": "partially_filled", "filled": 0.4}, "pending"),
    ({"status": "canceled", "filled": None}, "pending"),         # terminal, but the fill is unknown
    ({"status": "closed", "filled": None}, "pending"),           # claims closed, reports no fill
    ({"status": "closed", "filled": 0}, "pending"),
    ({"status": None, "filled": 0}, "pending"),
    ({}, "pending"),
    (None, "pending"),
])
def test_only_a_terminal_status_with_zero_fill_counts_as_unfilled(order, kind):
    assert classify_entry(order)[0] == kind


# ------------------------------------------------------------------ uncertain entries are never "safely unfilled" (C4)
async def test_open_order_with_zero_fills_is_uncertain_not_unfilled_and_nothing_is_resent():
    a = Adapter(entry={"filled": 0, "status": "open"}, lookup={"filled": 0, "status": "open"})
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert len([c for c in a.market_calls if not c["reduce_only"]]) == 1          # no duplicate entry
    assert a.stop_calls == [] and len(a.cancels) == 1                              # remainder cancel was attempted by client id
    assert a.cancels[0] == a.market_calls[0]["client_id"]


async def test_order_that_is_open_at_first_and_filled_on_the_next_lookup_is_protected_without_a_second_entry():
    a = Adapter(entry={"filled": 0, "status": "open"},
                lookups=[{"filled": 0, "status": "open"}, {"filled": 1.0, "status": "closed"}])
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 1.0 and not out["outstanding"] and a.stop_calls == [("sell", 1.0, 98.0)]
    assert len([c for c in a.market_calls if not c["reduce_only"]]) == 1


async def test_timeout_then_lookup_showing_an_open_zero_fill_order_stays_uncertain():
    a = Adapter(entry_error=ccxt.RequestTimeout("t"), lookup={"filled": 0, "status": "new"})
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert a.stop_calls == [] and len(a.market_calls) == 1


async def test_partially_filled_working_order_protects_the_filled_part_and_cancels_the_rest():
    a = Adapter(entry={"filled": 0.4, "status": "open"}, lookup={"filled": 0.4, "status": "open"},
                cancel_to={"filled": 0.4, "status": "canceled"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 0.4 and out["partial"] and not out["outstanding"]
    assert a.stop_calls == [("sell", 0.4, 98.0)] and len(a.cancels) == 1


async def test_partial_fill_whose_remainder_cannot_be_cancelled_is_protected_and_flagged_outstanding():
    a = Adapter(entry={"filled": 0.4, "status": "open"}, lookup={"filled": 0.4, "status": "open"},
                cancel_error=ccxt.NetworkError("cancel timed out"))
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["outstanding"] is True and a.stop_calls == [("sell", 0.4, 98.0)]       # the filled part is never left bare


async def test_canceled_order_with_zero_fills_is_definitively_unfilled():
    a = Adapter(entry={"filled": 0, "status": "canceled"})
    with pytest.raises(OrderNotFilled) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert exc.value.definitive is True and a.stop_calls == [] and len(a.market_calls) == 1


async def test_terminal_cancel_with_a_partial_fill_still_gets_a_stop_for_the_filled_part():
    a = Adapter(entry={"filled": 0.25, "status": "canceled"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 0.25 and a.stop_calls == [("sell", 0.25, 98.0)]


async def test_timeout_followed_by_a_successful_lookup_uses_the_looked_up_fill_without_resending():
    a = Adapter(entry_error=ccxt.RequestTimeout("t"), lookup={"filled": 1.0, "status": "closed"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 1.0 and a.lookups == 1
    assert [c["reduce_only"] for c in a.market_calls] == [False]


async def test_timeout_followed_by_an_unreachable_lookup_is_uncertain_and_never_resent():
    a = Adapter(entry_error=ccxt.RequestTimeout("t"), lookup_errors=99)
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert len(a.market_calls) == 1 and a.stop_calls == []


async def test_an_order_the_exchange_has_not_seen_after_a_timeout_is_not_confirmed_unfilled():
    a = Adapter(entry_error=ccxt.RequestTimeout("t"), lookup=None)
    with pytest.raises(OrderNotFilled) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert exc.value.definitive is False and exc.value.client_id               # the request may still be in flight


async def test_an_exchange_rejection_is_definitive_and_resends_nothing():
    a = Adapter(entry_error=ccxt.InsufficientFunds("no margin"))
    with pytest.raises(OrderNotFilled) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert exc.value.definitive is True and len(a.market_calls) == 1


async def test_an_unknown_failure_while_sending_is_uncertain_not_ignored():
    a = Adapter(entry_error=ValueError("boom"))
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)


async def test_a_reported_fill_with_no_exchange_position_is_uncertain_not_protected():
    a = Adapter(entry={"filled": 1.0, "status": "closed"}, position=0.0)
    with pytest.raises(OrderUncertain):
        await execute_plan(a, plan(), sleep=no_sleep)
    assert a.stop_calls == []


async def test_the_stop_covers_the_exchange_position_when_it_differs_from_the_reported_fill():
    a = Adapter(entry={"filled": 0.9, "status": "closed"}, position=1.0)
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert a.stop_calls == [("sell", 1.0, 98.0)] and out["filled"] == 1.0


async def test_an_unreadable_position_still_gets_a_stop_for_the_reported_fill_but_is_not_called_protected():
    a = Adapter(entry={"filled": 1.0, "status": "closed"}, positions_error=ccxt.NetworkError("down"))
    with pytest.raises(ProtectionFailure) as exc:                      # the stop could not be confirmed against the position
        await execute_plan(a, plan(), sleep=no_sleep)
    assert a.stop_calls[0] == ("sell", 1.0, 98.0) and "not confirmed" in exc.value.detail


# ------------------------------------------------------------------ protection failure is never reported as closed (C1)
async def test_stop_failure_and_close_failure_raises_protection_failure_with_the_open_quantity():
    a = Adapter(entry={"filled": 1.0, "status": "closed"}, stop_error=RuntimeError("stop rejected"),
                close_error=ccxt.NetworkError("close failed"))
    with pytest.raises(ProtectionFailure) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert "1 still open" in exc.value.detail and exc.value.client_id and exc.value.__cause__.args == ("stop rejected",)


async def test_stop_failure_and_a_close_that_does_not_flatten_the_position_is_a_protection_failure():
    class Sticky(Adapter):
        async def market_order(self, *a, **k):
            res = await super().market_order(*a, **k)
            self.flat = False                                   # the exchange still shows the position
            return res

    a = Sticky(entry={"filled": 1.0, "status": "closed"}, stop_error=RuntimeError("stop rejected"))
    with pytest.raises(ProtectionFailure):
        await execute_plan(a, plan(), sleep=no_sleep)


async def test_stop_failure_with_an_unreadable_position_after_the_close_is_a_protection_failure():
    class Blind(Adapter):
        async def fetch_open_positions(self, symbols=None):
            if self.flat:
                raise ccxt.NetworkError("down")
            return await super().fetch_open_positions(symbols)

    a = Blind(entry={"filled": 1.0, "status": "closed"}, stop_error=RuntimeError("stop rejected"))
    with pytest.raises(ProtectionFailure) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert "unknown" in exc.value.detail
