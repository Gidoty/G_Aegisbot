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
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pandas as pd

from g_aegisbot import audit
from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.trading_models import EquitySnapshot, Heartbeat
from g_aegisbot.engine import livegate, risk
from g_aegisbot.engine.controls import ControlState, ControlStore
from g_aegisbot.engine import strategies as strat
from g_aegisbot.engine.paper import PaperExchange
from g_aegisbot.engine.protection import ProtectionError, assess_stops
from g_aegisbot.engine import journal as jr
from g_aegisbot.engine.journal import PendingEntry
from g_aegisbot.engine.reconcile import decide, gather_evidence
from g_aegisbot.services import KEYS_UNREADABLE, keys_verification_problem
from g_aegisbot.engine.runlock import (
    EXIT_CODE_ALREADY_RUNNING, RunnerAlreadyRunning, RunnerLock, RunnerLockError, RunnerLockLost,
)
from g_aegisbot.engine.store import TradeStore
from g_aegisbot.engine.strategy import (
    MultiPairScanner, OrderNotFilled, OrderUncertain, ProtectionFailure, Skip, StrategyParams, TrailingStop,
    classify_entry, execute_plan, plan_trade, rank_signals,
)
from g_aegisbot.exchange import BinanceFuturesAdapter, PositionModeError
from g_aegisbot.exchange.binance_futures import parse_position_qty

