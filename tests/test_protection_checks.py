"""M1/M2: a stop only counts when it really protects the whole position, and one symbol's failure never hides the others."""
import ccxt.async_support as ccxt
import pytest

from g_aegisbot.engine.protection import ProtectionError, assess_stops
from tests.test_critical_safety import A, B, C, build, positions, prepared, trades


def stop(oid="s1", side="sell", price=95.0, amount=10.0, **kw):
    base = {"id": oid, "stopPrice": price, "side": side, "amount": amount, "reduceOnly": True, "closePosition": False,
            "type": "stop_market", "workingType": "MARK_PRICE"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- pure assessment
def test_a_correct_full_size_stop_is_protection():
    a = assess_stops([stop()], position_side="long", position_qty=10.0)
    assert a.best["id"] == "s1" and not a.invalid


@pytest.mark.parametrize("position_side,wrong", [("long", "buy"), ("short", "sell")])
def test_a_stop_on_the_wrong_side_is_not_protection(position_side, wrong):
    a = assess_stops([stop(side=wrong)], position_side=position_side, position_qty=10.0)
    assert a.best is None and "wrong side" in a.invalid[0][1]


def test_mirrored_short_side_is_accepted():
    assert assess_stops([stop(side="buy", price=105.0)], position_side="short", position_qty=10.0).best


def test_an_insufficient_quantity_is_not_full_protection():
    a = assess_stops([stop(amount=4.0)], position_side="long", position_qty=10.0)
    assert a.best is None and "insufficient quantity" in a.invalid[0][1]


def test_rounding_sized_differences_are_tolerated_but_real_gaps_are_not():
    assert assess_stops([stop(amount=10.0 * (1 - 1e-8))], position_side="long", position_qty=10.0).best
    assert assess_stops([stop(amount=9.9)], position_side="long", position_qty=10.0).best is None


def test_two_half_size_stops_do_not_add_up_to_protection():
    a = assess_stops([stop("a", amount=5.0), stop("b", amount=5.0)], position_side="long", position_qty=10.0)
    assert a.best is None and len(a.invalid) == 2


@pytest.mark.parametrize("override,fragment", [
    ({"reduceOnly": False}, "not reduce-only"),
    ({"reduceOnly": None}, "not reduce-only"),
    ({"type": "stop_limit"}, "not a stop-market"),
    ({"type": "trailing_stop_market"}, "trailing"),
    ({"workingType": "CONTRACT_PRICE"}, "CONTRACT_PRICE"),
    ({"stopPrice": 0}, "invalid trigger price"),
    ({"stopPrice": float("nan")}, "invalid trigger price"),
    ({"stopPrice": None}, "no trigger price"),
    ({"amount": None}, "quantity unknown"),
    ({"amount": 0}, "quantity unknown"),
])
def test_orders_that_could_not_protect_are_rejected_with_a_reason(override, fragment):
    a = assess_stops([stop(**override)], position_side="long", position_qty=10.0)
    assert a.best is None and fragment in a.invalid[0][1]


def test_a_close_position_stop_needs_no_quantity():
    a = assess_stops([stop(amount=None, closePosition="true", reduceOnly=None)], position_side="long", position_qty=10.0)
    assert a.best is not None


def test_duplicate_valid_stops_keep_the_tightest_and_list_the_rest():
    long_ = assess_stops([stop("a", price=95.0), stop("b", price=97.0), stop("c", price=96.0)],
                         position_side="long", position_qty=10.0)
    assert long_.best["id"] == "b" and {o["id"] for o in long_.duplicates} == {"a", "c"}
    short = assess_stops([stop("a", "buy", 105.0), stop("b", "buy", 103.0)], position_side="short", position_qty=10.0)
    assert short.best["id"] == "b"


def test_a_valid_stop_next_to_invalid_ones_is_still_the_protection():
    a = assess_stops([stop("bad", side="buy"), stop("part", amount=2.0), stop("ok")], position_side="long", position_qty=10.0)
    assert a.best["id"] == "ok" and {o["id"] for o, _ in a.invalid} == {"bad", "part"}


# ---------------------------------------------------------------- a stop-book exchange to drive the runner
class Book:
    """Positions and stop orders in memory, with an ordered event log and switchable failures."""

    def __init__(self, positions, stops=None, price=100.0):
        self.pos = dict(positions)                 # symbol -> (side, qty, entry)
        self.stops = {s: list(v) for s, v in (stops or {}).items()}
        self.price = price
        self.events: list[tuple] = []
        self.fail_fetch_stops: set[str] = set()
        self.fail_place: set[str] = set()
        self.fail_close: set[str] = set()
        self._n = 0

    async def fetch_open_positions(self, symbols=None):
        return [{"symbol": s, "side": side, "contracts": q, "entryPrice": e} for s, (side, q, e) in self.pos.items()
                if (not symbols or s in symbols) and q]

    async def fetch_stop_orders(self, symbol):
        self.events.append(("fetch_stops", symbol))
        if symbol in self.fail_fetch_stops:
            raise ccxt.NetworkError("exchange timed out")
        return [dict(o) for o in self.stops.get(symbol, [])]

    async def fetch_last_price(self, symbol):
        return self.price

    async def stop_market(self, symbol, side, amount, stop_price):
        self.events.append(("stop_market", symbol, side, amount, stop_price))
        if symbol in self.fail_place:
            raise ccxt.ExchangeError("stop rejected")
        self._n += 1
        order = stop(f"new{self._n}", side, stop_price, amount)
        self.stops.setdefault(symbol, []).append(order)
        return {"id": order["id"], "stopPrice": stop_price}

    async def cancel_order(self, symbol, order_id):
        self.events.append(("cancel", symbol, order_id))
        self.stops[symbol] = [o for o in self.stops.get(symbol, []) if o["id"] != order_id]

    async def cancel_all(self, symbol):
        self.events.append(("cancel_all", symbol))
        self.stops[symbol] = []

    async def market_order(self, symbol, side, amount, reduce_only=False, **kw):
        self.events.append(("market", symbol, side, amount, reduce_only))
        if symbol in self.fail_close:
            raise ccxt.NetworkError("close failed")
        self.pos[symbol] = (self.pos[symbol][0], 0, self.pos[symbol][2])
        return {"filled": amount, "status": "closed"}


def ev(book, kind, symbol=None):
    return [e for e in book.events if e[0] == kind and (symbol is None or e[1] == symbol)]


def index(book, kind, symbol, order_id=None):
    for i, e in enumerate(book.events):
        if e[0] == kind and e[1] == symbol and (order_id is None or e[2] == order_id):
            return i
    raise AssertionError(f"no {kind} event for {symbol}")


async def runner_and_profile(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B, C))
    r, profile, _ = await prepared(w)
    return w, r, profile


