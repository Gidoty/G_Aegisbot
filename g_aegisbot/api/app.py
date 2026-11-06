"""G-Aegisbot HTTP API (for the dashboard).

Run:  uvicorn --factory g_aegisbot.api.app:create_app_from_env --host 127.0.0.1 --port 8000

The API never trades and never holds exchange connections. It reads and writes the database; the runner
does the trading. Logins are Supabase JWTs. A login maps to a profile when profile.user_id equals the
login's email (or Supabase user id), so create each profile with the person's login email as its user_id.
Nothing secret is ever returned: API keys go in, only 'stored: true' comes out.
"""
from __future__ import annotations

import collections
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, text

from g_aegisbot import audit
from g_aegisbot.api import billing as bill
from g_aegisbot.api.auth import AuthError, Authenticator
from g_aegisbot.api.schemas import AdminCreateIn, AdminPatchIn, ExtendIn, KeysIn, KillIn, ResumeIn, SettingsIn
from g_aegisbot.api.settings import ApiSettings, load_api_settings
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileLimitError, ProfileRepository
from g_aegisbot.db.trading_models import (
    AuditLog, Control, EquitySnapshot, Heartbeat, OpenTrade, Payment, TradeLog,
)
from g_aegisbot.engine.controls import GLOBAL, ControlStore
from g_aegisbot.exchange.key_check import check_api_key
from g_aegisbot.services import as_utc, extend_subscription, live_switch_problems

log = logging.getLogger("aegis.api")


@dataclass(frozen=True)
class Principal:
    user_id: str
    is_admin: bool


