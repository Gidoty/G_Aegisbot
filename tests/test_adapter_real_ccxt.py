"""The production Binance adapter on top of the installed ccxt, with only the HTTP layer replaced (tests/ccxt_real.py).

These tests exist because hand-written, already-normalised fake orders hid two real defects: ccxt reports a STOP_MARKET as the
unified type 'market', and cancelling a conditional (algo) order needs a different endpoint."""
import ccxt.async_support as ccxt
import pytest

from g_aegisbot.engine.protection import assess_stops
from g_aegisbot.exchange.binance_futures import (
    CancelNotConfirmed, OrderDataError, OrderIdentityMismatch, PositionDataError, parse_position_qty,
)
from tests.ccxt_real import (
    SYM, algo_stop, make, order_response, position, regular_limit, regular_market, regular_stop,
)


def protected(rows, side="long", qty=0.1):
    return assess_stops(rows, position_side=side, position_qty=qty).best is not None


# ================================================================ C-1: stop detection from raw fields
async def test_a_real_algo_stop_is_found_and_counts_as_protection():
    a, rec = make({"algo": [algo_stop()]})
    rows = await a.fetch_stop_orders(SYM)
    assert len(rows) == 1
    r = rows[0]
    assert (r["id"], r["route"], r["side"], r["amount"], r["stopPrice"], r["type"], r["positionSide"], r["status"]) == \
        ("1000000001", "algo", "sell", 0.1, 95000.0, "stop_market", "BOTH", "NEW")
    assert r["reduceOnly"] is True and r["closePosition"] is False and r["workingType"] == "MARK_PRICE"
    assert protected(rows)
    assert rec.names() == ["fapiPrivateGetOpenOrders", "fapiPrivateGetOpenAlgoOrders"]          # both listings are read


async def test_a_real_regular_stop_is_found_and_counts_as_protection():
    a, _ = make({"regular": [regular_stop()]})
    rows = await a.fetch_stop_orders(SYM)
    assert [(r["id"], r["route"], r["amount"], r["stopPrice"]) for r in rows] == [("2000000002", "regular", 0.1, 95000.0)]
    assert protected(rows)


async def test_the_unified_type_is_not_what_decides():
    """ccxt turns STOP_MARKET into 'market'; the adapter must still see the stop."""
    a, _ = make({"algo": [algo_stop()]})
    parsed = a.exchange.parse_order(algo_stop())
    assert parsed["type"] == "market"                                    # the trap that hid every stop
    assert len(await a.fetch_stop_orders(SYM)) == 1


async def test_both_routes_are_merged_and_equal_ids_on_different_routes_are_not_confused():
    a, _ = make({"regular": [regular_stop(orderId=777)], "algo": [algo_stop(algoId=777)]})
    rows = await a.fetch_stop_orders(SYM)
    assert sorted((r["route"], r["id"]) for r in rows) == [("algo", "777"), ("regular", "777")]


@pytest.mark.parametrize("payload,why", [
    (regular_limit(), "a reduce-only LIMIT order"),
    (regular_market(), "a MARKET order carrying price-like fields"),
    (regular_stop(type="MARKET", origType="STOP_MARKET"), "a stop that already turned into a market order"),
    (regular_stop(type="TAKE_PROFIT_MARKET", origType="TAKE_PROFIT_MARKET"), "a take-profit"),
    (regular_stop(reduceOnly=False, closePosition=False), "a conditional order that can open a position"),
    (regular_stop(status="FILLED"), "a finished order"),
    (regular_stop(status="CANCELED"), "a canceled order"),
])
async def test_orders_that_are_not_active_protective_stops_are_not_returned(payload, why):
    a, _ = make({"regular": [payload]})
    assert await a.fetch_stop_orders(SYM) == [], why


@pytest.mark.parametrize("status", ["TRIGGERING", "TRIGGERED", "FINISHED", "CANCELED", "EXPIRED", "REJECTED"])
async def test_algo_orders_that_are_no_longer_active_are_not_returned(status):
    a, _ = make({"algo": [algo_stop(algoStatus=status)]})
    assert await a.fetch_stop_orders(SYM) == []


async def test_take_profit_and_limit_style_conditionals_never_count_as_protection():
    a, _ = make({"algo": [algo_stop(algoId=1, orderType="TAKE_PROFIT_MARKET"), algo_stop(algoId=2, orderType="STOP"),
                          algo_stop(algoId=3, orderType="TRAILING_STOP_MARKET")]})
    rows = await a.fetch_stop_orders(SYM)
    assert sorted(r["type"] for r in rows) == ["stop", "trailing_stop_market"]      # take-profit is not even listed
    assert not protected(rows)                                                      # and neither of the others protects


