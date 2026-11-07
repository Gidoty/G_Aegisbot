"""Drive the REAL BinanceFuturesAdapter through the REAL installed ccxt, replacing only the HTTP layer.

ccxt's implicit REST methods (for example `fapiPrivateGetOpenAlgoOrders`) are replaced on the instance with recorders that
return representative Binance payloads, so ccxt's own routing, parameter mapping and parsing run exactly as in production.
No network is used. The payload shapes follow the Binance USD-M documentation and the sample responses quoted in ccxt's
own source; their exact production shape still needs confirming on the testnet."""
import copy

from g_aegisbot.exchange.binance_futures import BinanceFuturesAdapter

SYM = "BTC/USDT:USDT"
MARKET_ID = "BTCUSDT"

MARKET = {
    "id": MARKET_ID, "symbol": SYM, "base": "BTC", "quote": "USDT", "settle": "USDT", "baseId": "BTC", "quoteId": "USDT",
    "settleId": "USDT", "type": "swap", "spot": False, "margin": False, "swap": True, "future": False, "option": False,
    "contract": True, "linear": True, "inverse": False, "contractSize": 1.0, "active": True,
    "precision": {"amount": 0.001, "price": 0.1}, "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
    "info": {"orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"]},
}
ETH_MARKET = {**MARKET, "id": "ETHUSDT", "symbol": "ETH/USDT:USDT", "base": "ETH", "baseId": "ETH"}


class Recorder:
    """Everything the adapter sent to ccxt's HTTP layer: [(endpoint name, params)]."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def params(self, name: str) -> list[dict]:
        return [p for n, p in self.calls if n == name]


def make(listings: dict | None = None, **responses):
    """(adapter, recorder). `listings` maps 'regular' / 'algo' to a mutable list returned by the open-orders endpoints;
    `responses` maps an implicit method name to the payload (or an Exception) it should answer with."""
    a = BinanceFuturesAdapter("key", "secret", testnet=False)     # sandbox mode is refused by this ccxt for futures; no network is used either way
    ex = a.exchange
    ex.set_markets([copy.deepcopy(MARKET), copy.deepcopy(ETH_MARKET)])
    rec = Recorder()
    listings = listings if listings is not None else {}

    def install(method, answer):
        async def fn(params=None, *args, **kw):
            rec.calls.append((method, dict(params or {})))
            result = answer(params) if callable(answer) else answer
            if isinstance(result, Exception):
                raise result
            return result
        setattr(ex, method, fn)

    install("fapiPrivateGetLeverageBracket", [])            # fetch_positions also asks for these; nothing may reach the network
    install("fapiPrivateV3GetPositionRisk", [])
    install("fapiPrivateGetOpenOrders", lambda p: list(listings.get("regular", [])))
    install("fapiPrivateGetOpenAlgoOrders", lambda p: list(listings.get("algo", [])))
    for method, answer in responses.items():
        install(method, answer)
    return a, rec


# ---------------------------------------------------------------- representative payloads
def regular_stop(**over) -> dict:
    """A STOP_MARKET order from GET /fapi/v1/openOrders (Binance's field names)."""
    base = {"orderId": 2000000002, "symbol": MARKET_ID, "status": "NEW", "clientOrderId": "c2", "price": "0", "avgPrice": "0",
            "origQty": "0.100", "executedQty": "0", "cumQuote": "0", "timeInForce": "GTC", "type": "STOP_MARKET",
            "reduceOnly": True, "closePosition": False, "side": "SELL", "positionSide": "BOTH", "stopPrice": "95000",
            "workingType": "MARK_PRICE", "priceProtect": False, "origType": "STOP_MARKET", "time": 1700000000000,
            "updateTime": 1700000000000}
    base.update(over)
    return base


def algo_stop(**over) -> dict:
    """A conditional order from GET /fapi/v1/openAlgoOrders."""
    base = {"algoId": 1000000001, "clientAlgoId": "c1", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
            "symbol": MARKET_ID, "side": "SELL", "positionSide": "BOTH", "timeInForce": "GTC", "quantity": "0.100",
            "algoStatus": "NEW", "triggerPrice": "95000", "price": "0", "workingType": "MARK_PRICE", "closePosition": False,
            "reduceOnly": True, "priceProtect": False, "createTime": 1700000000000, "updateTime": 1700000000000}
    base.update(over)
    return base


def regular_limit(**over) -> dict:
    return regular_stop(**{"orderId": 3000000003, "clientOrderId": "c3", "type": "LIMIT", "origType": "LIMIT", "price": "96000",
                           "stopPrice": "0", "reduceOnly": True, **over})


def regular_market(**over) -> dict:
    """A market order that carries price-like fields (avgPrice, a non-zero stopPrice): still never a protective stop."""
    return regular_stop(**{"orderId": 4000000004, "clientOrderId": "c4", "type": "MARKET", "origType": "MARKET",
                           "avgPrice": "95500", "stopPrice": "95000", **over})


def position(amount="0.100", side="BOTH", symbol=MARKET_ID, **over) -> dict:
    """An entry from GET /fapi/v3/positionRisk."""
    try:
        negative = float(amount) < 0
    except (TypeError, ValueError):
        negative = False
    base = {"symbol": symbol, "positionSide": side, "positionAmt": amount, "entryPrice": "96000.0", "breakEvenPrice": "96050.0",
            "markPrice": "96100.0", "unRealizedProfit": "10.0", "liquidationPrice": "64000.0", "isolatedMargin": "3200.0",
            "notional": "-9610.0" if negative else "9610.0", "isolatedWallet": "3200.0", "updateTime": 1700000000000, "initialMargin": "3200.0",
            "maintMargin": "40.0", "positionInitialMargin": "3200.0", "openOrderInitialMargin": "0", "adl": 2,
            "bidNotional": "0", "askNotional": "0", "marginAsset": "USDT"}
    base.update(over)
    return base


def order_response(**over) -> dict:
    """GET /fapi/v1/order (query by origClientOrderId)."""
    base = {"orderId": 5000000005, "symbol": MARKET_ID, "status": "FILLED", "clientOrderId": "aeg-bre-BTC-1-abcd1234",
            "price": "0", "avgPrice": "96000.0", "origQty": "0.100", "executedQty": "0.100", "cumQuote": "9600", "timeInForce": "GTC",
            "type": "MARKET", "reduceOnly": False, "closePosition": False, "side": "BUY", "positionSide": "BOTH",
            "stopPrice": "0", "workingType": "CONTRACT_PRICE", "origType": "MARKET", "time": 1700000000000,
            "updateTime": 1700000000000}
    base.update(over)
    return base
