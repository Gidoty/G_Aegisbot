import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ccxt.async_support as ccxt
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from g_aegisbot.api import billing as bill
from g_aegisbot.api.app import create_app
from g_aegisbot.api.auth import AuthError, Authenticator
from g_aegisbot.api.settings import ApiSettings, load_api_settings
from g_aegisbot.db import get_session_factory, init_db
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.db.trading_models import EquitySnapshot, Heartbeat, OpenTrade, Payment, TradeLog
from g_aegisbot.engine.controls import ControlStore
from g_aegisbot.exchange.key_check import KeyCheck
from g_aegisbot.security.crypto import SecretBox, generate_key

SECRET = "unit-test-jwt-secret-0123456789abcdef"
IPN = "ipn-test-secret"
NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)
KEY, SEC = "A" * 20 + "keyvalue", "B" * 20 + "secretvalue"


def token(email="alice@example.com", sub="uid-alice", secret=SECRET, alg="HS256", aud="authenticated", exp=3600, **extra):
    claims = {"sub": sub, "email": email, "aud": aud, "exp": int(time.time()) + exp, **extra}
    return jwt.encode(claims, secret, algorithm=alg)


def hdr(**kw):
    return {"Authorization": f"Bearer {token(**kw)}"}


class FakeBilling:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def create_invoice(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise bill.BillingError("down")
        return {"id": "inv-1", "invoice_url": "https://nowpayments.io/payment/?iid=inv-1"}


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/api.db")
    init_db(engine)
    sf = get_session_factory(engine)
    box = SecretBox([generate_key()])
    with sf() as s:
        repo = ProfileRepository(s, box)
        for uid, admin in (("alice@example.com", False), ("boss@example.com", True), ("bob@example.com", False)):
            p = repo.create(uid, is_admin=admin)
            repo.set_subscription(p, "active", FAR)
    state = {"checks": [], "check_result": KeyCheck(True, warnings=["not IP restricted"]), "check_error": None}

    async def key_checker(k, s):
        state["checks"].append((k, s))
        if state["check_error"]:
            raise state["check_error"]
        return state["check_result"]

    settings = ApiSettings(jwt_secret=SECRET, now_api_key="np-key", now_ipn_secret=IPN, price_usd=15.0,
                           subscription_days=30, public_api_url="https://api.example.com",
                           dashboard_url="https://dash.example.com")
    billing = FakeBilling()
    clock = {"now": NOW}
    app = create_app(settings, sf, box, key_checker=key_checker, billing_client=billing, clock=lambda: clock["now"])
    return dict(client=TestClient(app), sf=sf, box=box, state=state, billing=billing, engine=engine, clock=clock,
                settings=settings, tmp=tmp_path)


def get_profile(env, uid="alice@example.com"):
    with env["sf"]() as s:
        return s.scalar(select(UserProfile).where(UserProfile.user_id == uid))


# ------------------------------------------------------------------ authentication
def test_requests_without_a_valid_login_are_rejected(env):
    c = env["client"]
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert c.get("/api/me", headers={"Authorization": "Basic abc"}).status_code == 401
    assert c.get("/api/me", headers=hdr(secret="a-different-secret-0123456789abcdef!!")).status_code == 401
    assert c.get("/api/me", headers=hdr(exp=-60)).status_code == 401
    assert c.get("/api/me", headers=hdr(aud="someone-else")).status_code == 401
    unsigned = jwt.encode({"sub": "x", "email": "alice@example.com", "aud": "authenticated",
                           "exp": int(time.time()) + 600}, None, algorithm="none")
    assert c.get("/api/me", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_a_valid_login_without_a_profile_gets_a_helpful_403(env):
    r = env["client"].get("/api/me", headers=hdr(email="stranger@example.com", sub="uid-x"))
    assert r.status_code == 403 and "no G-Aegisbot profile" in r.json()["detail"]


def test_login_maps_to_the_profile_by_email_case_insensitively_or_by_sub(env):
    c = env["client"]
    assert c.get("/api/me", headers=hdr(email="ALICE@Example.COM")).json()["user_id"] == "alice@example.com"
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("uid-carol")
    assert c.get("/api/me", headers=hdr(email="", sub="uid-carol")).json()["user_id"] == "uid-carol"


def test_asymmetric_tokens_are_verified_against_jwks_and_hs256_downgrade_is_refused():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key()

    class FakeJwks:
        def get_signing_key_from_jwt(self, t):
            return type("K", (), {"key": pub})()

    auth = Authenticator(jwks_client=FakeJwks())
    good = jwt.encode({"sub": "u", "email": "A@b.c", "aud": "authenticated", "exp": int(time.time()) + 600}, key,
                      algorithm="RS256")
    assert auth.verify(good).email == "a@b.c"
    pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    import base64, hashlib, hmac
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 600}).encode())
    mac = b64(hmac.new(pem.encode(), head + b"." + body, hashlib.sha256).digest())
    forged = (head + b"." + body + b"." + mac).decode()      # hand-built: PyJWT refuses to create this itself
    with pytest.raises(AuthError):                                   # attacker signs HS256 with the public key
        auth.verify(forged)


