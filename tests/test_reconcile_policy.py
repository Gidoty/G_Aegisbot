"""The release policy as a pure function of the evidence: which combinations may release automatically, which need an
operator, and which block. Errors are never turned into empty results."""
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest

from g_aegisbot.engine.reconcile import Evidence, decide, gather_evidence
from g_aegisbot.exchange.binance_futures import EvidenceIncomplete

SYM, CID = "BTC/USDT:USDT", "aeg-bre-BTC-1-abcd1234"


def ev(**kw) -> Evidence:
    """Evidence in which everything has been read and nothing was found; override fields per case."""
    base = dict(symbol=SYM, client_id=CID, since_ms=0, until_ms=1, lookup_state="not_found", history_state="complete",
                trades_state="complete", open_state="complete", conditional_state="complete", position_state="ok",
                stops_state="ok", mode_state="oneway")
    if kw.get("position_qty") and "position_modes" not in kw:
        base["position_modes"] = ["BOTH"]                # a real position always reports its (one-way) mode
    base.update(kw)
    return Evidence(**base)


def order(status, filled):
    return {"id": "77", "status": status, "filled": filled, "amount": 5.0}


def test_nothing_found_anywhere_is_not_seen_and_never_automatic():
    d = decide(ev())
    assert d.outcome == "not_seen" and d.auto is False


def test_only_a_terminal_outcome_of_the_order_itself_is_automatic():
    assert decide(ev(lookup_state="found", order=order("closed", 5.0), position_qty=5.0, position_side="long")).auto is True
    assert decide(ev(lookup_state="found", order=order("canceled", 0))).auto is True
    assert decide(ev(lookup_state="found", order=order("expired", 0))).outcome == "unfilled"
    assert decide(ev(lookup_state="found", order=order("canceled", 2.0), position_qty=2.0, position_side="long")).outcome == "filled"


@pytest.mark.parametrize("kw", [
    dict(lookup_state="error", errors={"lookup": "RequestTimeout"}),
    dict(lookup_state="unsupported"),
    dict(position_state="error", errors={"position": "NetworkError"}),
    dict(position_modes=["LONG"], position_qty=1.0, position_side="long"),
    dict(symbol="?"), dict(symbol=""), dict(client_id=""),
    dict(history_state="error"), dict(history_state="incomplete"), dict(history_state="unsupported"),
    dict(trades_state="error"), dict(trades_state="incomplete"), dict(trades_state="unsupported"),
    dict(open_state="error"), dict(open_state="unsupported"), dict(open_match=True),
    dict(conditional_state="error"), dict(conditional_state="unsupported"), dict(conditional_match=True),
    dict(history_match={"clientOrderId": CID}),
    dict(trades=[{"id": "t1"}]),
    dict(position_qty=1.0, position_side="long"),
])
def test_every_gap_or_contradiction_blocks_a_not_found_order(kw):
    d = decide(ev(**kw))
    assert d.outcome == "blocked" and d.auto is False and d.reasons


@pytest.mark.parametrize("kw", [
    dict(order=order("open", 0)), dict(order=order("new", 0)), dict(order=order("open", 2.0)),
    dict(order=order("canceled", None)), dict(order={"status": None}),
])
def test_a_found_order_that_is_not_terminal_or_not_provable_blocks(kw):
    assert decide(ev(lookup_state="found", **kw)).outcome == "blocked"


@pytest.mark.parametrize("kw", [
    dict(trades_state="error"), dict(trades_state="incomplete"), dict(trades_state="unsupported"),
    dict(trades=[{"id": "t1"}], trades_for_order=1), dict(trades=[{"id": "t1"}]),
    dict(position_qty=3.0, position_side="long"),
])
def test_a_terminal_zero_fill_must_be_proved_by_trades_and_a_flat_position(kw):
    assert decide(ev(lookup_state="found", order=order("canceled", 0), **kw)).outcome == "blocked"


def test_a_paper_ledger_releases_a_position_but_a_flat_ledger_still_needs_an_operator():
    assert decide(ev(ledger=True, lookup_state="unsupported", position_qty=1.0, position_side="long")).auto is True
    flat = decide(ev(ledger=True, lookup_state="unsupported"))
    assert flat.outcome == "not_seen" and flat.auto is False


