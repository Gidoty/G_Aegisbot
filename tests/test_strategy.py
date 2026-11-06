from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from g_aegisbot.engine.strategy import (
    ELITE_SYMBOLS, MultiPairScanner, Skip, StrategyParams, TradePlan, TrailingStop,
    atr, ema, evaluate, execute_plan, plan_trade, rsi,
)

P = StrategyParams()


def make(n=400, up=True, seed=1):
    rng = np.random.default_rng(seed)
    base = np.where(np.arange(n) % 2 == 0, 0.003, -0.002)
    steps = (base if up else -base[::-1] * 1.0) + rng.normal(0, 0.0005, n)
    if not up:
        steps = np.where(np.arange(n) % 2 == 0, -0.003, 0.002) + rng.normal(0, 0.0005, n)
    close = 100 * np.cumprod(1 + steps)
    return pd.DataFrame(
        {"open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 100.0},
        index=pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
    )


def with_breakout(df, vol=160.0, up=True, beyond=0.004):
    df = df.copy()
    if up:
        lvl = df["high"].iloc[-21:-1].max()
        close = lvl * (1 + beyond)
    else:
        lvl = df["low"].iloc[-21:-1].min()
        close = lvl * (1 - beyond)
    df.iloc[-1, df.columns.get_loc("close")] = close
    df.iloc[-1, df.columns.get_loc("high")] = max(close, lvl) * 1.002
    df.iloc[-1, df.columns.get_loc("low")] = min(close, lvl) * 0.998
    df.iloc[-1, df.columns.get_loc("volume")] = vol
    return df


# ---- indicators
def test_indicators_basic():
    s = pd.Series(np.arange(1, 100, dtype=float))
    assert ema(pd.Series([5.0] * 50), 10).iloc[-1] == pytest.approx(5.0)
    assert rsi(s).iloc[-1] == 100.0            # only gains
    assert rsi(s[::-1].reset_index(drop=True)).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    assert (atr(make(), 14).dropna() > 0).all()


# ---- signals
def test_long_breakout_accepted():
    r = evaluate("X", with_breakout(make()), P)
    assert r.signal and r.signal.side == "buy"
    assert r.signal.stop_price == pytest.approx(r.signal.entry_price - 2 * r.signal.atr)


def test_short_breakout_accepted():
    r = evaluate("X", with_breakout(make(up=False), up=False), P)
    assert r.signal and r.signal.side == "sell"
    assert r.signal.stop_price > r.signal.entry_price


def test_volume_gate_exactly_1_5x():
    assert evaluate("X", with_breakout(make(), vol=150.0), P).signal is not None   # >= passes
    r = evaluate("X", with_breakout(make(), vol=149.9), P)
    assert r.signal is None and "volume" in r.reason


def test_signal_candle_cannot_inflate_own_volume_average():
    # Huge spike: ratio is measured against the PRIOR 20 candles, so it stays large.
    assert evaluate("X", with_breakout(make(), vol=1000.0), P).signal.volume_ratio == pytest.approx(10.0)


def test_no_breakout_rejected():
    df = make()
    assert evaluate("X", df, P).reason == "no breakout"


def test_trend_filter_rejects_counter_trend_breakout():
    r = evaluate("X", with_breakout(make(up=False), up=True, beyond=0.02), P)
    assert r.signal is None and "trend" in r.reason


def test_rsi_filter_rejects():
    r = evaluate("X", with_breakout(make()), replace(P, rsi_long=(50.0, 55.0)))
    assert r.signal is None and "RSI" in r.reason


def test_wide_stop_rejected():
    r = evaluate("X", with_breakout(make()), replace(P, max_stop_pct=0.001))
    assert r.signal is None and "stop too wide" in r.reason


def test_too_few_candles():
    assert "not enough" in evaluate("X", make(n=100), P).reason


def test_high_conviction_range():
    r = evaluate("X", with_breakout(make(), vol=300.0, beyond=0.01), replace(P, rsi_long=(50.0, 80.0)))
    assert r.signal.conviction == "high" and 0.12 <= r.signal.size_pct <= 0.15


def test_normal_conviction_range():
    s = evaluate("X", with_breakout(make(), vol=155.0), P).signal
    assert s.conviction == "normal" and 0.05 <= s.size_pct <= 0.07


# ---- trailing stop
def test_trailing_stop_ratchets_long():
    t = TrailingStop("buy", stop_price=98.0, extreme_price=100.0, atr_mult=2.0)
    assert t.update(110, 105, 1.0) and t.stop_price == 108.0
    assert not t.update(109, 100, 1.0) and t.stop_price == 108.0      # pullback: no loosening
    assert not t.update(110, 100, 5.0) and t.stop_price == 108.0      # ATR spike: no loosening


def test_trailing_stop_ratchets_short():
    t = TrailingStop("sell", stop_price=102.0, extreme_price=100.0, atr_mult=2.0)
    assert t.update(95, 90, 1.0) and t.stop_price == 92.0
    assert not t.update(100, 95, 1.0) and t.stop_price == 92.0


# ---- sizing and pre-trade verification
class FakeEx:
    markets = {"BTC/USDT:USDT": {"limits": {"amount": {"min": 0.001}, "cost": {"min": 100}}}}

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1000) / 1000:.3f}"