async def test_a_close_position_stop_needs_no_quantity():
    a, _ = make({"algo": [algo_stop(closePosition=True, reduceOnly=False, quantity="0")]})
    rows = await a.fetch_stop_orders(SYM)
    assert rows[0]["closePosition"] is True and protected(rows)


async def test_a_hedge_mode_stop_is_listed_but_is_not_protection():
    a, _ = make({"algo": [algo_stop(positionSide="LONG")]})
    rows = await a.fetch_stop_orders(SYM)
    assert rows[0]["positionSide"] == "LONG" and not protected(rows)


@pytest.mark.parametrize("route,payload,fragment", [
    ("regular", regular_stop(type=None, origType=None), "raw order type is missing"),
    ("regular", regular_stop(status=None), "raw order status is missing"),
    ("regular", regular_stop(status="WHAT"), "unknown raw order status"),
    ("regular", regular_stop(side="HOLD"), "side"),
    ("regular", regular_stop(positionSide=None), "position side"),
    ("regular", regular_stop(positionSide="UP"), "position side"),
    ("regular", regular_stop(reduceOnly=None, closePosition=None), "neither reduceOnly nor closePosition"),
    ("regular", regular_stop(reduceOnly="maybe"), "not a boolean"),
    ("regular", regular_stop(stopPrice=None), "trigger price"),
    ("regular", regular_stop(stopPrice="0"), "trigger price is not positive"),
    ("regular", regular_stop(stopPrice="NaN"), "not finite"),
    ("regular", regular_stop(stopPrice="abc"), "not a number"),
    ("regular", regular_stop(origQty=None), "order quantity"),
    ("regular", regular_stop(origQty="0"), "quantity is not positive"),
    ("regular", regular_stop(origQty="inf"), "could not be parsed|not finite"),
    ("regular", regular_stop(type="STOP_MARKET", origType="STOP"), "ambiguous"),
    ("algo", algo_stop(orderType="LIMIT"), "unexpected order type"),
    ("algo", algo_stop(orderType=None), "raw order type is missing"),
    ("algo", algo_stop(algoStatus=None), "raw order status is missing"),
    ("algo", algo_stop(triggerPrice=None), "trigger price"),
    ("algo", algo_stop(quantity=None), "order quantity"),
    ("algo", algo_stop(positionSide=None), "position side"),
])
async def test_malformed_or_ambiguous_orders_raise_instead_of_being_guessed(route, payload, fragment):
    a, _ = make({route: [payload]})
    with pytest.raises(OrderDataError) as exc:
        await a.fetch_stop_orders(SYM)
    import re
    assert re.search(fragment, str(exc.value)), str(exc.value)


async def test_an_order_without_raw_fields_is_malformed():
    a, _ = make()
    original = a.exchange.fetch_open_orders

    async def stripped(symbol=None, since=None, limit=None, params={}):
        rows = await original(symbol, since, limit, params)
        for r in rows:
            r["info"] = {}
        return rows

    a.exchange.fetch_open_orders = stripped
    a.exchange.fapiPrivateGetOpenAlgoOrders = lambda p=None: _async([algo_stop()])
    with pytest.raises(OrderDataError, match="raw exchange fields"):
        await a.fetch_stop_orders(SYM)


async def _async(value):
    return value


async def test_a_listing_error_is_an_error_never_an_empty_result():
    for method in ("fapiPrivateGetOpenAlgoOrders", "fapiPrivateGetOpenOrders"):
        for failure in (ccxt.BadRequest("unsupported"), ccxt.RequestTimeout("timed out"), ccxt.ExchangeError("-1000")):
            a, _ = make(**{method: failure})
            with pytest.raises(type(failure)):
                await a.fetch_stop_orders(SYM)


async def test_a_response_that_is_not_a_list_is_malformed():
    a, _ = make(fapiPrivateGetOpenAlgoOrders={"code": -1, "msg": "oops"})
    with pytest.raises(Exception):
        await a.fetch_stop_orders(SYM)


# ================================================================ C-2: cancellation routes and proof
def listing_pair(rec_order_ids):
    """Open-order listings that shrink when the matching DELETE endpoint is called."""
    state = {"regular": [regular_stop(orderId=int(i)) for i in rec_order_ids.get("regular", [])],
             "algo": [algo_stop(algoId=int(i)) for i in rec_order_ids.get("algo", [])]}
    return state