def test_authenticator_needs_some_key_configured_and_api_503s_without_one(env, tmp_path):
    with pytest.raises(ValueError):
        Authenticator()
    app = create_app(ApiSettings(), env["sf"], env["box"])
    assert TestClient(app).get("/api/me", headers=hdr()).status_code == 503


def test_settings_load_from_environment():
    s = load_api_settings({"SUPABASE_URL": "https://abc.supabase.co/", "AEGIS_CORS_ORIGINS": "https://a.com, https://b.com",
                           "NOWPAYMENTS_API_KEY": "k", "NOWPAYMENTS_IPN_SECRET": "s", "SUBSCRIPTION_PRICE_USD": "20",
                           "AEGIS_PUBLIC_API_URL": "https://api.x.com/"})
    assert s.jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json" and s.jwt_issuer.endswith("/auth/v1")
    assert s.cors_origins == ("https://a.com", "https://b.com") and s.billing_enabled and s.price_usd == 20
    assert not load_api_settings({}).billing_enabled and not load_api_settings({}).auth_configured


# ------------------------------------------------------------------ my account
def test_me_shows_the_profile_without_any_secret(env):
    me = env["client"].get("/api/me", headers=hdr()).json()
    assert me["user_id"] == "alice@example.com" and me["keys_stored"] is False and me["leverage"] == 3
    assert me["subscription"]["active"] is True and me["control"]["paused"] is False
    assert "encrypted_api_key" not in json.dumps(me)


def test_storing_keys_verifies_encrypts_and_never_echoes(env):
    r = env["client"].put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr())
    assert r.status_code == 200 and r.json() == {"stored": True, "warnings": ["not IP restricted"]}
    assert KEY not in r.text and SEC not in r.text
    assert env["state"]["checks"] == [(KEY, SEC)]
    p = get_profile(env)
    assert p.has_credentials and KEY not in p.encrypted_api_key
    assert env["box"].decrypt(p.encrypted_api_key) == KEY
    assert env["client"].get("/api/me", headers=hdr()).json()["keys_stored"] is True


def test_a_key_that_can_withdraw_is_refused_and_not_stored(env):
    env["state"]["check_result"] = KeyCheck(False, ["WITHDRAWALS ARE ENABLED on this key."])
    r = env["client"].put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr())
    assert r.status_code == 422 and "WITHDRAWALS" in r.json()["detail"]["problems"][0]
    assert KEY not in r.text and not get_profile(env).has_credentials


def test_binance_being_unreachable_is_a_retryable_502(env):
    env["state"]["check_error"] = ccxt.RequestTimeout("timeout")
    r = env["client"].put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr())
    assert r.status_code == 502 and "Try again" in r.json()["detail"] and not get_profile(env).has_credentials


def test_validation_errors_never_echo_the_submitted_secret(env):
    r = env["client"].put("/api/me/keys", json={"api_key": "MARKERX12345", "api_secret": "tiny"}, headers=hdr())
    assert r.status_code == 422 and "MARKERX12345" not in r.text and "tiny" not in r.text


