"""Background trading loop.

Two cadences run side by side:
  * candle loop: a few seconds after every closed candle, scan all pairs once (shared by all
    users), then for each user reconcile closed trades, trail stops, and open new trades.
  * risk loop: every minute, check each user's equity against the daily drawdown limit and
    (for paper accounts) test open positions against 1-minute candles for stop hits.

Run:  python -m g_aegisbot.engine.runner [--dry-run] [--once]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal as os_signal
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pandas as pd

from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT
from g_aegisbot.db.models import UserProfile
from g_aegisbot.engine import risk
from g_aegisbot.engine.controls import ControlState, ControlStore
from g_aegisbot.engine.paper import PaperExchange
from g_aegisbot.engine.store import TradeStore
from g_aegisbot.engine.strategy import (
    MultiPairScanner, Skip, StrategyParams, TrailingStop, execute_plan, plan_trade, rank_signals,
)
from g_aegisbot.exchange import BinanceFuturesAdapter, PositionModeError

log = logging.getLogger("aegis.runner")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_close(now: datetime, tf_seconds: int) -> datetime:
    """The next candle boundary after `now` (candles align to the Unix epoch, i.e. UTC)."""
    return datetime.fromtimestamp((int(now.timestamp()) // tf_seconds + 1) * tf_seconds, tz=timezone.utc)


class TradingRunner:
    def __init__(self, session_factory, box, params: StrategyParams = StrategyParams(), *, market=None,
                 dry_run: bool = False, settle_seconds: int = 5, risk_interval: int = 60,
                 drawdown_limit: float = DAILY_DRAWDOWN_LIMIT, adopt_unmanaged: bool = True, clock=_utcnow):
        self.sf, self.box, self.params = session_factory, box, params
        self.market, self.dry_run = market, dry_run
        self.settle_seconds, self.risk_interval = settle_seconds, risk_interval
        self.limit, self.clock = drawdown_limit, clock
        self.store = TradeStore(session_factory)
        self.controls = ControlStore(session_factory)
        self.adopt_unmanaged = adopt_unmanaged
        self.scanner: MultiPairScanner | None = None
        self.tf_seconds = ccxt.Exchange.parse_timeframe(params.timeframe)
        self._adapters: dict[str, tuple] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._universe: tuple[str, ...] = ()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        if self.market is None:
            self.market = BinanceFuturesAdapter(testnet=False)   # public data only, no keys
        self.scanner = MultiPairScanner(self.market, symbols=(), params=self.params)

    async def shutdown(self) -> None:
        for _, adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()
        if self.market is not None:
            await self.market.close()

    def stop(self) -> None:
        self._stop.set()

    def _load_profiles(self) -> list[UserProfile]:
        from sqlalchemy import select
        with self.sf() as s:
            profiles = list(s.scalars(select(UserProfile).order_by(UserProfile.id)))
        out = []
        for p in profiles:
            if p.trading_mode == "live" and not p.has_credentials:
                log.warning("[%s] skipped: live mode but no API credentials stored", p.user_id)
            else:
                out.append(p)
        return out

    def _save_tracker(self, user_id: str, tracker: dict) -> None:
        from sqlalchemy import select
        with self.sf() as s:
            p = s.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
            if p is not None:
                p.daily_loss_tracker = dict(tracker)
                s.commit()

    async def _adapter_for(self, profile: UserProfile):
        cached = self._adapters.get(profile.user_id)
        stamp = (profile.updated_at, profile.trading_mode)
        if cached and cached[0] == stamp:
            return cached[1]
        if cached:
            await cached[1].close()
        if profile.trading_mode == "testnet":
            adapter = PaperExchange(self.sf, profile.user_id, self.market,
                                    starting_balance=float(profile.virtual_balance), clock=self.clock)
        else:
            key = self.box.decrypt(profile.encrypted_api_key)
            secret = self.box.decrypt(profile.encrypted_api_secret)
            adapter = BinanceFuturesAdapter.from_profile(profile, key, secret)
            try:
                await adapter.exchange.load_markets()
                await adapter.assert_one_way_mode()
            except Exception:
                await adapter.close()
                raise
        self._adapters[profile.user_id] = (stamp, adapter)
        return adapter

    # ------------------------------------------------------------------ circuit breaker
    async def _check_breaker(self, profile: UserProfile, adapter) -> dict:
        now = self.clock()
        equity = await adapter.fetch_equity()
        tracker, event = risk.update_tracker(profile.daily_loss_tracker, equity, now, self.limit)
        if event or tracker != profile.daily_loss_tracker:
            await asyncio.to_thread(self._save_tracker, profile.user_id, tracker)
        profile.daily_loss_tracker = tracker
        if event == "tripped":
            log.warning(risk.trip_message(profile.user_id, tracker, equity, now, self.limit))
        elif event == "new_day":
            log.info(risk.new_day_message(profile.user_id, tracker))
        return tracker

    async def risk_pass(self) -> None:
        for profile in await asyncio.to_thread(self._load_profiles):
            async with self._locks[profile.user_id]:
                try:
                    adapter = await self._adapter_for(profile)
                except PositionModeError as exc:
                    log.error("[%s] %s", profile.user_id, exc)
                    continue
                except Exception:
                    log.exception("[%s] could not build exchange adapter", profile.user_id)
                    continue
                if isinstance(adapter, PaperExchange):
                    try:
                        for c in await adapter.process_stops():
                            log.info("[%s] paper stop-loss hit, closed at %.6g (pnl %.2f)",
                                     profile.user_id, c["average"], c["pnl"])
                    except Exception:
                        log.exception("[%s] paper stop check failed", profile.user_id)
                try:   # the drawdown check must run even if the stop check failed
                    await self._check_breaker(profile, adapter)
                except Exception:
                    log.exception("[%s] drawdown check failed", profile.user_id)
                try:
                    ctrl = await self._apply_controls(profile, adapter)
                    if not ctrl.flatten:
                        await self._ensure_protected(profile, adapter)
                except Exception:
                    log.exception("[%s] safety check failed", profile.user_id)

    # ------------------------------------------------------------------ candle cycle
    async def run_cycle(self, allow_entries: bool = True) -> None:
        profiles = await asyncio.to_thread(self._load_profiles)
        if not profiles:
            log.info("No eligible profiles; nothing to do.")
            return
        universe = tuple(sorted({c for p in profiles for c in p.active_coins}))
        if universe != self._universe:
            self.scanner.symbols = list(universe)
            dropped = await self.scanner.prepare()
            self._universe = universe
            if dropped:
                log.warning("Binance does not list these symbols, they are ignored: %s", dropped)
        results = await self.scanner.scan()
        signals = rank_signals(results)
        rejected = {}
        for r in results:
            if not r.signal:
                rejected[r.reason.split(" (")[0]] = rejected.get(r.reason.split(" (")[0], 0) + 1
        log.info("Scan done: %d pairs, %d signals %s; rejected: %s", len(results), len(signals),
                 [f"{s.symbol} {s.side} {s.conviction}" for s in signals], rejected)
        await asyncio.gather(*(self._manage_user(p, signals, allow_entries) for p in profiles))

    async def _manage_user(self, profile: UserProfile, signals, allow_entries: bool) -> None:
        async with self._locks[profile.user_id]:
            try:
                adapter = await self._adapter_for(profile)
                tracker = await self._check_breaker(profile, adapter)
                ctrl = await self._apply_controls(profile, adapter)
                if not ctrl.flatten:
                    await self._ensure_protected(profile, adapter)
                await self._reconcile_and_trail(profile, adapter)
                if not allow_entries:
                    return
                if ctrl.paused:
                    log.warning("[%s] TRADING PAUSED by the administrator (%s). No new trades will open. "
                                "Open positions keep their stop-loss orders.", profile.user_id, ctrl.describe())
                    return
                if not profile.subscription_is_active(self.clock()):
                    log.info("[%s] Subscription is not active: managing existing positions only, no new trades.",
                             profile.user_id)
                    return
                if tracker["breaker_tripped"]:
                    log.warning(risk.still_paused_message(profile.user_id, tracker, self.clock()))
                    return
                for sig in signals:
                    if sig.symbol in profile.active_coins:
                        await self._enter(profile, adapter, sig)
            except PositionModeError as exc:
                log.error("[%s] %s", profile.user_id, exc)
            except Exception:
                log.exception("[%s] cycle failed", profile.user_id)

    async def _enter(self, profile: UserProfile, adapter, sig) -> None:
        uid = profile.user_id
        plan = await plan_trade(profile, adapter, sig, self.params)
        if isinstance(plan, Skip):
            log.info("[%s] skipped %s %s: %s", uid, sig.symbol, sig.side, plan.reason)
            return
        note = " (size cut to respect the per-trade risk limit)" if plan.risk_capped else ""
        desc = (f"{plan.side.upper()} {plan.amount} {plan.symbol} @ ~{plan.entry_price:.6g}, stop {plan.stop_price:.6g}, "
                f"{plan.leverage}x, margin ${plan.margin:,.2f}, risk ${plan.risk_usdt:,.2f}, {plan.conviction}{note}")
        if self.dry_run:
            log.info("[%s] DRY RUN, would open %s", uid, desc)
            return
        try:
            res = await execute_plan(adapter, plan)
        except Exception:
            log.exception("[%s] order failed for %s", uid, plan.symbol)
            try:   # a timeout can hide a fill; make sure any resulting position has a stop
                await self._ensure_protected(profile, adapter)
            except Exception:
                log.exception("[%s] could not verify protection after the failed order", uid)
            return
        fill = float(res["entry"].get("average") or plan.entry_price)
        await asyncio.to_thread(
            self.store.open, user_id=uid, symbol=plan.symbol, side=plan.side, entry_price=fill,
            stop_price=plan.stop_price, stop_order_id=res.get("stop_order_id"), leverage=plan.leverage,
            mode=profile.trading_mode, opened_at=self.clock())
        log.info("[%s] OPENED %s", uid, desc)

    # ------------------------------------------------------------------ trailing-stop monitor
    async def _reconcile_and_trail(self, profile: UserProfile, adapter) -> None:
        uid = profile.user_id
        trades = await asyncio.to_thread(self.store.list_open, uid)
        if not trades:
            return
        positions = {p["symbol"]: p for p in await adapter.fetch_open_positions()}
        tf = f"{self.tf_seconds}s"
        for t in trades:
            pos = positions.get(t.symbol)
            if pos is None:
                await self._on_closed(profile, adapter, t)
                continue
            ind = self.scanner.indicators.get(t.symbol)
            if ind is None:
                continue
            last = _aware(t.last_trail_ts) if t.last_trail_ts else None
            bars = ind[ind.index > pd.Timestamp(last)] if last else \
                ind[ind.index >= pd.Timestamp(_aware(t.opened_at)).floor(tf)]
            if bars.empty:
                continue
            trail = TrailingStop(t.side, t.stop_price, t.extreme_price, self.params.trail_atr_mult)
            moved = False
            for _, bar in bars.iterrows():
                moved |= trail.update(float(bar["high"]), float(bar["low"]), float(bar["atr"]))
            new_id = None
            if moved:
                new_id = await self._move_stop(profile, adapter, t, trail.stop_price, float(pos["contracts"]))
                if new_id is None:           # stop could not be moved; keep the old one
                    trail.stop_price = t.stop_price
            await asyncio.to_thread(
                self.store.update_trail, t.id, stop_price=trail.stop_price, extreme_price=trail.extreme_price,
                last_trail_ts=bars.index[-1].to_pydatetime(), stop_order_id=new_id)

    async def _move_stop(self, profile, adapter, t, new_stop: float, amount: float) -> str | None:
        """Place the new stop first, then cancel the old one, so the position is never unprotected."""
        uid, exit_side = profile.user_id, ("sell" if t.side == "buy" else "buy")
        if self.dry_run:
            log.info("[%s] DRY RUN, would move %s stop %.6g -> %.6g", uid, t.symbol, t.stop_price, new_stop)
            return None
        try:
            order = await adapter.stop_market(t.symbol, exit_side, amount, new_stop)
        except (ccxt.InvalidOrder, ccxt.ExchangeError) as exc:
            price = await adapter.fetch_last_price(t.symbol)
            crossed = price <= new_stop if t.side == "buy" else price >= new_stop
            if crossed:   # price already through the trailed level: exit now
                log.warning("[%s] %s price %.6g is through the trailed stop %.6g; closing at market",
                            uid, t.symbol, price, new_stop)
                await adapter.market_order(t.symbol, exit_side, amount, reduce_only=True)
            else:
                log.warning("[%s] could not move %s stop to %.6g (%s); old stop stays", uid, t.symbol, new_stop, exc)
            return None
        if t.stop_order_id and t.stop_order_id != order.get("id"):
            await adapter.cancel_order(t.symbol, t.stop_order_id)
        log.info("[%s] %s trailing stop %.6g -> %.6g", uid, t.symbol, t.stop_price, new_stop)
        return order.get("id")

    async def _on_closed(self, profile, adapter, t, reason: str = "closed on exchange (stop or manual)") -> None:
        uid = profile.user_id
        if t.mode == "live":
            await adapter.cancel_all(t.symbol)          # remove any leftover stop order
            await asyncio.to_thread(
                self.store.log_closed, user_id=uid, symbol=t.symbol, side=t.side, amount=0.0,
                entry_price=t.entry_price, exit_price=None, pnl=None, fees=0.0,
                opened_at=_aware(t.opened_at), exit_reason=reason, mode="live")
        log.info("[%s] %s position is closed; removing its trailing-stop state", uid, t.symbol)
        await asyncio.to_thread(self.store.remove, t.id)

    # ------------------------------------------------------------------ kill switch and protection
    async def _apply_controls(self, profile: UserProfile, adapter) -> ControlState:
        ctrl = await asyncio.to_thread(self.controls.effective, profile.user_id)
        if ctrl.flatten:
            await self._flatten(profile, adapter, ctrl)
        return ctrl

    async def _flatten(self, profile: UserProfile, adapter, ctrl: ControlState) -> None:
        """Emergency exit: cancel stops and close every position the bot manages for this user."""
        uid = profile.user_id
        trades = {t.symbol: t for t in await asyncio.to_thread(self.store.list_open, uid)}
        positions = [p for p in await adapter.fetch_open_positions()
                     if p["symbol"] in trades or p["symbol"] in profile.active_coins]
        if not positions and not trades:
            return
        log.warning("[%s] EMERGENCY FLATTEN (%s): closing %d open position(s) at market and cancelling their stops.",
                    uid, ctrl.describe(), len(positions))
        if self.dry_run:
            return
        for pos in positions:
            exit_side = "sell" if pos["side"] == "long" else "buy"
            try:
                await adapter.cancel_all(pos["symbol"])
                await adapter.market_order(pos["symbol"], exit_side, float(pos["contracts"]), reduce_only=True)
            except Exception:
                log.exception("[%s] could not close %s; will retry on the next check", uid, pos["symbol"])
        still_open = {p["symbol"] for p in await adapter.fetch_open_positions()}
        for sym, t in trades.items():
            if sym not in still_open:
                await self._on_closed(profile, adapter, t, reason="emergency flatten")

    def _emergency_stop(self, symbol: str, side: str, entry: float) -> float:
        """Stop for a position that has none: 2 x ATR if candles are loaded, otherwise 3% from entry."""
        ind = self.scanner.indicators.get(symbol) if self.scanner else None
        dist = entry * 0.03
        if ind is not None and len(ind) and float(ind["atr"].iloc[-1]) > 0:
            dist = self.params.stop_atr_mult * float(ind["atr"].iloc[-1])
        dist = min(dist, entry * self.params.max_stop_pct)
        return entry - dist if side == "buy" else entry + dist

    async def _ensure_protected(self, profile: UserProfile, adapter) -> None:
        """Every position the bot manages must have a live exchange-side stop and a tracking record.
        Repairs the cases a crash or timeout leaves behind: a position with no stop, a missing stop
        order, or a position with no record. Positions in symbols the bot does not trade are ignored,
        so use a dedicated Binance account (or sub-account) for the bot."""
        uid = profile.user_id
        positions = {p["symbol"]: p for p in await adapter.fetch_open_positions()}
        if not positions:
            return
        trades = {t.symbol: t for t in await asyncio.to_thread(self.store.list_open, uid)}
        for sym, pos in positions.items():
            t = trades.get(sym)
            if t is None and (not self.adopt_unmanaged or sym not in profile.active_coins):
                continue
            side = "buy" if pos["side"] == "long" else "sell"
            exit_side = "sell" if side == "buy" else "buy"
            amount, entry = float(pos["contracts"]), float(pos.get("entryPrice") or 0)
            stops = await adapter.fetch_stop_orders(sym)
            best = (max if side == "buy" else min)(stops, key=lambda o: o["stopPrice"]) if stops else None

            if t is not None and best is not None:
                if best["id"] != t.stop_order_id:            # keep our record in step with the exchange
                    await asyncio.to_thread(self.store.update_trail, t.id, stop_price=best["stopPrice"],
                                            extreme_price=t.extreme_price, last_trail_ts=t.last_trail_ts,
                                            stop_order_id=best["id"])
                continue
            if t is None and best is not None:               # has a stop but no record: adopt it
                log.warning("[%s] SAFETY: found %s position without a tracking record; adopting its existing stop "
                            "at %.6g.", uid, sym, best["stopPrice"])
                if not self.dry_run:
                    await asyncio.to_thread(
                        self.store.open, user_id=uid, symbol=sym, side=side, entry_price=entry,
                        stop_price=best["stopPrice"], stop_order_id=best["id"], leverage=profile.leverage,
                        mode=profile.trading_mode, opened_at=self.clock())
                continue

            stop = t.stop_price if t is not None else self._emergency_stop(sym, side, entry)
            why = "its stop-loss order was missing" if t is not None else "it had no stop-loss and no tracking record"
            log.warning("[%s] SAFETY: %s %s position is unprotected (%s). Placing a stop-loss at %.6g.",
                        uid, sym, pos["side"], why, stop)
            if self.dry_run:
                continue
            price = await adapter.fetch_last_price(sym)
            if (price <= stop) if side == "buy" else (price >= stop):
                log.warning("[%s] SAFETY: %s price %.6g is already through the stop %.6g; closing at market.",
                            uid, sym, price, stop)
                await adapter.market_order(sym, exit_side, amount, reduce_only=True)
                if t is not None:
                    await self._on_closed(profile, adapter, t, reason="closed by safety check (price through stop)")
                continue
            try:
                order = await adapter.stop_market(sym, exit_side, amount, stop)
            except Exception:
                log.exception("[%s] SAFETY: could not place a stop on %s; closing the position instead", uid, sym)
                await adapter.market_order(sym, exit_side, amount, reduce_only=True)
                if t is not None:
                    await self._on_closed(profile, adapter, t, reason="closed by safety check (no stop possible)")
                continue
            if t is None:
                await asyncio.to_thread(
                    self.store.open, user_id=uid, symbol=sym, side=side, entry_price=entry, stop_price=stop,
                    stop_order_id=order.get("id"), leverage=profile.leverage, mode=profile.trading_mode,
                    opened_at=self.clock())
            else:
                await asyncio.to_thread(self.store.update_trail, t.id, stop_price=stop,
                                        extreme_price=t.extreme_price, last_trail_ts=t.last_trail_ts,
                                        stop_order_id=order.get("id"))

    # ------------------------------------------------------------------ scheduling
    async def _sleep_until(self, target: datetime) -> None:
        delay = (target - self.clock()).total_seconds()
        if delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("Unhandled error; the loop continues")

    async def _risk_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep_until(self.clock() + timedelta(seconds=self.risk_interval))
            if not self._stop.is_set():
                await self._safe(self.risk_pass())

    async def run_once(self, allow_entries: bool = True) -> None:
        await self.start()
        try:
            await self.run_cycle(allow_entries)
        finally:
            await self.shutdown()

    async def run_forever(self) -> None:
        await self.start()
        risk_task = asyncio.create_task(self._risk_loop())
        try:
            await self._safe(self.run_cycle(allow_entries=False))   # catch up on stops/closures after a restart
            while not self._stop.is_set():
                await self._sleep_until(next_close(self.clock(), self.tf_seconds) + timedelta(seconds=self.settle_seconds))
                if self._stop.is_set():
                    break
                await self._safe(self.run_cycle())
        finally:
            risk_task.cancel()
            await self.shutdown()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="G-Aegisbot trading loop")
    ap.add_argument("--dry-run", action="store_true", help="log what would be traded, place no orders")
    ap.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from g_aegisbot.db import get_session_factory, init_db
    from g_aegisbot.security.crypto import SecretBox

    init_db()
    runner = TradingRunner(get_session_factory(), SecretBox.from_env(), dry_run=args.dry_run)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            loop.add_signal_handler(sig, runner.stop)
        await (runner.run_once() if args.once else runner.run_forever())

    asyncio.run(_main())


if __name__ == "__main__":
    main()