async def test_an_algo_stop_is_cancelled_on_the_algo_endpoint_and_proved_gone():
    state = listing_pair({"algo": ["1000000001"]})

    def delete_algo(p):
        state["algo"] = [o for o in state["algo"] if str(o["algoId"]) != str(p["algoId"])]
        return {"code": "200", "msg": "success"}

    a, rec = make(state, fapiPrivateDeleteAlgoOrder=delete_algo, fapiPrivateDeleteOrder=ccxt.ExchangeError("must not be called"))
    await a.cancel_order(SYM, "1000000001", route="algo")
    assert rec.params("fapiPrivateDeleteAlgoOrder") == [{"symbol": "BTCUSDT", "algoId": "1000000001"}]
    assert "fapiPrivateDeleteOrder" not in rec.names()
    assert rec.names()[-1] == "fapiPrivateGetOpenAlgoOrders"                      # the listing was re-read afterwards


async def test_a_regular_order_is_cancelled_on_the_regular_endpoint():
    state = listing_pair({"regular": ["2000000002"]})

    def delete_regular(p):
        state["regular"] = [o for o in state["regular"] if str(o["orderId"]) != str(p["orderId"])]
        return {"orderId": p["orderId"], "status": "CANCELED"}

    a, rec = make(state, fapiPrivateDeleteOrder=delete_regular)
    await a.cancel_order(SYM, "2000000002", route="regular")
    assert rec.params("fapiPrivateDeleteOrder") == [{"symbol": "BTCUSDT", "orderId": "2000000002"}]
    assert "fapiPrivateDeleteAlgoOrder" not in rec.names()


async def test_without_a_route_the_listings_decide_it():
    state = listing_pair({"algo": ["1000000001"]})

    def delete_algo(p):
        state["algo"] = []
        return {"code": "200"}

    a, rec = make(state, fapiPrivateDeleteAlgoOrder=delete_algo)
    await a.cancel_order(SYM, "1000000001")
    assert len(rec.params("fapiPrivateDeleteAlgoOrder")) == 1 and "fapiPrivateDeleteOrder" not in rec.names()


async def test_an_id_on_both_routes_is_ambiguous_and_nothing_is_cancelled():
    state = {"regular": [regular_stop(orderId=5)], "algo": [algo_stop(algoId=5)]}
    a, rec = make(state)
    with pytest.raises(CancelNotConfirmed, match="both"):
        await a.cancel_order(SYM, "5")
    assert not [n for n in rec.names() if n.startswith("fapiPrivateDelete")]


async def test_an_id_that_is_in_neither_listing_needs_no_cancel_because_the_listings_prove_it_gone():
    a, rec = make({"regular": [], "algo": []})
    await a.cancel_order(SYM, "999")
    assert not [n for n in rec.names() if n.startswith("fapiPrivateDelete")]


async def test_a_cancel_that_reports_success_but_leaves_the_order_open_is_not_confirmed():
    state = listing_pair({"algo": ["1000000001"]})
    a, _ = make(state, fapiPrivateDeleteAlgoOrder={"code": "200", "msg": "success"})       # answers 'success', removes nothing
    with pytest.raises(CancelNotConfirmed, match="still open"):
        await a.cancel_order(SYM, "1000000001", route="algo")


async def test_a_not_found_error_is_not_success_unless_the_listing_agrees():
    state = listing_pair({"algo": ["1000000001"]})
    a, _ = make(state, fapiPrivateDeleteAlgoOrder=ccxt.OrderNotFound("Unknown order sent"))
    with pytest.raises(CancelNotConfirmed, match="still open"):
        await a.cancel_order(SYM, "1000000001", route="algo")
    state2 = listing_pair({"algo": []})                                                 # it really is gone
    b, _ = make(state2, fapiPrivateDeleteAlgoOrder=ccxt.OrderNotFound("Unknown order sent"))
    await b.cancel_order(SYM, "1000000001", route="algo")