async def test_a_wrong_side_stop_is_replaced_and_only_removed_after_the_replacement_is_confirmed(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("wrong", side="buy", price=95.0)]})
    await r._ensure_protected(profile, book)
    placed = ev(book, "stop_market", A)
    assert len(placed) == 1 and placed[0][2:4] == ("sell", 10.0)
    assert index(book, "stop_market", A) < index(book, "cancel", A, "wrong")      # replacement first, removal second
    assert [o["id"] for o in book.stops[A]] == ["new1"]
    assert [t.symbol for t in trades(w["sf"])] == [A] and trades(w["sf"])[0].stop_order_id == "new1"


async def test_an_undersized_stop_is_replaced_by_a_full_size_stop_and_then_removed(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("small", amount=4.0, price=96.0)]})
    await r._ensure_protected(profile, book)
    assert ev(book, "stop_market", A)[0][3] == 10.0
    assert index(book, "stop_market", A) < index(book, "cancel", A, "small")
    assert [o["id"] for o in book.stops[A]] == ["new1"] and book.stops[A][0]["amount"] == 10.0


async def test_duplicate_valid_stops_leave_the_tightest_and_place_nothing_new(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("loose", price=94.0), stop("tight", price=97.0)]})
    await r._ensure_protected(profile, book)
    assert ev(book, "stop_market") == []
    assert [o["id"] for o in book.stops[A]] == ["tight"]
    assert trades(w["sf"])[0].stop_order_id == "tight"