def test_key_uploads_are_rate_limited(env):
    codes = [env["client"].put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr()).status_code
             for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_users_can_change_leverage_and_pairs_but_not_their_cap_or_mode(env):
    c = env["client"]
    r = c.put("/api/me/settings", json={"leverage": 5, "active_coins": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                                        "max_wallet_cap_usdt": 999999, "trading_mode": "live"}, headers=hdr())
    assert r.status_code == 200 and r.json()["leverage"] == 5 and len(r.json()["active_coins"]) == 2
    p = get_profile(env)
    assert float(p.max_wallet_cap_usdt) == 10000 and p.trading_mode == "testnet"
    for bad in ({"leverage": 7}, {"leverage": 0}, {"active_coins": ["BTC/USDT"]}, {"active_coins": ["a/USDT:USDT"]},
                {"active_coins": [f"C{i}/USDT:USDT" for i in range(16)]},
                {"active_coins": ["BTC/USDT:USDT", "BTC/USDT:USDT"]}):
        assert c.put("/api/me/settings", json=bad, headers=hdr()).status_code == 422, bad


def test_positions_trades_and_equity_are_scoped_to_the_caller(env):
    with env["sf"]() as s:
        s.add(OpenTrade(user_id="alice@example.com", symbol="BTC/USDT:USDT", side="buy", entry_price=100, stop_price=97,
                        extreme_price=105, leverage=3, mode="testnet"))
        s.add(OpenTrade(user_id="bob@example.com", symbol="ETH/USDT:USDT", side="sell", entry_price=10, stop_price=11,
                        extreme_price=9, leverage=3, mode="testnet"))
        s.add(TradeLog(user_id="alice@example.com", symbol="SOL/USDT:USDT", side="sell", amount=1, entry_price=10,
                       exit_price=9, pnl=1.0, fees=0.1, opened_at=NOW, closed_at=NOW, exit_reason="stop-loss", mode="testnet"))
        s.add(TradeLog(user_id="bob@example.com", symbol="X/USDT:USDT", side="buy", amount=1, entry_price=1,
                       opened_at=NOW, closed_at=NOW, exit_reason="r", mode="testnet"))
        s.add(EquitySnapshot(user_id="alice@example.com", at=NOW - timedelta(hours=2), equity=10_000.0))
        s.add(EquitySnapshot(user_id="alice@example.com", at=NOW - timedelta(days=30), equity=9_000.0))
        s.add(EquitySnapshot(user_id="bob@example.com", at=NOW - timedelta(hours=1), equity=1.0))
        s.commit()
    c = env["client"]
    pos = c.get("/api/me/positions", headers=hdr()).json()
    assert [p["symbol"] for p in pos] == ["BTC/USDT:USDT"] and pos[0]["side"] == "long" and pos[0]["stop_price"] == 97
    trades = c.get("/api/me/trades", headers=hdr()).json()
    assert [t["symbol"] for t in trades] == ["SOL/USDT:USDT"] and trades[0]["side"] == "short"
    eq = c.get("/api/me/equity?hours=24", headers=hdr()).json()
    assert [e["equity"] for e in eq] == [10_000.0]
    assert len(c.get("/api/me/equity?hours=2000", headers=hdr()).json()) == 2


def test_pause_and_resume_respect_who_set_the_switch(env):
    c, controls = env["client"], ControlStore(env["sf"])
    assert c.post("/api/me/pause", headers=hdr()).json() == {"paused": True}
    assert controls.effective("alice@example.com").paused
    assert c.post("/api/me/resume", headers=hdr()).json() == {"paused": False}
    controls.set("alice@example.com", reason="admin says no", by="boss@example.com")
    r = c.post("/api/me/resume", headers=hdr())
    assert r.status_code == 403 and controls.effective("alice@example.com").paused      # the user cannot lift an admin pause
    controls.clear("alice@example.com")
    controls.set("global", reason="market event", by="boss@example.com")
    assert c.post("/api/me/pause", headers=hdr()).status_code == 409


# ------------------------------------------------------------------ admin
def test_admin_endpoints_refuse_ordinary_users(env):
    c, h = env["client"], hdr()
    calls = [("get", "/api/admin/overview", None), ("get", "/api/admin/audit", None), ("get", "/api/admin/payments", None),
             ("post", "/api/admin/users", {"user_id": "x"}), ("patch", "/api/admin/users/bob@example.com", {"leverage": 2}),
             ("post", "/api/admin/users/bob@example.com/extend", {"days": 5}), ("post", "/api/admin/kill", {}),
             ("post", "/api/admin/resume", {})]
    for method, url, body in calls:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, url