async def test_a_network_error_during_cancel_is_settled_by_the_listing_not_assumed():
    state = listing_pair({"algo": ["1000000001"]})

    def lands_but_times_out(p):
        state["algo"] = []
        return ccxt.RequestTimeout("timed out after the cancel was processed")

    a, _ = make(state, fapiPrivateDeleteAlgoOrder=lands_but_times_out)
    await a.cancel_order(SYM, "1000000001", route="algo")                                # listing shows it gone: fine
    state3 = listing_pair({"algo": ["1000000001"]})
    b, _ = make(state3, fapiPrivateDeleteAlgoOrder=ccxt.RequestTimeout("never arrived"))
    with pytest.raises(CancelNotConfirmed):
        await b.cancel_order(SYM, "1000000001", route="algo")


async def test_an_unreadable_listing_means_the_cancel_is_not_confirmed():
    state = listing_pair({"algo": ["1000000001"]})
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        return list(state["algo"]) if calls["n"] == 0 else ccxt.RequestTimeout("listing timed out")

    a, _ = make(state, fapiPrivateDeleteAlgoOrder={"code": "200"}, fapiPrivateGetOpenAlgoOrders=flaky)
    with pytest.raises(CancelNotConfirmed, match="unreadable"):
        await a.cancel_order(SYM, "1000000001", route="algo")


async def test_cancel_all_sends_both_cancel_all_endpoints_and_proves_both_listings_empty():
    state = listing_pair({"regular": ["2000000002"], "algo": ["1000000001"]})

    def clear_regular(p):
        state["regular"] = []
        return {"code": 200}

    def clear_algo(p):
        state["algo"] = []
        return {"code": 200}

    a, rec = make(state, fapiPrivateDeleteAllOpenOrders=clear_regular, fapiPrivateDeleteAlgoOpenOrders=clear_algo)
    await a.cancel_all(SYM)
    assert rec.params("fapiPrivateDeleteAllOpenOrders") == [{"symbol": "BTCUSDT"}]
    assert rec.params("fapiPrivateDeleteAlgoOpenOrders") == [{"symbol": "BTCUSDT"}]


async def test_cancel_all_raises_when_any_order_survives():
    state = listing_pair({"regular": [], "algo": ["1000000001"]})
    a, _ = make(state, fapiPrivateDeleteAllOpenOrders={"code": 200}, fapiPrivateDeleteAlgoOpenOrders={"code": 200})
    with pytest.raises(CancelNotConfirmed, match="algo:1000000001"):
        await a.cancel_all(SYM)


async def test_cancel_all_raises_when_a_listing_cannot_be_read_afterwards():
    state = listing_pair({"regular": [], "algo": []})
    a, _ = make(state, fapiPrivateDeleteAllOpenOrders={"code": 200}, fapiPrivateDeleteAlgoOpenOrders={"code": 200},
                fapiPrivateGetOpenAlgoOrders=ccxt.RequestTimeout("timed out"))
    with pytest.raises(CancelNotConfirmed, match="unreadable"):
        await a.cancel_all(SYM)


async def test_the_protective_stop_itself_is_created_on_the_algo_endpoint():
    a, rec = make(fapiPrivatePostAlgoOrder=algo_stop(), fapiPrivatePostOrder=ccxt.ExchangeError("must not be called"))
    created = await a.stop_market(SYM, "sell", 0.1, 95000.0)
    assert created["id"] == "1000000001"
    sent = rec.params("fapiPrivatePostAlgoOrder")[0]
    assert (sent["type"], sent["side"], sent["triggerPrice"], sent["quantity"], sent["reduceOnly"], sent["workingType"],
            sent["algoType"]) == ("STOP_MARKET", "SELL", "95000", "0.1", True, "MARK_PRICE", "CONDITIONAL")
    assert "fapiPrivatePostOrder" not in rec.names()


# ================================================================ H-3: malformed positions are never flat
def positions_answer(*entries):
    return {"fapiPrivateV3GetPositionRisk": list(entries)}


async def test_real_positions_one_way_long_short_and_flat():
    a, _ = make(**positions_answer(position("0.100"), position("-0.250", symbol="ETHUSDT")))
    rows = await a.fetch_open_positions()
    assert sorted((r["symbol"], r["side"], r["contracts"]) for r in rows) == [("BTC/USDT:USDT", "long", 0.1), ("ETH/USDT:USDT", "short", 0.25)]
    assert all(r["info"]["positionSide"] == "BOTH" for r in rows)
    b, _ = make(**positions_answer(position("0"), position("0.0", symbol="ETHUSDT")))
    assert await b.fetch_open_positions() == []                                          # genuinely flat