class RateLimiter:
    """Fixed in-memory sliding window. Enough to blunt abuse of the key and invoice endpoints."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def check(self, key: str) -> None:
        now, q = self.clock(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
        q.append(now)


def profile_view(p: UserProfile, ctrl, now: datetime | None = None) -> dict:
    t = p.daily_loss_tracker or {}
    return {
        "user_id": p.user_id, "is_admin": p.is_admin, "trading_mode": p.trading_mode, "leverage": p.leverage,
        "max_wallet_cap_usdt": float(p.max_wallet_cap_usdt or 0), "virtual_balance": float(p.virtual_balance or 0),
        "active_coins": list(p.active_coins), "keys_stored": p.has_credentials,
        "subscription": {"status": p.subscription_status,
                         "expires_at": as_utc(p.subscription_expires_at).isoformat() if p.subscription_expires_at else None,
                         "active": p.subscription_is_active(now)},
        "today": {"date": t.get("date"), "start_equity": t.get("start_equity"), "pnl": t.get("pnl"),
                  "loss_pct": t.get("loss_pct"), "breaker_tripped": bool(t.get("breaker_tripped"))},
        "control": {"paused": ctrl.paused, "flatten": ctrl.flatten, "reason": ctrl.reason, "scope": ctrl.scope},
    }


def create_app(settings: ApiSettings, sf, box, *, authenticator: Authenticator | None = None, key_checker=None,
               billing_client=None, clock=None) -> FastAPI:
    clock = clock or (lambda: datetime.now(timezone.utc))
    app = FastAPI(title="G-Aegisbot API", docs_url="/api/docs" if settings.docs else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if settings.docs else None)
    auth = authenticator or (Authenticator.from_settings(settings) if settings.auth_configured else None)
    controls = ControlStore(sf)
    check_key = key_checker or check_api_key
    limiter = RateLimiter(5, 60)
    now_client = billing_client or (bill.NowPaymentsClient(settings.now_api_key, settings.now_base_url)
                                    if settings.billing_enabled else None)

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def secure_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # The default response echoes the submitted input, which would leak API keys. Strip it.
        errs = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errs})

    # ------------------------------------------------------------ auth dependencies
    def principal(request: Request) -> Principal:
        if auth is None:
            raise HTTPException(503, "Authentication is not configured on the server.")
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer ") or not header[7:].strip():
            raise HTTPException(401, "Sign in first.")
        try:
            ident = auth.verify(header[7:].strip())
        except AuthError:
            raise HTTPException(401, "Your login is invalid or has expired. Sign in again.")
        with sf() as s:
            rows = list(s.scalars(select(UserProfile).where(
                func.lower(UserProfile.user_id).in_([x for x in (ident.email, ident.sub.lower()) if x]))))
        if not rows:
            raise HTTPException(403, "This login has no G-Aegisbot profile. Ask the administrator to create one "
                                     "using your login email as the user id.")
        return Principal(rows[0].user_id, rows[0].is_admin)

    def admin(p: Principal = Depends(principal)) -> Principal:
        if not p.is_admin:
            raise HTTPException(403, "Administrator access required.")
        return p

    def get_profile(s, user_id: str) -> UserProfile:
        p = s.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        if p is None:
            raise HTTPException(404, f"No profile '{user_id}'.")
        return p

    def runner_status() -> dict:
        with sf() as s:
            beat = s.get(Heartbeat, "runner")
        if beat is None:
            return {"alive": False, "seconds_since": None, "info": "the runner has never reported in"}
        age = int((clock() - as_utc(beat.at)).total_seconds())
        return {"alive": age <= settings.runner_stale_seconds, "seconds_since": age, "info": beat.info}

    # ------------------------------------------------------------ public
    @app.get("/api/health")
    def health():
        try:
            with sf() as s:
                s.execute(text("select 1"))
            db = True
        except Exception:
            db = False
        return {"status": "ok" if db else "degraded", "db": db, "runner": runner_status(),
                "billing_enabled": settings.billing_enabled}

    # ------------------------------------------------------------ me
    @app.get("/api/me")
    def me(p: Principal = Depends(principal)):
        with sf() as s:
            return profile_view(get_profile(s, p.user_id), controls.effective(p.user_id), clock())

    @app.get("/api/me/positions")
    def my_positions(p: Principal = Depends(principal)):
        with sf() as s:
            rows = list(s.scalars(select(OpenTrade).where(OpenTrade.user_id == p.user_id).order_by(OpenTrade.opened_at)))
        return [{"symbol": t.symbol, "side": "long" if t.side == "buy" else "short", "entry_price": t.entry_price,
                 "stop_price": t.stop_price, "best_price": t.extreme_price, "leverage": t.leverage, "mode": t.mode,
                 "opened_at": as_utc(t.opened_at).isoformat()} for t in rows]

    @app.get("/api/me/trades")
    def my_trades(limit: int = 50, p: Principal = Depends(principal)):
        with sf() as s:
            rows = list(s.scalars(select(TradeLog).where(TradeLog.user_id == p.user_id)
                                  .order_by(TradeLog.id.desc()).limit(max(1, min(limit, 500)))))
        return [{"symbol": t.symbol, "side": "long" if t.side == "buy" else "short", "amount": t.amount,
                 "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": t.pnl, "fees": t.fees,
                 "reason": t.exit_reason, "mode": t.mode, "opened_at": as_utc(t.opened_at).isoformat(),
                 "closed_at": as_utc(t.closed_at).isoformat()} for t in rows]

    @app.get("/api/me/equity")
    def my_equity(hours: int = 168, p: Principal = Depends(principal)):
        since = clock() - timedelta(hours=max(1, min(hours, 24 * 90)))
        with sf() as s:
            rows = list(s.scalars(select(EquitySnapshot).where(EquitySnapshot.user_id == p.user_id,
                                                               EquitySnapshot.at >= since).order_by(EquitySnapshot.at)))
        return [{"at": as_utc(r.at).isoformat(), "equity": r.equity} for r in rows]

    @app.put("/api/me/settings")
    def my_settings(body: SettingsIn, p: Principal = Depends(principal)):
        with sf() as s:
            prof = get_profile(s, p.user_id)
            if body.leverage is not None:
                prof.leverage = body.leverage
            if body.active_coins is not None:
                prof.active_coins = body.active_coins
            s.commit()
            view = profile_view(prof, controls.effective(p.user_id), clock())
        audit.record(sf, p.user_id, "self-settings", p.user_id,
                     f"leverage={body.leverage} coins={len(body.active_coins) if body.active_coins is not None else '-'}")
        return view

    @app.put("/api/me/keys")
    async def my_keys(body: KeysIn, p: Principal = Depends(principal)):
        limiter.check(f"keys:{p.user_id}")
        try:
            check = await check_key(body.api_key, body.api_secret)
        except Exception as exc:
            log.warning("[%s] key check could not complete: %s", p.user_id, type(exc).__name__)
            raise HTTPException(502, "Could not reach Binance to check the key. Try again in a minute.")
        if not check.ok:
            raise HTTPException(422, {"message": "This key was not saved.", "problems": check.problems})

        def store():
            with sf() as s:
                ProfileRepository(s, box).set_credentials(get_profile(s, p.user_id), body.api_key, body.api_secret)

        await run_in_threadpool(store)
        audit.record(sf, p.user_id, "self-set-keys", p.user_id, "verified")
        return {"stored": True, "warnings": check.warnings}

    @app.post("/api/me/pause")
    def pause_me(p: Principal = Depends(principal)):
        cur = controls.effective(p.user_id)
        if cur.paused and cur.scope == GLOBAL:
            raise HTTPException(409, "Trading is already paused by the administrator.")
        controls.set(p.user_id, paused=True, reason="paused by you", by=p.user_id)
        audit.record(sf, p.user_id, "self-pause", p.user_id)
        return {"paused": True}

    @app.post("/api/me/resume")
    def resume_me(p: Principal = Depends(principal)):
        with sf() as s:
            row = s.get(Control, p.user_id)
        if row and (row.paused or row.flatten) and row.set_by != p.user_id:
            raise HTTPException(403, "The administrator paused your account. Only they can resume it.")
        controls.clear(p.user_id)
        audit.record(sf, p.user_id, "self-resume", p.user_id)
        return {"paused": controls.effective(p.user_id).paused}

    # ------------------------------------------------------------ billing
    @app.get("/api/me/billing")
    def my_billing(p: Principal = Depends(principal)):
        with sf() as s:
            rows = list(s.scalars(select(Payment).where(Payment.user_id == p.user_id).order_by(Payment.id.desc()).limit(20)))
        return {"enabled": settings.billing_enabled, "price_usd": settings.price_usd, "days": settings.subscription_days,
                "payments": [{"order_id": r.order_id, "status": r.status, "price_usd": r.price_usd, "days": r.days,
                              "credited": r.credited, "created_at": as_utc(r.created_at).isoformat()} for r in rows]}

    @app.post("/api/me/billing/invoice")
    async def create_invoice(p: Principal = Depends(principal)):
        if now_client is None:
            raise HTTPException(503, "Billing is not enabled on this server.")
        limiter.check(f"invoice:{p.user_id}")
        row = await run_in_threadpool(bill.new_payment, sf, p.user_id, settings.price_usd, settings.subscription_days, clock())
        try:
            data = await now_client.create_invoice(
                order_id=row.order_id, price_usd=settings.price_usd,
                description=f"G-Aegisbot subscription, {settings.subscription_days} days ({p.user_id})",
                ipn_url=f"{settings.public_api_url}/api/billing/nowpayments/ipn",
                success_url=f"{settings.dashboard_url}/billing?paid=1" if settings.dashboard_url else None,
                cancel_url=f"{settings.dashboard_url}/billing" if settings.dashboard_url else None)
        except bill.BillingError as exc:
            log.warning("[%s] invoice failed: %s", p.user_id, exc)
            raise HTTPException(502, "Could not create the payment invoice. Try again shortly.")
        await run_in_threadpool(bill.attach_invoice, sf, row.order_id, data.get("id"))
        return {"invoice_url": data["invoice_url"], "order_id": row.order_id}

    @app.post("/api/billing/nowpayments/ipn")
    async def nowpayments_ipn(request: Request):
        if not settings.now_ipn_secret:
            raise HTTPException(503, "Billing is not enabled.")
        raw = await request.body()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Malformed body.")
        if not bill.verify_signature(payload, request.headers.get("x-nowpayments-sig"), settings.now_ipn_secret):
            log.warning("IPN rejected: bad signature")
            raise HTTPException(401, "Bad signature.")
        result = await run_in_threadpool(bill.process_ipn, sf, payload, clock())
        log.info("IPN %s: %s", result.action, result.detail)
        return {"result": result.action}

    # ------------------------------------------------------------ admin
    @app.get("/api/admin/overview")
    def overview(_: Principal = Depends(admin)):
        with sf() as s:
            profiles = list(s.scalars(select(UserProfile).order_by(UserProfile.id)))
            open_trades = s.scalar(select(func.count()).select_from(OpenTrade)) or 0
            active = [{"scope": c.scope, "flatten": c.flatten, "reason": c.reason, "set_by": c.set_by}
                      for c in s.scalars(select(Control)) if c.paused or c.flatten]
        return {"runner": runner_status(), "kill_switch": active, "open_trades": open_trades,
                "users": [profile_view(x, controls.effective(x.user_id), clock()) for x in profiles]}

    @app.post("/api/admin/users", status_code=201)
    def admin_create(body: AdminCreateIn, a: Principal = Depends(admin)):
        fields = {"trading_mode": body.trading_mode, "leverage": body.leverage}
        for k in ("max_wallet_cap_usdt", "virtual_balance"):
            if getattr(body, k) is not None:
                fields[k] = getattr(body, k)
        if body.active_coins:
            fields["active_coins"] = body.active_coins
        try:
            with sf() as s:
                repo = ProfileRepository(s, box)
                if repo.get(body.user_id):
                    raise HTTPException(409, f"'{body.user_id}' already exists.")
                prof = repo.create(body.user_id, is_admin=body.is_admin, **fields)
                if body.subscription_days:
                    extend_subscription(prof, body.subscription_days, clock())
                    s.commit()
                view = profile_view(prof, controls.effective(prof.user_id), clock())
        except (ProfileLimitError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        audit.record(sf, a.user_id, "create-user", body.user_id, f"mode={body.trading_mode}")
        return view

    @app.patch("/api/admin/users/{user_id}")
    def admin_patch(user_id: str, body: AdminPatchIn, a: Principal = Depends(admin)):
        try:
            with sf() as s:
                prof = get_profile(s, user_id)
                if body.max_wallet_cap_usdt is not None:
                    prof.max_wallet_cap_usdt = body.max_wallet_cap_usdt
                if body.virtual_balance is not None:
                    prof.virtual_balance = body.virtual_balance
                if body.leverage is not None:
                    prof.leverage = body.leverage
                if body.active_coins is not None:
                    prof.active_coins = body.active_coins
                if body.trading_mode is not None:
                    if body.trading_mode == "live" and prof.trading_mode != "live":
                        problems = live_switch_problems(prof, body.confirm_real_money)
                        if problems:
                            raise HTTPException(400, "Cannot switch to live: " + "; ".join(problems) + ".")
                    prof.trading_mode = body.trading_mode
                s.commit()
                view = profile_view(prof, controls.effective(user_id), clock())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit.record(sf, a.user_id, "admin-update", user_id, ", ".join(k for k, v in body.model_dump().items()
                                                                          if v not in (None, False)))
        return view

    @app.post("/api/admin/users/{user_id}/extend")
    def admin_extend(user_id: str, body: ExtendIn, a: Principal = Depends(admin)):
        with sf() as s:
            prof = get_profile(s, user_id)
            extend_subscription(prof, body.days, clock())
            s.commit()
            view = profile_view(prof, controls.effective(user_id), clock())
        audit.record(sf, a.user_id, "extend", user_id, f"+{body.days} days")
        return view

    @app.post("/api/admin/kill")
    def admin_kill(body: KillIn, a: Principal = Depends(admin)):
        if body.user_id:
            with sf() as s:
                get_profile(s, body.user_id)
        controls.set(body.user_id or GLOBAL, paused=True, flatten=body.flatten, reason=body.reason or None, by=a.user_id)
        audit.record(sf, a.user_id, "kill-flatten" if body.flatten else "kill-pause", body.user_id or GLOBAL, body.reason)
        return {"scope": body.user_id or GLOBAL, "flatten": body.flatten}

    @app.post("/api/admin/resume")
    def admin_resume(body: ResumeIn, a: Principal = Depends(admin)):
        controls.clear(body.user_id or GLOBAL)
        audit.record(sf, a.user_id, "resume", body.user_id or GLOBAL)
        return {"scope": body.user_id or GLOBAL, "cleared": True}

    @app.get("/api/admin/audit")
    def admin_audit(limit: int = 50, _: Principal = Depends(admin)):
        return [{"at": as_utc(r.at).isoformat(), "actor": r.actor, "action": r.action, "target": r.target,
                 "detail": r.detail} for r in audit.recent(sf, max(1, min(limit, 500)))]

    @app.get("/api/admin/payments")
    def admin_payments(limit: int = 50, _: Principal = Depends(admin)):
        with sf() as s:
            rows = list(s.scalars(select(Payment).order_by(Payment.id.desc()).limit(max(1, min(limit, 500)))))
        return [{"order_id": r.order_id, "user_id": r.user_id, "status": r.status, "price_usd": r.price_usd,
                 "days": r.days, "credited": r.credited, "payment_id": r.payment_id,
                 "created_at": as_utc(r.created_at).isoformat()} for r in rows]

    return app


def create_app_from_env() -> FastAPI:
    from g_aegisbot.config import get_settings
    from g_aegisbot.db import get_engine, get_session_factory, init_db
    from g_aegisbot.security.crypto import SecretBox

    from g_aegisbot.migrate import ensure_schema

    engine = get_engine()
    ensure_schema(engine)
    box = SecretBox.from_env()
    return create_app(load_api_settings(), get_session_factory(engine), box)