async def test_a_stale_stop_of_the_wrong_kind_does_not_count_and_is_cleaned_up_after_replacement(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("limit", type="stop_limit", price=96.0),
                                                 stop("contract", workingType="CONTRACT_PRICE", price=96.5)]})
    await r._ensure_protected(profile, book)
    assert len(ev(book, "stop_market", A)) == 1
    assert [o["id"] for o in book.stops[A]] == ["new1"]


async def test_nothing_is_cancelled_when_the_replacement_cannot_be_placed_or_the_position_closed(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("small", amount=4.0, price=96.0)]})
    book.fail_place, book.fail_close = {A}, {A}
    with pytest.raises(ProtectionError) as exc:
        await r._ensure_protected(profile, book)
    assert set(exc.value.failures) == {A}
    assert ev(book, "cancel") == [] and ev(book, "cancel_all") == []             # the partial stop was left alone
    assert [o["id"] for o in book.stops[A]] == ["small"]
    assert book.pos[A][1] == 10.0                                                 # still open, and we do not claim otherwise


async def test_one_failing_symbol_does_not_stop_the_others_from_being_checked_and_repaired(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0), B: ("long", 10.0, 100.0), C: ("long", 10.0, 100.0)})
    book.fail_fetch_stops = {B}
    with pytest.raises(ProtectionError) as exc:
        await r._ensure_protected(profile, book)
    assert set(exc.value.failures) == {B} and "NetworkError" in exc.value.failures[B]
    assert len(ev(book, "stop_market", A)) == 1 and len(ev(book, "stop_market", C)) == 1      # A and C still repaired
    assert {t.symbol for t in trades(w["sf"])} == {A, C}


async def test_failures_on_several_symbols_are_all_reported(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0), B: ("long", 10.0, 100.0), C: ("long", 10.0, 100.0)})
    book.fail_fetch_stops = {A, C}
    with pytest.raises(ProtectionError) as exc:
        await r._ensure_protected(profile, book)
    assert set(exc.value.failures) == {A, C}
    assert len(ev(book, "stop_market", B)) == 1


async def test_an_unresolved_protection_failure_marks_the_account_unverified_and_blocks_entries(tmp_path, monkeypatch, caplog):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0), B: ("long", 10.0, 100.0)})
    await r._load_journal("alice")                                              # entries also need the order journal read
    book.fail_fetch_stops = {B}
    assert await r._verify_exchange_state(profile, book) is False
    reason = r._entries_blocked("alice")
    assert reason and B in reason and "1 symbol" in reason
    book.fail_fetch_stops = set()
    assert await r._verify_exchange_state(profile, book) is True               # recovers once everything verifies
    assert r._entries_blocked("alice") is None


async def test_a_short_position_is_protected_by_a_buy_stop_and_a_sell_stop_is_rejected(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    ok = Book({A: ("short", 10.0, 100.0)}, {A: [stop("ok", side="buy", price=103.0)]})
    await r._ensure_protected(profile, ok)
    assert ev(ok, "stop_market") == []
    bad = Book({B: ("short", 10.0, 100.0)}, {B: [stop("bad", side="sell", price=103.0)]})
    await r._ensure_protected(profile, bad)
    assert ev(bad, "stop_market", B)[0][2] == "buy"
    assert index(bad, "stop_market", B) < index(bad, "cancel", B, "bad")


async def test_a_replacement_that_is_accepted_but_never_visible_is_an_error_not_a_success(tmp_path, monkeypatch):
    w, r, profile = await runner_and_profile(tmp_path, monkeypatch)
    book = Book({A: ("long", 10.0, 100.0)})

    async def ghost(symbol, side, amount, stop_price):
        book.events.append(("stop_market", symbol, side, amount, stop_price))
        return {"id": "ghost"}                                                  # accepted, but not in the order list

    book.stop_market = ghost
    with pytest.raises(ProtectionError) as exc:
        await r._ensure_protected(profile, book)
    assert "not visible" in exc.value.failures[A]
    assert trades(w["sf"]) == []                                                # no record claims it is protected