class FakeAdapter:
    def __init__(self, total=10_000, free=10_000, used=0, positions=()):
        self.bal = {"total": total, "free": free, "used": used}
        self.positions = list(positions)
        self.exchange = FakeEx()
        self.calls = []
        self.fail_stop = False

    async def fetch_usdt_balance(self): return self.bal
    async def fetch_open_positions(self, symbols=None): return self.positions
    async def configure_symbol(self, s, lev): self.calls.append(("configure", s, lev))
    async def market_order(self, s, side, amt, reduce_only=False):
        self.calls.append(("market", side, amt, reduce_only)); return {"id": "m"}
    async def stop_market(self, s, side, amt, price):
        if self.fail_stop: raise RuntimeError("boom")
        self.calls.append(("stop", side, amt, price)); return {"id": "s"}


def profile(cap=Decimal("5000"), active=True, tripped=False, leverage=3):
    exp = datetime.now(timezone.utc) + timedelta(days=5)
    return SimpleNamespace(
        max_wallet_cap_usdt=cap,
        leverage=leverage,
        daily_loss_tracker={"breaker_tripped": tripped},
        subscription_is_active=lambda: active,
    )


def signal(pct=0.06, price=50_000.0):
    r = evaluate("BTC/USDT:USDT", with_breakout(make()), P).signal
    from dataclasses import replace as rep
    return rep(r, entry_price=price, stop_price=price * 0.97, size_pct=pct, symbol="BTC/USDT:USDT")


async def test_plan_sizes_from_allocation_cap_at_3x():
    plan = await plan_trade(profile(), FakeAdapter(), signal(0.06), P)
    assert isinstance(plan, TradePlan)
    assert plan.margin == pytest.approx(5000 * 0.06, rel=0.01)     # 6% of capped capital, not 10k balance
    assert plan.leverage == 3 and plan.notional == pytest.approx(plan.margin * 3, rel=0.01)
    assert plan.risk_usdt == pytest.approx(plan.notional * 0.03, rel=0.01)


async def test_allocation_is_min_of_cap_and_balance():
    plan = await plan_trade(profile(cap=Decimal("50000")), FakeAdapter(total=2000, free=2000), signal(0.06), P)
    assert 100 < plan.margin <= 120      # 6% of the 2000 balance; the fake rounds size down to 0.001 BTC


@pytest.mark.parametrize("kwargs,word", [
    (dict(active=False), "subscription"),
    (dict(tripped=True), "breaker"),
    (dict(cap=Decimal("0")), "cap"),
])
async def test_profile_blocks(kwargs, word):
    r = await plan_trade(profile(**kwargs), FakeAdapter(), signal(), P)
    assert isinstance(r, Skip) and word in r.reason


async def test_insufficient_free_margin_skips_gracefully():
    r = await plan_trade(profile(), FakeAdapter(total=10_000, free=100), signal(0.06), P)
    assert isinstance(r, Skip) and "free margin" in r.reason