def test_the_digest_changes_when_the_evidence_changes():
    assert ev().digest() == ev().digest()
    assert len({ev().digest(), ev(open_count=1).digest(), ev(position_qty=1.0).digest()}) == 3


# ---- gathering: failures are recorded, never raised and never mistaken for emptiness
class Adapter:
    ledger = False

    def __init__(self, **answers):
        self.answers = answers

    def _get(self, name):
        v = self.answers[name]
        if isinstance(v, Exception):
            raise v
        return v

    async def fetch_order_by_client_id(self, symbol, cid):
        got = self._get("lookup")
        return {"clientOrderId": cid, "symbol": symbol, **got} if isinstance(got, dict) else got      # a real exchange echoes identity
    async def assert_one_way_mode(self): return self._get("mode") if "mode" in self.answers else None
    async def fetch_orders_history(self, symbol, a, b): return self._get("history")
    async def fetch_account_trades(self, symbol, a, b): return self._get("trades")
    async def fetch_open_orders(self, symbol): return self._get("open")
    async def fetch_conditional_orders(self, symbol): return self._get("conditional")
    async def fetch_open_positions(self, symbols=None): return self._get("positions")
    async def fetch_stop_orders(self, symbol): return self._get("stops")


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def gather(**answers):
    clean = dict(lookup=None, history=[], trades=[], open=[], conditional=[], positions=[], stops=[])
    clean.update(answers)
    return await gather_evidence(Adapter(**clean), SYM, CID, NOW, NOW + timedelta(minutes=5))


async def test_clean_answers_are_recorded_as_complete_and_empty():
    e = await gather()
    assert (e.lookup_state, e.history_state, e.trades_state, e.open_state, e.conditional_state, e.position_state) == \
        ("not_found", "complete", "complete", "complete", "complete", "ok")
    assert e.flat and decide(e).outcome == "not_seen"


@pytest.mark.parametrize("name,state", [("lookup", "lookup_state"), ("history", "history_state"), ("trades", "trades_state"),
                                        ("open", "open_state"), ("conditional", "conditional_state")])
async def test_an_error_from_any_source_is_an_error_state_not_an_empty_result(name, state):
    e = await gather(**{name: ccxt.RequestTimeout("timed out")})
    assert getattr(e, state) == "error" and decide(e).outcome == "blocked"


async def test_an_incomplete_history_is_incomplete_not_empty():
    for name, state in (("history", "history_state"), ("trades", "trades_state")):
        e = await gather(**{name: EvidenceIncomplete("more than 20 pages")})
        assert getattr(e, state) == "incomplete" and decide(e).outcome == "blocked"


async def test_an_unreadable_position_blocks():
    e = await gather(positions=ccxt.NetworkError("down"))
    assert e.position_state == "error" and decide(e).outcome == "blocked"


async def test_hedge_mode_is_seen_through_the_raw_position_side():
    e = await gather(positions=[{"symbol": SYM, "side": "long", "contracts": 1.0, "info": {"positionSide": "LONG"}}])
    assert e.hedge and decide(e).outcome == "blocked"
    e = await gather(positions=[{"symbol": SYM, "side": "long", "contracts": 1.0, "info": {"positionSide": "BOTH"}}],
                     lookup={"id": "1", "status": "closed", "filled": 1.0}, stops=[])
    assert not e.hedge and decide(e).outcome == "filled"


async def test_matching_the_order_in_any_listing_is_recorded():
    e = await gather(open=[{"clientOrderId": CID}], conditional=[{"clientOrderId": "other"}],
                     history=[{"id": "4", "clientOrderId": CID}])
    assert e.open_match and not e.conditional_match and e.history_match is not None


async def test_trades_are_attributed_to_the_order_by_its_exchange_id():
    e = await gather(lookup={"id": "77", "status": "canceled", "filled": 0},
                     trades=[{"id": "t1", "order": "77"}, {"id": "t2", "order": "5"}])
    assert e.trades_for_order == 1 and decide(e).outcome == "blocked"


async def test_error_messages_are_reduced_to_the_exception_type():
    e = await gather(lookup=ccxt.AuthenticationError('binance {"msg":"Invalid API-key, secret=SHOULD-NOT-APPEAR"}'))
    assert e.errors["lookup"] == "AuthenticationError" and "SHOULD-NOT-APPEAR" not in " ".join(e.lines())