async def test_hedge_mode_entries_keep_their_position_side_and_are_not_merged():
    a, _ = make(**positions_answer(position("0.100", side="LONG"), position("0.100", side="SHORT")))
    rows = await a.fetch_open_positions([SYM])
    assert sorted(r["info"]["positionSide"] for r in rows) == ["LONG", "SHORT"]


@pytest.mark.parametrize("amount", [None, "", "NaN", "nan", "inf", "-inf", "abc", "1e999999", "--1"])
async def test_a_malformed_raw_quantity_is_unreadable_not_flat(amount):
    a, _ = make(**positions_answer(position(amount)))
    with pytest.raises(PositionDataError):
        await a.fetch_open_positions([SYM])


async def test_a_missing_quantity_field_is_unreadable_not_flat():
    entry = position()
    del entry["positionAmt"]
    a, _ = make(**positions_answer(entry))
    with pytest.raises(PositionDataError):
        await a.fetch_open_positions([SYM])


async def test_one_bad_entry_poisons_the_whole_answer_instead_of_vanishing():
    a, _ = make(**positions_answer(position("0.1"), position("oops", symbol="ETHUSDT")))
    with pytest.raises(PositionDataError):
        await a.fetch_open_positions()


async def test_a_positions_response_that_is_not_a_list_is_unreadable():
    a, _ = make()
    async def not_a_list(symbols=None, params={}):
        return {"code": -1}
    a.exchange.fetch_positions = not_a_list
    with pytest.raises(PositionDataError):
        await a.fetch_open_positions()


@pytest.mark.parametrize("tamper", [
    lambda p: p.update(contracts=None),
    lambda p: p.update(contracts=float("nan")),
    lambda p: p.update(contracts=float("inf")),
    lambda p: p.update(contracts="x"),
    lambda p: p.update(contracts=-1.0),
    lambda p: p.update(contracts=5.0),                                  # normalised 5 but the raw positionAmt says 0.1
    lambda p: p.update(contracts=0.0),                                  # normalised 0 but the raw positionAmt says 0.1 (flat?)
    lambda p: p.update(side=None),
    lambda p: p.update(side="sideways"),
    lambda p: p.update(side="short"),                                   # raw amount is positive: not a short
    lambda p: p.update(symbol=None),
    lambda p: p["info"].update(positionAmt="0.5"),
    lambda p: p["info"].update(positionAmt="nan"),
])
async def test_inconsistent_normalised_and_raw_quantities_are_unreadable(tamper):
    a, _ = make(**positions_answer(position("0.100")))
    parsed = (await a.exchange.fetch_positions([SYM]))[0]
    tamper(parsed)

    async def tampered(symbols=None, params={}):
        return [parsed]

    a.exchange.fetch_positions = tampered
    with pytest.raises(PositionDataError):
        await a.fetch_open_positions([SYM])


def test_parse_position_qty_accepts_only_consistent_entries():
    ok = {"symbol": SYM, "side": "long", "contracts": 0.1, "contractSize": 1.0, "info": {"positionAmt": "0.100", "positionSide": "BOTH"}}
    assert parse_position_qty(ok) == 0.1
    assert parse_position_qty({"symbol": SYM, "side": None, "contracts": 0.0, "info": {"positionAmt": "0.0"}}) == 0.0
    with pytest.raises(PositionDataError):
        parse_position_qty(None)
    with pytest.raises(PositionDataError):
        parse_position_qty({"symbol": SYM, "side": "long", "contracts": 0.1, "contractSize": 0.1,
                            "info": {"positionAmt": "0.100"}})          # contract size makes the raw amount inconsistent


# ================================================================ M-12: the returned order must be the one asked for
CID = "aeg-bre-BTC-1-abcd1234"


async def test_a_matching_order_is_returned():
    a, _ = make(fapiPrivateGetOrder=order_response(clientOrderId=CID))
    order = await a.fetch_order_by_client_id(SYM, CID)
    assert order["clientOrderId"] == CID and order["status"] == "closed" and order["filled"] == 0.1


async def test_not_found_is_none_and_nothing_else_is():
    a, _ = make(fapiPrivateGetOrder=ccxt.OrderNotFound("Order does not exist"))
    assert await a.fetch_order_by_client_id(SYM, CID) is None
    b, _ = make(fapiPrivateGetOrder=ccxt.RequestTimeout("timed out"))
    with pytest.raises(ccxt.RequestTimeout):
        await b.fetch_order_by_client_id(SYM, CID)


