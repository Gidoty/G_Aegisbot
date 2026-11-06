"""Exchange self-test: proves the live order paths work against a real Binance account.

    export AEGIS_SELFTEST_API_KEY=...  AEGIS_SELFTEST_API_SECRET=...
    python -m g_aegisbot.tools.selftest                      # Binance futures TESTNET (default)
    python -m g_aegisbot.tools.selftest --mainnet --yes-real-money --notional 120   # real, tiny order

It opens one small position, protects it with an exchange-side stop, checks the stop can be found,
moves it (new stop first, then cancel the old one), then closes everything. Cleanup runs even when
a step fails. Keys come from the environment only and are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


async def run_selftest(adapter, symbol: str = "BTC/USDT:USDT", notional: float = 120.0, leverage: int = 1,
                       say=print) -> list[StepResult]:
    results: list[StepResult] = []
    state = {"amount": None, "stop_id": None, "opened": False}

    def record(name: str, ok: bool, detail: str = "") -> bool:
        results.append(StepResult(name, ok, detail))
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        return ok

    async def step(name, coro_fn) -> bool:
        try:
            detail = await coro_fn()
            return record(name, True, detail or "")
        except Exception as exc:
            return record(name, False, f"{type(exc).__name__}: {str(exc)[:200]}")

    async def markets():
        if symbol not in (adapter.exchange.markets or {}):
            raise RuntimeError(f"{symbol} is not a listed market")

    async def mode():
        await adapter.assert_one_way_mode()
        return "one-way position mode"

    async def balance():
        bal = await adapter.fetch_usdt_balance()
        eq = await adapter.fetch_equity()
        need = notional / leverage * 1.1
        if bal["free"] < need:
            raise RuntimeError(f"free balance {bal['free']:.2f} USDT is below the {need:.2f} needed for this test")
        return f"equity {eq:,.2f}, free {bal['free']:,.2f} USDT"

    async def configure():
        await adapter.configure_symbol(symbol, leverage)
        return f"isolated margin, {leverage}x"

    async def sizing():
        price = await adapter.fetch_last_price(symbol)
        amount = float(adapter.exchange.amount_to_precision(symbol, notional / price))
        limits = (adapter.exchange.markets.get(symbol) or {}).get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min")
        if amount <= 0 or (min_amt and amount < min_amt):
            raise RuntimeError(f"amount {amount} is below the exchange minimum {min_amt}")
        state.update(amount=amount, price=price)
        return f"{amount} @ ~{price:g}"

    async def entry():
        await adapter.market_order(symbol, "buy", state["amount"])
        state["opened"] = True

    async def visible():
        pos = [p for p in await adapter.fetch_open_positions() if p["symbol"] == symbol]
        if not pos:
            raise RuntimeError("the position does not appear in fetch_open_positions")
        return f"contracts {pos[0]['contracts']}"

    async def place_stop():
        o = await adapter.stop_market(symbol, "sell", state["amount"], state["price"] * 0.97)
        state["stop_id"] = str(o["id"])
        return f"id {o['id']} @ {state['price'] * 0.97:g}"

    async def find_stop():
        ids = [o["id"] for o in await adapter.fetch_stop_orders(symbol)]
        if state["stop_id"] not in ids:
            raise RuntimeError(f"stop {state['stop_id']} not found among open stop orders {ids}. The "
                               "protection checks depend on this call, so do not go live until it works.")

    async def move_stop():
        new = await adapter.stop_market(symbol, "sell", state["amount"], state["price"] * 0.975)
        await adapter.cancel_order(symbol, state["stop_id"])
        ids = [o["id"] for o in await adapter.fetch_stop_orders(symbol)]
        if ids != [str(new["id"])]:
            raise RuntimeError(f"expected only the new stop {new['id']}, found {ids}")
        state["stop_id"] = str(new["id"])

    async def cancel():
        await adapter.cancel_all(symbol)
        left = await adapter.fetch_stop_orders(symbol)
        if left:
            raise RuntimeError(f"stops still open after cancel_all: {left}")

    async def close():
        await adapter.market_order(symbol, "sell", state["amount"], reduce_only=True)
        state["opened"] = False
        left = [p for p in await adapter.fetch_open_positions() if p["symbol"] == symbol]
        if left:
            raise RuntimeError(f"position still open after the reduce-only close: {left}")

    try:
        say(f"Self-test on {symbol}, notional ~{notional:g} USDT, {leverage}x")
        ok = await step("market listed", markets)
        for name, fn in (("one-way position mode", mode), ("balance and equity", balance),
                         ("isolated margin and leverage", configure), ("order sizing", sizing),
                         ("market entry", entry), ("position visible", visible), ("place stop-loss", place_stop),
                         ("stop-loss discoverable", find_stop), ("move stop (new first, then cancel old)", move_stop),
                         ("cancel all stops", cancel), ("reduce-only close", close)):
            if not ok:
                break
            ok = await step(name, fn)
    finally:
        # Cleanup always runs: never leave a test position or stop behind.
        try:
            await adapter.cancel_all(symbol)
            leftover = [p for p in await adapter.fetch_open_positions() if p["symbol"] == symbol]
            for p in leftover:
                await adapter.market_order(symbol, "sell" if p["side"] == "long" else "buy",
                                           float(p["contracts"]), reduce_only=True)
            if leftover:
                say(f"  [CLEANUP] closed a leftover test position of {symbol}")
        except Exception as exc:
            say(f"  [CLEANUP FAILED] check {symbol} on Binance by hand: {type(exc).__name__}: {str(exc)[:200]}")
            results.append(StepResult("cleanup", False, str(exc)[:200]))
    return results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Binance futures order-path self-test")
    ap.add_argument("--mainnet", action="store_true", help="use the real exchange (default: testnet)")
    ap.add_argument("--yes-real-money", action="store_true", help="required with --mainnet")
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--notional", type=float, default=120.0, help="USDT size of the test order (Binance minimum is about 100)")
    ap.add_argument("--leverage", type=int, default=1)
    a = ap.parse_args(argv)

    key, secret = os.environ.get("AEGIS_SELFTEST_API_KEY"), os.environ.get("AEGIS_SELFTEST_API_SECRET")
    if not key or not secret:
        raise SystemExit("Set AEGIS_SELFTEST_API_KEY and AEGIS_SELFTEST_API_SECRET in the environment.")
    if a.mainnet and not a.yes_real_money:
        raise SystemExit("--mainnet places a REAL order. Add --yes-real-money to confirm.")

    from g_aegisbot.exchange import BinanceFuturesAdapter

    async def _go() -> list[StepResult]:
        adapter = BinanceFuturesAdapter(key, secret, testnet=not a.mainnet)
        try:
            await adapter.exchange.load_markets()
            print(f"Endpoint: {'MAINNET (real funds)' if a.mainnet else 'testnet'}")
            return await run_selftest(adapter, a.symbol, a.notional, a.leverage)
        finally:
            await adapter.close()

    results = asyncio.run(_go())
    failed = [r for r in results if not r.ok]
    print("\nRESULT: " + ("ALL PASSED" if not failed else f"{len(failed)} FAILED, do not trade live until fixed"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