def test_overview_reports_runner_health_and_kill_switch(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    assert c.get("/api/admin/overview", headers=boss).json()["runner"]["alive"] is False
    with env["sf"]() as s:
        s.add(Heartbeat(name="runner", at=NOW, info="risk pass ok"))
        s.commit()
    env["clock"]["now"] = NOW + timedelta(seconds=60)
    ov = c.get("/api/admin/overview", headers=boss).json()
    assert ov["runner"] == {"alive": True, "seconds_since": 60, "info": "risk pass ok"} and len(ov["users"]) == 3
    env["clock"]["now"] = NOW + timedelta(seconds=900)
    assert c.get("/api/admin/overview", headers=boss).json()["runner"]["alive"] is False
    assert c.get("/api/health").json()["runner"]["alive"] is False


def test_admin_creates_updates_and_extends_users(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    r = c.post("/api/admin/users", headers=boss, json={"user_id": "dave@example.com", "subscription_days": 30,
                                                       "leverage": 4})
    assert r.status_code == 201 and r.json()["leverage"] == 4 and r.json()["subscription"]["active"]
    assert c.post("/api/admin/users", headers=boss, json={"user_id": "dave@example.com"}).status_code == 409
    r = c.patch("/api/admin/users/dave@example.com", headers=boss, json={"max_wallet_cap_usdt": 250, "leverage": 2})
    assert r.json()["max_wallet_cap_usdt"] == 250 and r.json()["leverage"] == 2
    assert c.patch("/api/admin/users/ghost", headers=boss, json={"leverage": 2}).status_code == 404
    r = c.post("/api/admin/users/dave@example.com/extend", headers=boss, json={"days": 10})
    assert r.status_code == 200
    audit = c.get("/api/admin/audit", headers=boss).json()
    assert {"create-user", "admin-update", "extend"} <= {a["action"] for a in audit}


def test_admin_cannot_exceed_eleven_profiles(env):
    boss = hdr(email="boss@example.com", sub="uid-boss")
    for i in range(8):
        assert env["client"].post("/api/admin/users", headers=boss, json={"user_id": f"u{i}"}).status_code == 201
    r = env["client"].post("/api/admin/users", headers=boss, json={"user_id": "one-too-many"})
    assert r.status_code == 400 and "limit reached" in r.json()["detail"]


def test_going_live_needs_keys_a_cap_and_explicit_confirmation(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live"})
    assert r.status_code == 400 and "no API keys" in r.json()["detail"] and "confirmation" in r.json()["detail"]
    c.put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr(email="bob@example.com", sub="uid-bob"))
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live", "confirm_real_money": True})
    assert r.status_code == 200 and r.json()["trading_mode"] == "live"


def test_kill_switch_over_the_api(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    assert c.post("/api/admin/kill", headers=boss, json={"flatten": True, "reason": "drill"}).json() == {"scope": "global", "flatten": True}
    ctrl = ControlStore(env["sf"]).effective("bob@example.com")
    assert ctrl.flatten and ctrl.reason == "drill"
    assert c.get("/api/me", headers=hdr(email="bob@example.com")).json()["control"]["flatten"] is True
    assert c.post("/api/admin/resume", headers=boss, json={}).json()["cleared"] is True
    assert not ControlStore(env["sf"]).effective("bob@example.com").paused
    assert c.post("/api/admin/kill", headers=boss, json={"user_id": "ghost"}).status_code == 404


# ------------------------------------------------------------------ billing: signatures
def test_js_number_formatting_matches_javascript():
    for x, want in {10.0: "10", 0.0148: "0.0148", 1e-7: "1e-7", 1.5e-6: "0.0000015", 123456.789: "123456.789",
                    1e21: "1e+21", 1e20: "100000000000000000000", -2.5: "-2.5", 0.1: "0.1", 5e-324: "5e-324"}.items():
        assert bill.js_number(x) == want


PAYLOAD = {"payment_id": 5745459419, "payment_status": "finished", "pay_address": "x", "price_amount": 15,
           "price_currency": "usd", "pay_amount": 0.00123, "actually_paid": 0.00123, "pay_currency": "btc",
           "order_id": "ORDER", "order_description": "G-Aegisbot", "fee": {"currency": "btc", "depositFee": 0.1, "serviceFee": 0}}


@pytest.mark.parametrize("variant", [0, 1, 2])
def test_signature_accepts_each_documented_canonical_form(variant):
    sig = bill.sign(PAYLOAD, IPN, variant)
    assert bill.verify_signature(PAYLOAD, sig, IPN) and bill.verify_signature(PAYLOAD, sig.upper(), IPN)


def test_signature_rejects_tampering_wrong_secret_and_missing_header():
    sig = bill.sign(PAYLOAD, IPN)
    assert not bill.verify_signature({**PAYLOAD, "price_amount": 1}, sig, IPN)
    assert not bill.verify_signature(PAYLOAD, sig, "other-secret")
    assert not bill.verify_signature(PAYLOAD, None, IPN) and not bill.verify_signature(PAYLOAD, "", IPN)
    assert not bill.verify_signature(PAYLOAD, "00" * 64, IPN)


def test_a_body_with_10_0_is_verified_when_the_sender_wrote_10():
    body = '{"order_id":"O","price_amount":10.0,"payment_status":"finished"}'
    sender_text = '{"order_id":"O","payment_status":"finished","price_amount":10}'      # JSON.stringify output
    import hashlib, hmac
    sig = hmac.new(IPN.encode(), sender_text.encode(), hashlib.sha512).hexdigest()
    assert bill.verify_signature(json.loads(body), sig, IPN)


# ------------------------------------------------------------------ billing: flow
def ipn(env, payload, signature=None, raw=None):
    body = raw if raw is not None else json.dumps(payload)
    sig = signature if signature is not None else bill.sign(payload, IPN)
    return env["client"].post("/api/billing/nowpayments/ipn", content=body,
                              headers={"x-nowpayments-sig": sig, "content-type": "application/json"})


def make_invoice(env, who="alice@example.com"):
    r = env["client"].post("/api/me/billing/invoice", headers=hdr(email=who))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def expiry(env, uid="alice@example.com"):
    e = get_profile(env, uid).subscription_expires_at
    return e.replace(tzinfo=timezone.utc) if e.tzinfo is None else e


def test_invoice_creation_sends_the_right_request_and_records_it(env):
    r = env["client"].post("/api/me/billing/invoice", headers=hdr())
    assert r.status_code == 200 and r.json()["invoice_url"].startswith("https://nowpayments.io/")
    call = env["billing"].calls[0]
    assert call["price_usd"] == 15.0 and call["ipn_url"] == "https://api.example.com/api/billing/nowpayments/ipn"
    assert call["order_id"] == r.json()["order_id"] and call["success_url"] == "https://dash.example.com/billing?paid=1"
    with env["sf"]() as s:
        row = s.scalar(select(Payment))
    assert row.user_id == "alice@example.com" and row.status == "created" and not row.credited and row.invoice_id == "inv-1"


def test_invoice_failures_and_disabled_billing(env):
    env["billing"].fail = True
    assert env["client"].post("/api/me/billing/invoice", headers=hdr()).status_code == 502
    app = create_app(ApiSettings(jwt_secret=SECRET), env["sf"], env["box"])
    assert TestClient(app).post("/api/me/billing/invoice", headers=hdr()).status_code == 503
    assert TestClient(app).post("/api/billing/nowpayments/ipn", json={}).status_code == 503


def test_a_finished_payment_extends_the_subscription_exactly_once(env):
    order = make_invoice(env)
    before = expiry(env)
    payload = {**PAYLOAD, "order_id": order}
    assert ipn(env, payload).json() == {"result": "credited"}
    assert expiry(env) == before + timedelta(days=30)
    assert ipn(env, payload).json() == {"result": "duplicate"}               # NOWPayments retries; nothing double-credits
    assert ipn(env, {**payload, "payment_status": "finished"}).json() == {"result": "duplicate"}
    assert expiry(env) == before + timedelta(days=30)
    bill_view = env["client"].get("/api/me/billing", headers=hdr()).json()
    assert bill_view["payments"][0]["credited"] is True and bill_view["price_usd"] == 15.0
    assert any(a["action"] == "billing-credit" for a in env["client"].get("/api/admin/audit", headers=hdr(email="boss@example.com")).json())


def test_payment_progress_updates_status_without_crediting(env):
    order = make_invoice(env)
    before = expiry(env)
    for status in ("waiting", "confirming", "confirmed", "sending", "partially_paid", "failed", "expired", "refunded"):
        assert ipn(env, {**PAYLOAD, "order_id": order, "payment_status": status}).json() == {"result": "updated"}
        with env["sf"]() as s:
            row = s.scalar(select(Payment))
        assert row.status == status and not row.credited
    assert expiry(env) == before


def test_wrong_amount_or_currency_is_not_credited(env):
    order = make_invoice(env)
    before = expiry(env)
    assert ipn(env, {**PAYLOAD, "order_id": order, "price_amount": 1}).json() == {"result": "rejected"}
    assert ipn(env, {**PAYLOAD, "order_id": order, "price_currency": "eur"}).json() == {"result": "rejected"}
    assert expiry(env) == before
    with env["sf"]() as s:
        assert not s.scalar(select(Payment)).credited


def test_bad_signature_malformed_body_and_unknown_order(env):
    order = make_invoice(env)
    before = expiry(env)
    payload = {**PAYLOAD, "order_id": order}
    assert ipn(env, payload, signature="00" * 64).status_code == 401
    assert ipn(env, payload, signature="").status_code == 401
    assert ipn(env, payload, raw="not json").status_code == 400
    assert ipn(env, payload, raw="[1,2]").status_code == 400
    assert ipn(env, {**PAYLOAD, "order_id": "no-such-order"}).json() == {"result": "ignored"}
    assert expiry(env) == before


def test_process_ipn_is_idempotent_under_repeated_calls(env):
    order = make_invoice(env)
    before = expiry(env)
    results = [bill.process_ipn(env["sf"], {**PAYLOAD, "order_id": order}, NOW).action for _ in range(5)]
    assert results == ["credited", "duplicate", "duplicate", "duplicate", "duplicate"]
    assert expiry(env) == before + timedelta(days=30)


def test_payment_for_a_deleted_profile_is_not_credited(env):
    order = make_invoice(env, "bob@example.com")
    with env["sf"]() as s:
        s.delete(s.scalar(select(UserProfile).where(UserProfile.user_id == "bob@example.com")))
        s.commit()
    assert bill.process_ipn(env["sf"], {**PAYLOAD, "order_id": order}, NOW).action == "rejected"


async def test_nowpayments_client_posts_the_invoice_with_the_api_key_header():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["key"], seen["body"] = str(request.url), request.headers.get("x-api-key"), json.loads(request.content)
        return httpx.Response(200, json={"id": "9", "invoice_url": "https://pay/9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = bill.NowPaymentsClient("np-key", "https://api-sandbox.nowpayments.io/v1", http)
        out = await client.create_invoice(order_id="O1", price_usd=15.0, description="d", ipn_url="https://x/ipn",
                                          success_url=None, cancel_url=None)
    assert out["invoice_url"] == "https://pay/9" and seen["url"] == "https://api-sandbox.nowpayments.io/v1/invoice"
    assert seen["key"] == "np-key" and seen["body"]["price_currency"] == "usd" and seen["body"]["order_id"] == "O1"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))) as http:
        with pytest.raises(bill.BillingError, match="HTTP 400"):
            await bill.NowPaymentsClient("k", "https://x", http).create_invoice(
                order_id="O", price_usd=1, description="d", ipn_url="u", success_url=None, cancel_url=None)


# ------------------------------------------------------------------ general
def test_health_is_public_and_responses_carry_security_headers(env):
    r = env["client"].get("/api/health")
    assert r.status_code == 200 and r.json()["db"] is True and r.json()["billing_enabled"] is True
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_interactive_docs_are_off_by_default(env):
    assert env["client"].get("/api/docs").status_code == 404 and env["client"].get("/api/openapi.json").status_code == 404