@pytest.mark.parametrize("payload,fragment", [
    (order_response(clientOrderId="aeg-bre-BTC-1-ffffffff"), "different client order id"),
    (order_response(clientOrderId=""), "no client order id"),
    (order_response(clientOrderId=None), "no client order id"),
    (order_response(symbol="ETHUSDT", clientOrderId=CID), "symbol"),
    ({}, "client order id|empty or malformed"),
])
async def test_a_returned_order_with_the_wrong_identity_is_an_error(payload, fragment):
    a, _ = make(fapiPrivateGetOrder=payload)
    with pytest.raises(OrderIdentityMismatch, match=fragment):
        await a.fetch_order_by_client_id(SYM, CID)


async def test_a_normalised_and_raw_client_id_that_disagree_is_an_error():
    a, _ = make(fapiPrivateGetOrder=order_response(clientOrderId=CID))
    real = a.exchange.fetch_order

    async def tampered(id=None, symbol=None, params={}):
        o = await real(id, symbol, params)
        o["info"]["clientOrderId"] = "someone-elses"
        return o

    a.exchange.fetch_order = tampered
    with pytest.raises(OrderIdentityMismatch, match="different client order id"):
        await a.fetch_order_by_client_id(SYM, CID)


async def test_a_normalised_symbol_that_differs_is_an_error():
    a, _ = make(fapiPrivateGetOrder=order_response(clientOrderId=CID))
    real = a.exchange.fetch_order

    async def tampered(id=None, symbol=None, params={}):
        o = await real(id, symbol, params)
        o["symbol"] = "ETH/USDT:USDT"
        return o

    a.exchange.fetch_order = tampered
    with pytest.raises(OrderIdentityMismatch, match="different symbol"):
        await a.fetch_order_by_client_id(SYM, CID)


async def test_an_order_for_another_symbol_never_counts_as_this_symbols_stop():
    a, _ = make({"regular": [regular_stop(symbol="ETHUSDT")]})
    assert await a.fetch_stop_orders(SYM) == []                       # ccxt itself filters other symbols out of the listing
    original = a.exchange.fetch_open_orders

    async def mislabelled(symbol=None, since=None, limit=None, params={}):
        rows = await original("ETH/USDT:USDT", since, limit, params)    # a listing for ETH handed back as if it were BTC's
        for r in rows:
            r["symbol"] = "ETH/USDT:USDT"
        return rows

    b, _ = make({"regular": [regular_stop(symbol="ETHUSDT")]})
    b.exchange.fetch_open_orders = mislabelled
    with pytest.raises(OrderDataError, match="different symbol"):
        await b.fetch_stop_orders(SYM)


# ---- which Binance environment the "testnet" adapter talks to (no network: only the configured URLs are read)
def _urls(**kw):
    from g_aegisbot.exchange.binance_futures import BinanceFuturesAdapter
    a = BinanceFuturesAdapter("k", "s", **kw)
    return a, a.exchange.urls["api"]


def test_the_testnet_adapter_uses_binance_demo_trading_by_default():
    _, api = _urls(testnet=True)
    assert api["fapiPrivate"].startswith("https://demo-fapi.binance.com/") and "fapi.binance.com/fapi" not in api["fapiPrivate"].replace("demo-fapi", "")


def test_the_legacy_testnet_is_available_by_explicit_choice():
    _, api = _urls(testnet=True, testnet_endpoint="testnet")
    assert "testnet.binancefuture.com" in api["fapiPrivate"]


def test_the_endpoint_can_be_chosen_from_the_environment(monkeypatch):
    monkeypatch.setenv("AEGIS_TESTNET_ENDPOINT", "testnet")
    assert "testnet.binancefuture.com" in _urls(testnet=True)[1]["fapiPrivate"]


@pytest.mark.parametrize("bad", ["mainnet", "live", "", " prod ", "demo2"])
def test_an_unknown_endpoint_is_an_error_and_never_falls_back_to_mainnet(bad, monkeypatch):
    monkeypatch.setenv("AEGIS_TESTNET_ENDPOINT", bad)
    if bad.strip() == "":
        assert "demo-fapi.binance.com" in _urls(testnet=True)[1]["fapiPrivate"]          # empty means the default
    else:
        with pytest.raises(ValueError):
            _urls(testnet=True)


def test_mainnet_is_never_a_test_environment(monkeypatch):
    monkeypatch.setenv("AEGIS_TESTNET_ENDPOINT", "testnet")
    _, api = _urls(testnet=False)
    assert api["fapiPrivate"].startswith("https://fapi.binance.com/")