log = logging.getLogger("aegis.runner")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_close(now: datetime, tf_seconds: int) -> datetime:
    """The next candle boundary after `now` (candles align to the Unix epoch, i.e. UTC)."""
    return datetime.fromtimestamp((int(now.timestamp()) // tf_seconds + 1) * tf_seconds, tz=timezone.utc)


@dataclass
class CloseResult:
    """What the EXCHANGE shows after a close attempt, never what the order call claimed.
    closed: position is zero | partial: some left | open: nothing closed | unknown: position could not be read."""
    status: str
    remaining: float | None
    detail: str = ""
    cleaned: bool = False          # closed AND every protective order verified cancelled


class TradingRunner:
    def __init__(self, session_factory, box, params: StrategyParams = StrategyParams(), *, market=None,
                 dry_run: bool = False, settle_seconds: int = 5, risk_interval: int = 60,
                 drawdown_limit: float = DAILY_DRAWDOWN_LIMIT, adopt_unmanaged: bool = True, clock=_utcnow,
                 sleep=asyncio.sleep, evidence_margin_seconds: int = 60, lock: RunnerLock | None = None,
                 live_gate=None):
        self.sf, self.box, self.params = session_factory, box, params
        self.live_gate = live_gate                 # None: engine/livegate.evaluate (looked up at call time). A parameter for tests only
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
        self._last_snapshot: dict[str, datetime] = {}
        self._last_prune: str | None = None
        self._unverified: dict[str, str] = {}      # user -> why exchange state could not be verified; entries blocked
        self._pending: dict[str, dict[str, PendingEntry]] = defaultdict(dict)   # user -> unresolved entry orders
        self.sleep = sleep
        # How far before the recorded send time the exchange history is searched (clock differences). Elapsed time never
        # releases an unresolved entry; only exchange evidence does (engine/reconcile.py).
        self.evidence_margin = timedelta(seconds=evidence_margin_seconds)
        self.lock = lock                           # single-runner lock: taken in start(), held until shutdown()
        self._cleanup_debt: dict[str, set[str]] = defaultdict(set)   # user -> flat symbols whose old stops are not proven cancelled
        self._journal_loaded: set[str] = set()     # users whose durable order journal was read since this process started
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        if self.lock is not None:
            self.lock.acquire()                    # first: a second runner must refuse before it touches anything
        if self.market is None:
            self.market = BinanceFuturesAdapter(testnet=False)   # public data only, no keys
        self.scanner = MultiPairScanner(self.market, symbols=(), params=self.params, clock=self.clock)

    async def shutdown(self) -> None:
        try:
            for _, adapter in self._adapters.values():
                await adapter.close()
            self._adapters.clear()
            if self.market is not None:
                await self.market.close()
        finally:
            if self.lock is not None:
                self.lock.release()

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
            else:                      # key verification gates NEW entries only (_enter); existing positions keep being managed
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
        elif profile.trading_mode == "live":
            key = self.box.decrypt(profile.encrypted_api_key)
            secret = self.box.decrypt(profile.encrypted_api_secret)
            adapter = BinanceFuturesAdapter.from_profile(profile, key, secret)
            try:
                await adapter.exchange.load_markets()
                await adapter.assert_one_way_mode()
            except Exception:
                await adapter.close()
                raise
        else:                                         # fail closed: an unexpected mode never becomes paper or live execution
            raise ValueError(f"[{profile.user_id}] unknown trading mode {profile.trading_mode!r}: expected 'live' or 'testnet'")
        self._adapters[profile.user_id] = (stamp, adapter)
        return adapter

    async def _adapter_or_alert(self, profile: UserProfile):
        """The adapter for this profile, or None after a loud failure. A live account whose exchange access fails (rejected
        credentials, network, hedge mode) has positions and stops nobody can confirm, so it is marked UNVERIFIED (CRITICAL
        log, new entries blocked) and never treated as protected."""
        uid = profile.user_id
        try:
            return await self._adapter_for(profile)
        except PositionModeError as exc:
            log.error("[%s] %s", uid, exc)
            if profile.trading_mode == "live":
                self._mark_unverified(uid, f"the account's position mode is not one-way, so its positions and stops cannot be "
                                           f"verified ({exc})")
        except Exception as exc:
            log.exception("[%s] could not build exchange adapter", uid)
            if profile.trading_mode == "live":
                self._mark_unverified(uid, f"the exchange cannot be reached with this account's credentials "
                                           f"({type(exc).__name__}); its open positions and protective stops cannot be verified")
        return None

    def _live_gate_problem(self, profile: UserProfile) -> str | None:
        """Why the live-trading gate refuses a NEW live entry for this profile, or None. Any error is a refusal."""
        try:
            res = (self.live_gate or livegate.evaluate)(params=self.params, drawdown_limit=self.limit, profile=profile,
                                 lock_held=self.lock is not None, now=self.clock())
            return None if res.allowed else "; ".join(res.problems) or "the live gate refused without a reason"
        except Exception as exc:
            return f"the live gate failed safely ({type(exc).__name__})"

    def _entry_keys_problem(self, uid: str) -> str | None:
        """Why a NEW live entry is not allowed for this user right now (API-key verification missing, stale, future-dated
        or unreadable), judged from a fresh read of the database and the runner's clock. A failed read is a problem."""
        from sqlalchemy import select
        try:
            with self.sf() as s:
                row = s.scalar(select(UserProfile).where(UserProfile.user_id == uid))
                if row is None:
                    return "the profile could not be found"
                return keys_verification_problem(row, self.clock())
        except Exception:
            return KEYS_UNREADABLE

    # ------------------------------------------------------------------ circuit breaker
    async def _check_breaker(self, profile: UserProfile, adapter) -> dict:
        now = self.clock()
        equity = await adapter.fetch_equity()
        tracker, event = risk.update_tracker(profile.daily_loss_tracker, equity, now, self.limit)
        if event or tracker != profile.daily_loss_tracker:
            await asyncio.to_thread(self._save_tracker, profile.user_id, tracker)
        profile.daily_loss_tracker = tracker
        await self._record_equity(profile.user_id, equity, now)
        if event == "tripped":
            log.warning(risk.trip_message(profile.user_id, tracker, equity, now, self.limit))
        elif event == "new_day":
            log.info(risk.new_day_message(profile.user_id, tracker))
        return tracker

    # ------------------------------------------------------------------ dashboard data
    def _write_snapshot(self, uid: str, equity: float, now: datetime, prune: bool) -> None:
        from sqlalchemy import delete
        with self.sf() as s:
            s.add(EquitySnapshot(user_id=uid, at=now, equity=equity))
            if prune:
                s.execute(delete(EquitySnapshot).where(EquitySnapshot.at < now - timedelta(days=90)))
            s.commit()

    async def _record_equity(self, uid: str, equity: float, now: datetime) -> None:
        """Sample equity at most every 5 minutes per user; keep 90 days."""
        last = self._last_snapshot.get(uid)
        if last is not None and (now - last).total_seconds() < 300:
            return
        self._last_snapshot[uid] = now
        today = risk.utc_date(now)
        prune, self._last_prune = self._last_prune != today, today
        await asyncio.to_thread(self._write_snapshot, uid, float(equity), now, prune)

    def _write_heartbeat(self, info: str) -> None:
        with self.sf() as s:
            row = s.get(Heartbeat, "runner") or Heartbeat(name="runner", at=self.clock())
            row.at, row.info = self.clock(), info[:200]
            s.merge(row)
            s.commit()

    async def _beat(self, info: str) -> None:
        try:
            await asyncio.to_thread(self._write_heartbeat, info)
        except Exception:
            log.exception("could not write the heartbeat")

    async def risk_pass(self) -> None:
        if self.lock is not None:
            try:
                self.lock.check()
            except RunnerLockLost as exc:
                log.critical("single-runner lock lost (%s); stopping so two runners cannot overlap", exc)
                self.stop()
                return
        for profile in await asyncio.to_thread(self._load_profiles):
            async with self._locks[profile.user_id]:
                await self._load_journal(profile.user_id)
                adapter = await self._adapter_or_alert(profile)
                if adapter is None:
                    continue
                if isinstance(adapter, PaperExchange) and not self.dry_run:      # closing paper positions IS a state change
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
                        if await self._verify_exchange_state(profile, adapter):
                            await self._reconcile_pending(profile, adapter)
                            await self._settle_cleanup(profile, adapter)
                except Exception:
                    log.exception("[%s] safety check failed", profile.user_id)
        await self._beat("risk pass ok")

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
        signals = rank_signals(results, self.params.strategy_priority)
        for d in self.scanner.decisions:
            if not d.kept:
                log.info("signal not used: %s %s [%s] %s", d.symbol, d.side, d.strategy, d.reason)
        rejected = {}
        for r in results:
            if not r.signal:
                rejected[r.reason.split(" (")[0]] = rejected.get(r.reason.split(" (")[0], 0) + 1
        log.info("Scan done: %d pairs, %d signals %s; rejected: %s", len(results), len(signals),
                 [f"{s.symbol} {s.side} {s.conviction}" for s in signals], rejected)
        await asyncio.gather(*(self._manage_user(p, signals, allow_entries) for p in profiles))
        await self._beat(f"candle cycle ok, {len(signals)} signal(s)")

    async def _manage_user(self, profile: UserProfile, signals, allow_entries: bool) -> None:
        async with self._locks[profile.user_id]:
            try:
                await self._load_journal(profile.user_id)
                adapter = await self._adapter_or_alert(profile)
                if adapter is None:
                    return
                tracker = await self._check_breaker(profile, adapter)
                ctrl = await self._apply_controls(profile, adapter)
                if not ctrl.flatten:
                    if not await self._verify_exchange_state(profile, adapter):
                        return                      # cannot confirm positions and stops: do nothing further, open nothing
                    await self._reconcile_pending(profile, adapter)
                    await self._settle_cleanup(profile, adapter)
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
                if profile.trading_mode == "live" and (why := await asyncio.to_thread(self._entry_keys_problem, profile.user_id)):
                    log.critical("[%s] NEW LIVE ENTRIES BLOCKED: %s. Existing positions keep being managed. Verify the keys "
                                 "again (set-keys --verify) to allow new entries.", profile.user_id, why)
                    return
                if profile.trading_mode == "live" and (why := await asyncio.to_thread(self._live_gate_problem, profile)):
                    log.critical("[%s] NEW LIVE ENTRIES BLOCKED BY THE LIVE GATE: %s. Existing positions keep being managed.",
                                 profile.user_id, why)
                    return
                for sig in signals:
                    blocked = self._entries_blocked(profile.user_id)
                    if blocked:
                        log.critical("[%s] NEW ENTRIES BLOCKED: %s", profile.user_id, blocked)
                        return
                    if sig.symbol in profile.active_coins:
                        if not await self._enter(profile, adapter, sig):
                            log.critical("[%s] entry processing stopped for this cycle: %s", profile.user_id,
                                         self._entries_blocked(profile.user_id) or "an entry failed in a way that could "
                                         "leave exposure; the remaining signals are not processed")
                            return
            except PositionModeError as exc:
                log.error("[%s] %s", profile.user_id, exc)
            except Exception:
                log.exception("[%s] cycle failed", profile.user_id)

    # ---- durable journal of entry orders (the audit table; no schema change). An "entry_intent" row is written BEFORE an
    # order can be sent and an "entry_resolved" row once the outcome is established, so a crash or restart in between
    # leaves an unresolved intent that the next process finds and reconciles.
    async def _journal(self, uid: str, action: str, client_id: str, detail: str) -> None:
        await asyncio.to_thread(audit.record, self.sf, uid, action, client_id, detail, self.clock())

    def _read_unresolved_intents(self, uid: str) -> list[PendingEntry]:
        """Every open intent of this user, however old; damaged records block (see engine/journal.py)."""
        return [pe for _, pe in jr.read_unresolved(self.sf, uid)]

    def _journal_state(self, client_id: str, uid: str | None = None) -> tuple[bool, bool]:
        return jr.state_of(self.sf, client_id, uid)

    async def _load_journal(self, uid: str) -> None:
        """Once per process and user: any entry order that was sent but never resolved goes back into the pending set.
        If the journal cannot be read, entries stay blocked and the read is retried next time."""
        if uid in self._journal_loaded:
            return
        try:
            found = await asyncio.to_thread(self._read_unresolved_intents, uid)
        except Exception as exc:
            # Not stored in _unverified (a later successful stop check would clear that). _entries_blocked refuses
            # entries for as long as the journal has not been read.
            log.critical("[%s] the order journal could not be read (%s); an unresolved entry order cannot be ruled out, "
                         "so new entries stay blocked and the read is retried", uid, type(exc).__name__)
            return
        for pe in found:
            self._pending[uid].setdefault(pe.client_id, pe)
            log.critical("[%s] unresolved entry order %s on %s found in the journal; entries stay blocked until the "
                         "exchange confirms what happened to it", uid, pe.client_id, pe.symbol)
        self._journal_loaded.add(uid)

    def _entries_blocked(self, uid: str) -> str | None:
        """Why new entries must not open for this user right now, or None. Unverified state or any unreconciled
        entry order blocks them."""
        if uid in self._unverified:
            return self._unverified[uid]
        if uid not in self._journal_loaded:
            return "the order journal has not been read, so an unresolved entry order cannot be ruled out"
        if self._pending.get(uid):
            ids = ", ".join(sorted(self._pending[uid]))
            return f"entry order(s) not yet reconciled with the exchange ({ids})"
        if self._cleanup_debt.get(uid):
            return ("old protective orders could not be proven cancelled on " + ", ".join(sorted(self._cleanup_debt[uid]))
                    + "; a leftover reduce-only stop could later close a new position")
        return None

    async def _resolve_journal(self, uid: str, client_id: str | None, how: str, symbol: str | None = None) -> None:
        """Append the resolution of this user's intent, bound to the original intent row, user and symbol, at most once."""
        if not client_id or self.dry_run:
            return
        try:
            intent = await asyncio.to_thread(jr.find_intent, self.sf, uid, client_id)
            if intent is None:
                log.error("[%s] cannot resolve order %s: this user has no such intent in the journal", uid, client_id)
                return
            wrote = await asyncio.to_thread(
                lambda: jr.resolve_once(self.sf, intent, symbol=symbol or intent.detail or "?", actor=uid, body=how, at=self.clock()))
            if not wrote:
                log.warning("[%s] order %s was already resolved; not writing a second resolution", uid, client_id)
        except Exception:
            # not fatal for safety: an unresolved row only makes the next start re-check this order, which is harmless
            log.exception("[%s] could not write the resolution of order %s to the journal", uid, client_id)

    def _register_pending(self, uid: str, symbol: str, client_id: str | None, detail: str, rejected: bool = False) -> None:
        if client_id:
            existing = self._pending[uid].get(client_id)
            # keep the journal's original send time when the intent is already known; the evidence window starts there
            sent = existing.submitted_at if existing else self.clock()
            self._pending[uid][client_id] = PendingEntry(symbol, client_id, sent, detail, rejected=rejected)

    async def _enter(self, profile: UserProfile, adapter, sig) -> bool:
        """Try to open one position. Returns True when the next signal may be processed, False when processing must
        stop for this cycle because the attempt could have left exposure that is not yet confirmed safe."""
        uid = profile.user_id
        if profile.trading_mode == "live" and (why := await asyncio.to_thread(self._entry_keys_problem, uid)):
            # the last check before anything can be sent; it does not depend on the profile row loaded earlier or on any cache
            log.critical("[%s] live entry %s %s REFUSED: %s", uid, sig.symbol, sig.side, why)
            return False
        if profile.trading_mode == "live" and (why := await asyncio.to_thread(self._live_gate_problem, profile)):
            log.critical("[%s] live entry %s %s REFUSED BY THE LIVE GATE: %s", uid, sig.symbol, sig.side, why)
            return False
        open_trades = await asyncio.to_thread(self.store.list_open, uid)
        plan = await plan_trade(profile, adapter, sig, self.params, trades=open_trades)
        if isinstance(plan, Skip):
            log.info("[%s] skipped %s %s: %s", uid, sig.symbol, sig.side, plan.reason)
            return True
        note = f" (size cut by the {plan.risk_note})" if plan.risk_capped else ""
        desc = (f"[{plan.strategy}] {plan.side.upper()} {plan.amount} {plan.symbol} @ ~{plan.entry_price:.6g}, "
                f"stop {plan.stop_price:.6g}, {plan.leverage}x, margin ${plan.margin:,.2f}, risk ${plan.risk_usdt:,.2f}, "
                f"{plan.conviction}{note}")
        if self.dry_run:
            log.info("[%s] DRY RUN, would open %s", uid, desc)
            return True
        async def write_ahead(client_id: str) -> None:
            had_intent, _ = await asyncio.to_thread(self._journal_state, client_id)
            if had_intent:                       # an id is used once, ever; reusing one would blur two orders into one
                raise RuntimeError(f"client order id {client_id} is already in the journal; refusing to reuse it")
            await self._journal(uid, "entry_intent", client_id, plan.symbol)

        try:
            res = await execute_plan(adapter, plan, sleep=self.sleep, before_send=write_ahead)
        except OrderNotFilled as exc:
            # Whatever the exchange said at that moment (a terminal status, a rejection), the entry is released only by the
            # same central policy used for recovery: complete evidence from every source (engine/reconcile.py).
            self._register_pending(uid, plan.symbol, exc.client_id, str(exc), rejected=exc.rejected)
            return await self._settle_failed_entry(profile, adapter, exc.client_id, str(exc))
        except OrderUncertain as exc:
            # Outcome unknown and not resendable. Block entries until reconciled by client id.
            self._register_pending(uid, plan.symbol, exc.client_id, str(exc))
            return await self._after_unconfirmed(profile, adapter, f"{exc}. Positions and orders will be re-read "
                                                 "before any new trade.")
        except ProtectionFailure as exc:
            self._register_pending(uid, plan.symbol, exc.client_id, exc.detail)
            return await self._after_unconfirmed(profile, adapter, f"PROTECTION FAILURE: {exc}")
        except Exception as exc:
            log.exception("[%s] order failed for %s", uid, plan.symbol)
            return await self._after_unconfirmed(profile, adapter, f"entry for {plan.symbol} failed with "
                                                 f"{type(exc).__name__}; exposure cannot be ruled out until verified")
        fill = float(res["entry"].get("average") or plan.entry_price)
        await asyncio.to_thread(
            self.store.open, user_id=uid, symbol=plan.symbol, side=plan.side, entry_price=fill,
            stop_price=plan.stop_price, stop_order_id=res.get("stop_order_id"), leverage=plan.leverage,
            mode=profile.trading_mode, opened_at=self.clock(), strategy=plan.strategy,
            trail_atr_mult=plan.trail_atr_mult)
        log.info("[%s] OPENED %s%s", uid, desc, " PARTIAL FILL" if res.get("partial") else "")
        if not res.get("outstanding"):
            await self._resolve_journal(uid, res.get("client_order_id"), "filled; stop visible and covering the position",
                                        symbol=plan.symbol)
        if res.get("outstanding"):
            # part of the order filled and is protected; the rest is still working. Do not open anything else until
            # the remainder is cancelled or filled and the stop covers the final quantity.
            self._register_pending(uid, plan.symbol, res.get("client_order_id"),
                                   "entry partly filled and still working")
            return await self._after_unconfirmed(profile, adapter, f"{plan.symbol}: entry partly filled and still "
                                                 "working; the remainder is being reconciled")
        return True

    async def _settle_failed_entry(self, profile: UserProfile, adapter, client_id: str | None, why: str) -> bool:
        """After an entry that did not open a position: run the central reconciliation. Processing of further signals
        continues only if THAT policy released the order and nothing else blocks entries."""
        uid = profile.user_id
        if not client_id:
            return await self._after_unconfirmed(profile, adapter, f"{why}. The order has no client id to reconcile.")
        try:
            if await self._verify_exchange_state(profile, adapter):
                await self._reconcile_pending(profile, adapter)
        except Exception:
            log.exception("[%s] reconciliation after a failed entry raised; entries stay blocked", uid)
        if client_id not in self._pending.get(uid, {}) and not self._entries_blocked(uid):
            log.warning("[%s] entry did not open a position and the exchange evidence agrees: %s", uid, why)
            return True
        log.critical("[%s] entry did not open a position, but the evidence does not yet prove it (%s); entries are blocked: %s",
                     uid, why, self._entries_blocked(uid) or "unresolved order")
        return False

    async def _after_unconfirmed(self, profile: UserProfile, adapter, reason: str) -> bool:
        """Mark the account unverified, immediately re-read positions, stops and the pending order, and tell the caller
        to stop processing entries this cycle. Entries resume only when verification AND reconciliation both pass."""
        uid = profile.user_id
        self._unverified[uid] = reason
        log.critical("[%s] %s", uid, reason)
        try:
            await self._verify_exchange_state(profile, adapter)
            await self._reconcile_pending(profile, adapter)
        except Exception:
            log.exception("[%s] verification after an unconfirmed entry failed; entries stay blocked", uid)
        return False

    async def _position_state(self, adapter, symbol: str, attempts: int = 3) -> tuple[bool, float, str | None]:
        """(readable, quantity, side) of the exchange position for one symbol. readable=False if every read failed."""
        for i in range(attempts):
            try:
                for p in await adapter.fetch_open_positions([symbol]):
                    size = parse_position_qty(p)           # malformed / inconsistent entries raise: unreadable, never flat
                    if p.get("symbol") == symbol and size > 0:
                        return True, size, p.get("side")
                return True, 0.0, None
            except Exception as exc:
                log.warning("could not read the %s position (%s), attempt %d/%d", symbol, type(exc).__name__, i + 1, attempts)
                if i + 1 < attempts:
                    await self.sleep(0.5 * 2 ** i)
        return False, 0.0, None

    async def _reconcile_pending(self, profile: UserProfile, adapter) -> None:
        """Settle entry orders whose outcome was unknown, using their original client order ids. An order is released
        only on exchange evidence of a terminal outcome (_resolve_pending), or because an operator resolved it through
        the audited resolve-entry command. Elapsed time, "not found" answers and empty listings release nothing."""
        uid = profile.user_id
        pending = self._pending.get(uid)
        if not pending:
            return
        for cid, pe in list(pending.items()):
            try:
                _, by_operator = await asyncio.to_thread(self._journal_state, cid, uid)
                if by_operator:
                    del pending[cid]
                    log.warning("[%s] entry order %s on %s was resolved in the journal (operator or earlier run)",
                                uid, cid, pe.symbol)
                    continue
                resolved, why = await self._resolve_pending(profile, adapter, pe)
            except Exception as exc:
                log.warning("[%s] could not reconcile order %s yet (%s); entries stay blocked", uid, cid, type(exc).__name__)
                continue
            if resolved and self.dry_run:
                log.warning("[%s] DRY RUN: entry order %s on %s WOULD be reconciled (%s); nothing was changed, no "
                            "resolution was written", uid, cid, pe.symbol, why)
            elif resolved:
                del pending[cid]
                await self._resolve_journal(uid, cid, why[:300], symbol=pe.symbol)
                log.warning("[%s] entry order %s on %s reconciled: %s", uid, cid, pe.symbol, why)
            else:
                log.warning("[%s] entry order %s on %s is still unresolved: %s", uid, cid, pe.symbol, why)
        if not pending:
            self._pending.pop(uid, None)

    async def _position_is_protected(self, profile: UserProfile, adapter, symbol: str) -> bool:
        """Repair if needed, then confirm from the exchange that the symbol's position has a valid full-size stop."""
        await self._ensure_protected(profile, adapter)
        readable, qty, side = await self._position_state(adapter, symbol)
        if not readable:
            return False
        if qty == 0:
            return True
        return assess_stops(await adapter.fetch_stop_orders(symbol), position_side=side, position_qty=qty).best is not None

    async def _resolve_pending(self, profile: UserProfile, adapter, pe: PendingEntry) -> tuple[bool, str]:
        """(True, reason) only when the exchange shows a TERMINAL outcome for this exact order:

          filled    terminal with a fill: released once the position is flat or confirmed protected by a valid
                    full-size stop;
          unfilled  terminal with zero fill, a flat position, and a complete trade history with no fills.

        Anything else stays blocked: an order still working, an error or timeout on any source, an incomplete page set,
        a position that exists, an adapter that cannot look orders up, a damaged journal record. "No such order" is never
        a release, however often it is repeated, and neither is time or an empty open-orders listing. A not-found order
        with every source complete and clean is reported to the log as awaiting an operator (resolve-entry)."""
        if pe.corrupt:
            return False, f"{pe.detail}; an operator must resolve it (resolve-entry --symbol ...)"
        ev = await gather_evidence(adapter, pe.symbol, pe.client_id, pe.submitted_at, self.clock(), self.evidence_margin,
                                   create_rejected=pe.rejected)
        if ev.lookup_state == "found" and classify_entry(ev.order)[0] == "pending":     # still working: cancel the rest
            canceller = getattr(adapter, "cancel_order_by_client_id", None)
            if canceller is not None and self.dry_run:
                log.warning("[%s] DRY RUN: order %s is still working; it WOULD be cancelled. Nothing was cancelled.",
                            profile.user_id, pe.client_id)
            elif canceller is not None:
                try:
                    await canceller(pe.symbol, pe.client_id)
                except Exception as exc:
                    log.warning("could not cancel the remainder of %s (%s)", pe.client_id, type(exc).__name__)
                ev = await gather_evidence(adapter, pe.symbol, pe.client_id, pe.submitted_at, self.clock(), self.evidence_margin,
                                   create_rejected=pe.rejected)
        dec = decide(ev)
        if dec.outcome == "filled":
            if ev.position_qty > 0 and not await self._position_is_protected(profile, adapter, pe.symbol):
                return False, "the order filled but the position is not yet confirmed protected"
            return True, f"{dec.reasons[0]}; position flat or confirmed protected [{ev.summary()}]"
        if dec.outcome == "unfilled":
            return True, f"{dec.reasons[0]} [{ev.summary()}]"
        if dec.outcome == "not_seen":
            return False, ("every source is clean but none shows this order; that cannot prove it was never accepted, so "
                           f"it needs an operator (resolve-entry) [{ev.summary()}]")
        return False, "; ".join(dec.reasons)

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
                await self._on_closed(profile, adapter, t)      # re-confirms flat and clears leftover stops first
                continue
            ind = self.scanner.indicators.get(t.symbol)
            if ind is None:
                continue
            last = _aware(t.last_trail_ts) if t.last_trail_ts else None
            bars = ind[ind.index > pd.Timestamp(last)] if last else \
                ind[ind.index >= pd.Timestamp(_aware(t.opened_at)).floor(tf)]
            if bars.empty:
                continue
            trail = TrailingStop(t.side, t.stop_price, t.extreme_price, t.trail_atr_mult or self.params.trail_atr_mult)
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
            # Strategy exit, judged on the last CLOSED candle only (it is new, because bars is not empty).
            why = strat.exit_signal(t.strategy, ind, len(ind) - 1, t.side)
            if why:
                await self._strategy_exit(profile, adapter, t, float(pos["contracts"]), why)

    async def _strategy_exit(self, profile, adapter, t, amount: float, why: str) -> None:
        """Close because the strategy's own exit rule fired. The protective stop stays in place until the exchange shows
        the position is gone; only then are the stop orders cancelled (and verified cancelled) and the trade recorded."""
        uid = profile.user_id
        if self.dry_run:
            log.info("[%s] DRY RUN, would close %s [%s]: %s", uid, t.symbol, t.strategy, why)
            return
        log.info("[%s] STRATEGY EXIT %s [%s]: %s", uid, t.symbol, t.strategy, why)
        res = await self._close_confirmed(profile, adapter, t.symbol, reason="strategy-exit")
        if res.status == "closed":
            await self._on_closed(profile, adapter, t, reason=f"strategy-exit: {why}", verified_flat=res.cleaned)
        else:
            log.error("[%s] strategy exit of %s not completed (%s%s). The position is NOT recorded as closed and its "
                      "stop protection is kept.", uid, t.symbol, res.status, f", {res.detail}" if res.detail else "")

    async def _cancel_and_verify_stops(self, adapter, symbol: str, attempts: int = 3) -> bool:
        """Cancel every protective order on a symbol that has no position, and confirm none is left."""
        if self.dry_run:
            log.info("DRY RUN: the orders on %s WOULD be cancelled; nothing was cancelled", symbol)
            return False
        for i in range(attempts):
            try:
                await adapter.cancel_all(symbol)
            except Exception as exc:
                log.warning("could not cancel the orders on %s (%s), attempt %d/%d", symbol, type(exc).__name__, i + 1, attempts)
            try:
                if not await adapter.fetch_stop_orders(symbol):
                    return True
            except Exception as exc:
                log.warning("could not list the orders on %s (%s), attempt %d/%d", symbol, type(exc).__name__, i + 1, attempts)
            if i + 1 < attempts:
                await self.sleep(0.5 * 2 ** i)
        return False

    async def _close_confirmed(self, profile, adapter, symbol: str, *, reason: str | None = None,
                               restore: bool = True) -> CloseResult:
        """Reduce-only market close that never lowers protection and never trusts the order call's own answer.

          1. read the position; keep every existing stop in place;
          2. send ONE reduce-only market close (never resent, even after a timeout or an error);
          3. re-read the position until it changes or the reads run out;
          4. zero left: cancel the leftover stops and verify they are gone. Some left, or nothing closed: restore
             full stop coverage for what remains. Unreadable: change nothing, report unknown.
        A stop that fills while the close is in flight is handled by the re-read: the reduce-only close is refused or
        shrinks, and the exchange shows the position at zero."""
        uid = profile.user_id
        if self.dry_run:
            log.info("[%s] DRY RUN: %s WOULD be closed with a reduce-only market order; nothing was sent", uid, symbol)
            return CloseResult("unknown", None, "dry run: nothing was sent")
        readable, before, pside = await self._position_state(adapter, symbol)
        if not readable:
            self._unverified[uid] = f"{symbol}: position could not be read while closing; protection left untouched"
            return CloseResult("unknown", None, "the position could not be read; stops left untouched")
        note = ""
        remaining: float | None = before
        if before > 0:
            exit_side = "sell" if pside == "long" else "buy"
            try:
                await adapter.market_order(symbol, exit_side, before, reduce_only=True, reason=reason)
            except Exception as exc:       # a timeout is NOT retried: the order may have been accepted
                note = f"the close order raised {type(exc).__name__}"
                log.warning("[%s] close of %s: %s; not resending, re-reading the position instead", uid, symbol, note)
            for delay in (0.0, 0.5, 1.0, 2.0):
                if delay:
                    await self.sleep(delay)
                ok, qty, _ = await self._position_state(adapter, symbol, attempts=1)
                remaining = qty if ok else None
                if remaining == 0.0:
                    break
        if remaining is None:
            self._unverified[uid] = f"{symbol}: position unreadable after a close attempt; stops left untouched"
            return CloseResult("unknown", None, (note + "; " if note else "") + "the position could not be read afterwards")
        if remaining == 0.0:
            cleaned = await self._cancel_and_verify_stops(adapter, symbol)
            if not cleaned:
                note = (note + "; " if note else "") + "leftover protective orders could not be verified cancelled"
            return CloseResult("closed", 0.0, note, cleaned)
        status = "partial" if remaining < before * (1 - 1e-9) else "open"
        problem = ""
        if restore:                                   # whatever is left must be fully covered by a valid stop
            problem = await self._restore_protection(profile, adapter, symbol)
        detail = "; ".join(x for x in (note, f"{remaining:g} of {before:g} still open", problem) if x)
        return CloseResult(status, remaining, detail)

    async def _restore_protection(self, profile, adapter, symbol: str) -> str:
        """Re-verify (and if needed repair) the stop on one symbol. Returns '' on success, else the problem; entries are
        blocked when it cannot be fixed."""
        uid = profile.user_id
        try:
            positions = {p["symbol"]: p for p in await adapter.fetch_open_positions([symbol])}
            pos = positions.get(symbol)
            if pos is None:
                return ""
            trades = {t.symbol: t for t in await asyncio.to_thread(self.store.list_open, uid)}
            await self._protect_symbol(profile, adapter, symbol, pos, trades.get(symbol))
            return ""
        except Exception as exc:
            msg = f"stop coverage for {symbol} could not be confirmed ({type(exc).__name__}: {str(exc)[:120]})"
            self._unverified[uid] = msg
            log.critical("[%s] %s", uid, msg)
            return msg

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
                await self._close_confirmed(profile, adapter, t.symbol)       # the old stop stays until flat is confirmed
            else:
                log.warning("[%s] could not move %s stop to %.6g (%s); old stop stays", uid, t.symbol, new_stop, exc)
            return None
        if t.stop_order_id and t.stop_order_id != order.get("id"):
            try:
                await adapter.cancel_order(t.symbol, t.stop_order_id)
            except Exception as exc:                 # the new stop is live; the old one is surplus but must be proven gone
                self._mark_unverified(uid, f"the old stop {t.stop_order_id} on {t.symbol} could not be proven cancelled "
                                           f"after the trail moved ({type(exc).__name__}); duplicate stops may exist")
        log.info("[%s] %s trailing stop %.6g -> %.6g", uid, t.symbol, t.stop_price, new_stop)
        return order.get("id")

    async def _on_closed(self, profile, adapter, t, reason: str = "closed on exchange (stop or manual)",
                         verified_flat: bool = False) -> bool:
        """Record a trade as closed. Only after the EXCHANGE shows the position at zero and its protective orders gone;
        a market order that returned without error proves nothing. Returns False (record kept) otherwise."""
        uid = profile.user_id
        if self.dry_run:
            log.info("[%s] DRY RUN: %s WOULD be recorded as closed once flat and its stops are cancelled; nothing was "
                     "cancelled or written", uid, t.symbol)
            return False
        if not verified_flat:
            readable, qty, _ = await self._position_state(adapter, t.symbol)
            if not readable or qty > 0:
                log.error("[%s] %s is NOT recorded as closed: the exchange %s", uid, t.symbol,
                          "position could not be read" if not readable else f"still shows {qty:g} open")
                return False
            if not await self._cancel_and_verify_stops(adapter, t.symbol):
                self._cleanup_debt[uid].add(t.symbol)          # blocks entries until the leftover stops are proven gone
                log.critical("[%s] %s is flat but its protective orders could not be verified cancelled; the record is "
                             "kept, new entries are blocked and cleanup retries on every check", uid, t.symbol)
                return False
        self._cleanup_debt[uid].discard(t.symbol)
        if t.mode == "live":
            await asyncio.to_thread(
                self.store.log_closed, user_id=uid, symbol=t.symbol, side=t.side, amount=0.0,
                entry_price=t.entry_price, exit_price=None, pnl=None, fees=0.0,
                opened_at=_aware(t.opened_at), exit_reason=reason, mode="live", strategy=t.strategy)
        log.info("[%s] %s position is closed; removing its trailing-stop state", uid, t.symbol)
        await asyncio.to_thread(self.store.remove, t.id)
        return True

    # ------------------------------------------------------------------ kill switch and protection
    async def _apply_controls(self, profile: UserProfile, adapter) -> ControlState:
        ctrl = await asyncio.to_thread(self.controls.effective, profile.user_id)
        if ctrl.flatten:
            await self._flatten(profile, adapter, ctrl)
        return ctrl

    async def _flatten(self, profile: UserProfile, adapter, ctrl: ControlState) -> None:
        """Emergency exit: close every position the bot manages for this user. Each stop stays active until the exchange
        shows that position is gone; a close that is not confirmed leaves (or restores) the stop and is retried."""
        uid = profile.user_id
        trades = {t.symbol: t for t in await asyncio.to_thread(self.store.list_open, uid)}
        positions = [p for p in await adapter.fetch_open_positions()
                     if p["symbol"] in trades or p["symbol"] in profile.active_coins]
        if not positions and not trades:
            return
        log.warning("[%s] EMERGENCY FLATTEN (%s): closing %d open position(s) at market; stops stay until each is confirmed flat.",
                    uid, ctrl.describe(), len(positions))
        if self.dry_run:
            return
        cleaned: dict[str, bool] = {}
        for pos in positions:
            res = await self._close_confirmed(profile, adapter, pos["symbol"])
            cleaned[pos["symbol"]] = res.cleaned
            if res.status != "closed":
                log.error("[%s] could not confirm %s closed (%s%s); its stop protection is kept; retrying on the next check",
                          uid, pos["symbol"], res.status, f", {res.detail}" if res.detail else "")
        still_open = {p["symbol"] for p in await adapter.fetch_open_positions()}
        for sym, t in trades.items():
            if sym not in still_open:
                await self._on_closed(profile, adapter, t, reason="emergency flatten", verified_flat=cleaned.get(sym, False))

    def _emergency_stop(self, symbol: str, side: str, entry: float) -> float:
        """Stop for a position that has none: 2 x ATR if candles are loaded, otherwise 3% from entry."""
        ind = self.scanner.indicators.get(symbol) if self.scanner else None
        dist = entry * 0.03
        if ind is not None and len(ind) and float(ind["atr"].iloc[-1]) > 0:
            dist = self.params.stop_atr_mult * float(ind["atr"].iloc[-1])
        dist = min(dist, entry * self.params.max_stop_pct)
        return entry - dist if side == "buy" else entry + dist

    async def _verify_exchange_state(self, profile: UserProfile, adapter) -> bool:
        """Confirm positions and their stop orders. If that cannot be done for ANY symbol, the account is treated as
        UNVERIFIED: a position is never assumed protected, and no new trade opens until a later check succeeds."""
        uid = profile.user_id
        try:
            await self._ensure_protected(profile, adapter)
        except ProtectionError as exc:
            reason = f"protection could not be confirmed for {len(exc.failures)} symbol(s): {exc}"
            self._mark_unverified(uid, reason)
            return False
        except Exception as exc:
            reason = f"cannot verify positions and stop orders on the exchange ({type(exc).__name__}: {str(exc)[:120]})"
            self._mark_unverified(uid, reason)
            return False
        if self._unverified.pop(uid, None):
            log.warning("[%s] exchange state verified again; new entries are allowed.", uid)
        return True

    def _mark_unverified(self, uid: str, reason: str) -> None:
        if self._unverified.get(uid) != reason:
            log.critical("[%s] %s. Treating the affected positions as UNPROTECTED and blocking new entries until it "
                         "succeeds.", uid, reason)
        self._unverified[uid] = reason

    async def _ensure_protected(self, profile: UserProfile, adapter) -> None:
        """Every position the bot manages must have a live exchange-side stop that really protects it, and a record.

        Each symbol is checked on its own: a failure on one never stops the others from being verified or repaired.
        Failures are collected and raised together as ProtectionError. Positions in symbols the bot does not trade are
        ignored, so use a dedicated Binance account (or sub-account) for the bot."""
        uid = profile.user_id
        positions = {p["symbol"]: p for p in await adapter.fetch_open_positions()}
        if not positions:
            return
        trades = {t.symbol: t for t in await asyncio.to_thread(self.store.list_open, uid)}
        failures: dict[str, str] = {}
        for sym, pos in positions.items():
            t = trades.get(sym)
            if t is None and (not self.adopt_unmanaged or sym not in profile.active_coins):
                continue
            try:
                await self._protect_symbol(profile, adapter, sym, pos, t)
            except Exception as exc:
                log.exception("[%s] SAFETY: could not verify or protect %s", uid, sym)
                failures[sym] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if failures:
            raise ProtectionError(failures)

    async def _remove_surplus_stops(self, adapter, sym: str, extras: list[tuple[dict, str]]) -> None:
        """Cancel stale or duplicate stop orders, each on its own route, and prove each one gone. Only ever called AFTER a
        valid full-coverage stop is confirmed. A cancel that cannot be confirmed raises, so the account becomes unverified."""
        if self.dry_run:
            if extras:
                log.info("DRY RUN: %d surplus stop(s) on %s WOULD be cancelled; nothing was cancelled", len(extras), sym)
            return
        failures = []
        for order, why in extras:
            kwargs = {"route": order["route"]} if order.get("route") else {}
            try:
                await adapter.cancel_order(sym, order["id"], **kwargs)
                log.warning("SAFETY: cancelled surplus stop %s on %s (%s)", order["id"], sym, why)
            except Exception as exc:
                failures.append(f"{order['id']} ({type(exc).__name__})")
        if failures:
            raise RuntimeError(f"could not prove surplus stop(s) on {sym} cancelled: {', '.join(failures)}")

    async def _settle_cleanup(self, profile: UserProfile, adapter) -> None:
        """Retry the removal of leftover stops on symbols that are now flat. Entries stay blocked until each is proven clean."""
        uid = profile.user_id
        for sym in sorted(self._cleanup_debt.get(uid, ())):
            readable, qty, _ = await self._position_state(adapter, sym)
            if not readable:
                continue
            if qty > 0:                                   # a new position now owns this symbol; _protect_symbol handles it
                self._cleanup_debt[uid].discard(sym)
            elif self.dry_run:
                log.info("[%s] DRY RUN: leftover stops on %s would be cancelled again", uid, sym)
            elif await self._cancel_and_verify_stops(adapter, sym):
                self._cleanup_debt[uid].discard(sym)
                log.warning("[%s] leftover stops on %s are now proven cancelled", uid, sym)
        if not self._cleanup_debt.get(uid):
            self._cleanup_debt.pop(uid, None)

    async def _protect_symbol(self, profile: UserProfile, adapter, sym: str, pos: dict, t) -> None:
        uid = profile.user_id
        side = "buy" if pos["side"] == "long" else "sell"
        exit_side = "sell" if side == "buy" else "buy"
        amount, entry = abs(float(pos["contracts"])), float(pos.get("entryPrice") or 0)
        assessment = assess_stops(await adapter.fetch_stop_orders(sym), position_side=pos["side"], position_qty=amount)
        best = assessment.best

        if best is not None:                                    # a stop that really protects the whole position exists
            if t is not None:
                if best["id"] != t.stop_order_id and self.dry_run:
                    log.info("[%s] DRY RUN: the stop record for %s would be synced to the exchange order %s", uid, sym, best["id"])
                elif best["id"] != t.stop_order_id:             # keep our record in step with the exchange
                    await asyncio.to_thread(self.store.update_trail, t.id, stop_price=float(best["stopPrice"]),
                                            extreme_price=t.extreme_price, last_trail_ts=t.last_trail_ts,
                                            stop_order_id=best["id"])
            else:                                               # has a stop but no record: adopt it
                log.warning("[%s] SAFETY: found %s position without a tracking record; adopting its existing stop "
                            "at %.6g.", uid, sym, float(best["stopPrice"]))
                if not self.dry_run:
                    await asyncio.to_thread(
                        self.store.open, user_id=uid, symbol=sym, side=side, entry_price=entry,
                        stop_price=float(best["stopPrice"]), stop_order_id=best["id"], leverage=profile.leverage,
                        mode=profile.trading_mode, opened_at=self.clock(), strategy="adopted")
            extras = [(o, "duplicate of a stop that already protects the position") for o in assessment.duplicates]
            extras += [(o, why) for o, why in assessment.invalid]
            await self._remove_surplus_stops(adapter, sym, extras)
            return

        stop = t.stop_price if t is not None else self._emergency_stop(sym, side, entry)
        why = "its stop-loss order was missing" if t is not None else "it had no stop-loss and no tracking record"
        if assessment.invalid:
            why += " (existing stop orders do not protect it: " + "; ".join(r for _, r in assessment.invalid) + ")"
        log.warning("[%s] SAFETY: %s %s position is unprotected (%s). Placing a stop-loss at %.6g.",
                    uid, sym, pos["side"], why, stop)
        if self.dry_run:
            return
        price = await adapter.fetch_last_price(sym)
        through = (price <= stop) if side == "buy" else (price >= stop)
        if through:
            log.warning("[%s] SAFETY: %s price %.6g is already through the stop %.6g; closing at market.",
                        uid, sym, price, stop)
            await self._close_unprotectable(profile, adapter, sym, t, "closed by safety check (price through stop)")
            return
        try:
            order = await adapter.stop_market(sym, exit_side, amount, stop)
        except Exception:
            log.exception("[%s] SAFETY: could not place a stop on %s; closing the position instead", uid, sym)
            await self._close_unprotectable(profile, adapter, sym, t, "closed by safety check (no stop possible)")
            return
        confirmed = None
        for i in range(3):                                       # the new stop must be visible and valid on the exchange
            confirmed = assess_stops(await adapter.fetch_stop_orders(sym), position_side=pos["side"],
                                     position_qty=amount)
            if confirmed.best is not None:
                break
            await self.sleep(0.5 * 2 ** i)
        if confirmed is None or confirmed.best is None:
            raise RuntimeError(f"stop {order.get('id')} was accepted but a valid full-coverage stop is not visible "
                               "on the exchange")
        best = confirmed.best
        if t is None:
            await asyncio.to_thread(
                self.store.open, user_id=uid, symbol=sym, side=side, entry_price=entry, stop_price=float(best["stopPrice"]),
                stop_order_id=best["id"], leverage=profile.leverage, mode=profile.trading_mode,
                opened_at=self.clock(), strategy="adopted")
        else:
            await asyncio.to_thread(self.store.update_trail, t.id, stop_price=float(best["stopPrice"]),
                                    extreme_price=t.extreme_price, last_trail_ts=t.last_trail_ts,
                                    stop_order_id=best["id"])
        extras = [(o, "duplicate of the new stop") for o in confirmed.duplicates]
        extras += [(o, why) for o, why in confirmed.invalid]
        await self._remove_surplus_stops(adapter, sym, extras)       # only now that the replacement is confirmed

    async def _close_unprotectable(self, profile, adapter, sym: str, t, reason: str) -> None:
        """Close a position that cannot be given a stop. Success means the EXCHANGE shows it flat."""
        res = await self._close_confirmed(profile, adapter, sym, restore=False)
        if res.status != "closed":
            raise RuntimeError(f"{sym} has no valid stop and the close is not confirmed ({res.status}"
                               f"{', ' + res.detail if res.detail else ''})")
        if t is not None:
            await self._on_closed(profile, adapter, t, reason=reason, verified_flat=res.cleaned)

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

    from g_aegisbot.db import get_engine, get_session_factory
    from g_aegisbot.migrate import ensure_schema
    from g_aegisbot.security.crypto import SecretBox

    log = logging.getLogger("aegis.runner")
    lock = RunnerLock(get_engine())
    try:
        lock.acquire()          # before schema work and before any exchange adapter exists: a second runner does nothing
    except RunnerAlreadyRunning as exc:
        log.critical("NOT STARTING: %s", exc)
        sys.exit(EXIT_CODE_ALREADY_RUNNING)
    except RunnerLockError as exc:
        log.critical("NOT STARTING: %s", exc)
        sys.exit(4)
    try:
        ensure_schema(get_engine())
        params = livegate.configured_params()          # AEGIS_STRATEGIES; unknown names stop the start-up
        log.info("Strategies enabled: %s (single-runner lock: %s)", ", ".join(params.strategies), lock.mode)
        runner = TradingRunner(get_session_factory(), SecretBox.from_env(), params, dry_run=args.dry_run, lock=lock)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            for sig in (os_signal.SIGINT, os_signal.SIGTERM):
                loop.add_signal_handler(sig, runner.stop)
            await (runner.run_once() if args.once else runner.run_forever())

        asyncio.run(_main())
    finally:
        lock.release()


if __name__ == "__main__":
    main()