async def test_buffer_applies():
    margin = 5000 * 0.06
    r = await plan_trade(profile(), FakeAdapter(free=margin * 1.04), signal(0.06), P)
    assert isinstance(r, Skip)
    r = await plan_trade(profile(), FakeAdapter(free=margin * 1.06), signal(0.06), P)
    assert isinstance(r, TradePlan)


async def test_allocation_headroom_limits_total_exposure():
    r = await plan_trade(profile(), FakeAdapter(used=4900), signal(0.06), P)
    assert isinstance(r, Skip) and "headroom" in r.reason


async def test_open_position_skips():
    r = await plan_trade(profile(), FakeAdapter(positions=[{"contracts": 1}]), signal(), P)
    assert isinstance(r, Skip) and "already open" in r.reason


async def test_min_notional_skips():
    r = await plan_trade(profile(cap=Decimal("10")), FakeAdapter(), signal(0.05), P)
    assert isinstance(r, Skip)


async def test_execute_places_entry_then_reduce_only_stop():
    a = FakeAdapter()
    plan = await plan_trade(profile(), a, signal(), P)
    await execute_plan(a, plan)
    kinds = [c[0] for c in a.calls]
    assert kinds == ["configure", "market", "stop"]
    assert a.calls[2][1] == "sell"      # long entry -> sell stop


async def test_failed_stop_flattens_position():
    a = FakeAdapter(); a.fail_stop = True
    plan = await plan_trade(profile(), a, signal(), P)
    with pytest.raises(RuntimeError):
        await execute_plan(a, plan)
    assert a.calls[-1] == ("market", "sell", plan.amount, True)


# ---- scanner
class ScanAdapter:
    def __init__(self, frames, bad=()):
        self.frames, self.bad = frames, list(bad)
        self.exchange = SimpleNamespace(load_markets=self._lm)
    async def _lm(self): return {}
    async def validate_symbols(self, syms): return [s for s in syms if s in self.bad]
    async def fetch_ohlcv_df(self, sym, tf, limit):
        f = self.frames[sym]
        if isinstance(f, Exception): raise f
        return f


async def test_scanner_isolates_errors_drops_forming_candle_and_ranks():
    good = with_breakout(make())
    forming = good.copy()                   # last row is the "forming" candle: scanner must drop it
    forming.loc[forming.index[-1] + pd.Timedelta(hours=1)] = forming.iloc[-1]
    frames = {"A/USDT:USDT": forming, "B/USDT:USDT": RuntimeError("net"), "C/USDT:USDT": make(),
              "D/USDT:USDT": make()}
    sc = MultiPairScanner(ScanAdapter(frames, bad=["D/USDT:USDT"]), symbols=list(frames))
    assert await sc.prepare() == ["D/USDT:USDT"]
    res = {r.symbol: r for r in await sc.scan()}
    assert res["A/USDT:USDT"].signal is not None
    assert res["B/USDT:USDT"].reason.startswith("error")
    assert res["C/USDT:USDT"].reason == "no breakout"
    assert "D/USDT:USDT" not in res


def test_default_symbols():
    assert len(ELITE_SYMBOLS) == 15 and "1000PEPE/USDT:USDT" in ELITE_SYMBOLS


async def test_profile_leverage_drives_sizing():
    p1 = await plan_trade(profile(leverage=1), FakeAdapter(), signal(0.06), P)
    p5 = await plan_trade(profile(leverage=5), FakeAdapter(), signal(0.06), P)
    assert p1.leverage == 1 and p5.leverage == 5
    assert p5.notional == pytest.approx(p1.notional * 5, rel=0.02)


async def test_wide_stop_blocked_at_high_leverage_only():
    wide = replace(signal(0.06), stop_price=50_000.0 * 0.88)   # 12% stop
    assert isinstance(await plan_trade(profile(leverage=3), FakeAdapter(), wide, P), TradePlan)
    r = await plan_trade(profile(leverage=5), FakeAdapter(), wide, P)
    assert isinstance(r, Skip) and "liquidation" in r.reason
