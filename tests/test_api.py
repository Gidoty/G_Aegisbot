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
from sqlalchemy import create_engine, func, select, text

from g_aegisbot.api import billing as bill
from g_aegisbot.api.app import create_app
from g_aegisbot.api.auth import AuthError, Authenticator
from g_aegisbot.api.identity import claim_profile
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


def token(email="alice@example.com", sub=None, secret=SECRET, alg="HS256", aud="authenticated", exp=3600, **extra):
    """A Supabase-style access token. Extra claims (for example user_metadata) are passed through untouched."""
    sub = sub or "uid-" + (email.split("@")[0].lower() or "anon")       # each login has its own immutable id
    claims = {"sub": sub, "email": email, "aud": aud, "exp": int(time.time()) + exp, **extra}
    return jwt.encode(claims, secret, algorithm=alg)


class FakeEmailChecker:
    """Stands in for the trusted Supabase Auth lookup. `result` is a bool or a function (sub, email) -> bool."""

    def __init__(self, result=True):
        self.result, self.calls = result, []

    def confirmed(self, token, *, sub, email):
        self.calls.append((sub, email))
        return self.result(sub, email) if callable(self.result) else self.result


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
    # Admin profiles are never claimed on first login; the operator binds them (python -m g_aegisbot.admin bind-login).
    with sf() as s:
        boss_id = s.scalar(select(UserProfile.id).where(UserProfile.user_id == "boss@example.com"))
    assert claim_profile(sf, boss_id, "uid-boss")
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
    checker = FakeEmailChecker(True)
    app = create_app(settings, sf, box, key_checker=key_checker, billing_client=billing, clock=lambda: clock["now"],
                     email_checker=checker)
    return dict(client=TestClient(app), checker=checker, sf=sf, box=box, state=state, billing=billing, engine=engine, clock=clock,
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
    assert r.status_code == 403 and "could not be linked to a G-Aegisbot profile" in r.json()["detail"]


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


def test_an_admin_cannot_create_a_live_account_directly_and_nothing_is_stored(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    with env["sf"]() as s:
        before = s.scalar(select(func.count()).select_from(UserProfile))
    r = c.post("/api/admin/users", headers=boss, json={"user_id": "live1@example.com", "trading_mode": "live",
                                                       "max_wallet_cap_usdt": 500})
    assert r.status_code == 422 and "cannot be created in live mode" in r.text
    r = c.post("/api/admin/users", headers=boss, json={"user_id": "weird@example.com", "trading_mode": "paper"})
    assert r.status_code == 422
    with env["sf"]() as s:
        assert s.scalar(select(func.count()).select_from(UserProfile)) == before
        assert s.scalar(select(UserProfile).where(UserProfile.user_id.in_(["live1@example.com", "weird@example.com"]))) is None


def test_an_admin_creates_a_paper_account_with_the_default_balance_and_cap(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    for body in ({"user_id": "p1@example.com"}, {"user_id": "p2@example.com", "trading_mode": "testnet"}):
        r = c.post("/api/admin/users", headers=boss, json=body)
        assert r.status_code == 201 and r.json()["trading_mode"] == "testnet" and r.json()["max_wallet_cap_usdt"] == 10000


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
PAYLOAD = {"payment_id": 5745459419, "payment_status": "finished", "pay_address": "x", "price_amount": 15,
           "price_currency": "usd", "pay_amount": 0.00123, "actually_paid": 0.00123, "pay_currency": "btc",
           "order_id": "ORDER", "order_description": "G-Aegisbot", "fee": {"currency": "btc", "depositFee": 0.1, "serviceFee": 0}}


def test_signature_uses_the_one_canonical_form():
    manual = '{"fee":{"currency":"btc","depositFee":0.1,"serviceFee":0},"order_description":"G-Aegisbot","order_id":"ORDER",' \
             '"pay_address":"x","pay_amount":0.00123,"pay_currency":"btc","payment_id":5745459419,' \
             '"payment_status":"finished","price_amount":15,"price_currency":"usd","actually_paid":0.00123}'
    import hashlib, hmac
    body = json.dumps(PAYLOAD)
    assert bill.canonical(json.loads(body)) != manual            # (key order differs in my literal on purpose)
    sig = bill.sign(body, IPN)
    assert bill.verify_signature(body, sig, IPN) and bill.verify_signature(body, sig.upper(), IPN)
    assert sig == hmac.new(IPN.encode(), json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":"),
                                                    ensure_ascii=False).encode(), hashlib.sha512).hexdigest()


def test_signature_rejects_tampering_wrong_secret_and_missing_header():
    body = json.dumps(PAYLOAD)
    sig = bill.sign(body, IPN)
    assert not bill.verify_signature(json.dumps({**PAYLOAD, "price_amount": 1}), sig, IPN)
    assert not bill.verify_signature(body, sig, "other-secret")
    assert not bill.verify_signature(body, None, IPN) and not bill.verify_signature(body, "", IPN)
    assert not bill.verify_signature(body, "00" * 64, IPN)
    assert not bill.verify_signature("not json", sig, IPN) and not bill.verify_signature('{"a": NaN}', sig, IPN)


def test_number_literals_are_signed_as_received_so_no_second_format_is_accepted():
    import hashlib, hmac
    body_10_0 = '{"order_id":"O","payment_status":"finished","price_amount":10.0}'
    sig_for_10_0 = hmac.new(IPN.encode(), body_10_0.encode(), hashlib.sha512).hexdigest()
    assert bill.verify_signature(body_10_0, sig_for_10_0, IPN)
    # a signature computed over a re-rendered "10" must NOT validate a body that says 10.0 (and vice versa)
    sig_for_10 = hmac.new(IPN.encode(), body_10_0.replace("10.0", "10").encode(), hashlib.sha512).hexdigest()
    assert not bill.verify_signature(body_10_0, sig_for_10, IPN)


def test_key_order_and_whitespace_in_the_incoming_body_do_not_matter_but_escaping_style_is_fixed():
    import hashlib, hmac
    canon = '{"a":1,"b":"é","c":[1,2.50]}'
    sig = hmac.new(IPN.encode(), canon.encode(), hashlib.sha512).hexdigest()
    assert bill.verify_signature('{ "c": [1, 2.50],\n "b": "é", "a": 1 }', sig, IPN)
    assert bill.verify_signature('{"c":[1,2.50],"b":"\\u00e9","a":1}', sig, IPN)          # an escaped é is the same text


# ------------------------------------------------------------------ billing: flow
def ipn(env, payload, signature=None, raw=None):
    body = raw if raw is not None else json.dumps(payload)
    sig = signature if signature is not None else bill.sign(body if raw is None else payload, IPN)
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


# ------------------------------------------------------------------ identity: immutable Supabase user id
def bound_id(env, user_id):
    with env["sf"]() as s:
        return s.scalar(select(UserProfile.auth_user_id).where(UserProfile.user_id == user_id))


def test_first_login_binds_the_supabase_id_and_later_logins_use_it(env):
    c = env["client"]
    assert bound_id(env, "alice@example.com") is None
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub="sb-alice")).status_code == 200
    assert bound_id(env, "alice@example.com") == "sb-alice"
    # the same id keeps working even if the account's email changes
    r = c.get("/api/me", headers=hdr(email="alice.new@example.com", sub="sb-alice"))
    assert r.status_code == 200 and r.json()["user_id"] == "alice@example.com"


def test_a_different_id_with_the_same_email_cannot_take_over_a_bound_profile(env):
    c = env["client"]
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub="sb-alice")).status_code == 200
    r = c.get("/api/me", headers=hdr(email="alice@example.com", sub="sb-impostor"))
    assert r.status_code == 403
    assert bound_id(env, "alice@example.com") == "sb-alice"


def test_an_empty_email_never_matches_a_profile(env):
    with env["sf"]() as s:
        before = s.scalar(select(func.count()).select_from(UserProfile).where(UserProfile.auth_user_id.is_not(None)))
    r = env["client"].get("/api/me", headers=hdr(email="", sub="sb-nobody"))
    assert r.status_code == 403
    with env["sf"]() as s:
        assert s.scalar(select(func.count()).select_from(UserProfile).where(UserProfile.auth_user_id.is_not(None))) == before


def test_binding_is_audited_and_an_id_cannot_bind_two_profiles(env):
    c = env["client"]
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub="sb-shared")).status_code == 200
    # a second profile whose email the same Supabase id presents: the id is already bound, so it resolves to alice only
    r = c.get("/api/me", headers=hdr(email="boss@example.com", sub="sb-shared"))
    assert r.json()["user_id"] == "alice@example.com"
    assert bound_id(env, "boss@example.com") == "uid-boss"          # untouched: it was never reachable through this id
    from g_aegisbot import audit
    assert any(a.action == "login_bound" and a.target == "alice@example.com" for a in audit.recent(env["sf"], 20))


def test_legacy_profiles_keyed_by_supabase_id_still_work_and_get_bound(env):
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("sb-legacy")
    r = env["client"].get("/api/me", headers=hdr(email="", sub="sb-legacy"))
    assert r.status_code == 200 and r.json()["user_id"] == "sb-legacy"
    assert bound_id(env, "sb-legacy") == "sb-legacy"


def test_positions_and_trades_expose_their_strategy(env):
    with env["sf"]() as s:
        s.add(OpenTrade(user_id="alice@example.com", symbol="ETH/USDT:USDT", side="buy", entry_price=100.0,
                        stop_price=98.0, extreme_price=100.0, leverage=3, mode="paper", opened_at=NOW, strategy="trend"))
        s.commit()
    c = env["client"]
    pos = c.get("/api/me/positions", headers=hdr()).json()
    assert [p["strategy"] for p in pos] == ["trend"]


def test_profiles_created_with_an_email_remember_it_as_the_invite_email(env):
    with env["sf"]() as s:
        p = ProfileRepository(s, env["box"]).create("Dana@Example.com")
        assert p.login_email == "dana@example.com" and p.auth_user_id is None
    assert env["client"].get("/api/me", headers=hdr(email="dana@example.com", sub="sb-dana")).status_code == 200


def test_every_admin_route_rejects_anonymous_and_non_admin_callers(env):
    """Server-side enforcement: walks the real route table so a new admin endpoint without the guard fails here."""
    app = env["client"].app
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/admin")]
    assert len(routes) >= 8
    c = env["client"]
    for r in routes:
        path = r.path.replace("{user_id}", "bob@example.com").replace("{name}", "x")
        for method in r.methods - {"HEAD", "OPTIONS"}:
            anon = c.request(method, path)
            assert anon.status_code == 401, (method, r.path, anon.status_code)
            user = c.request(method, path, headers=hdr(email="bob@example.com"), json={})
            assert user.status_code == 403, (method, r.path, user.status_code)


# ------------------------------------------------------------------ C3: verified email, atomic claim, admin fail-closed
def me(env, **kw):
    return env["client"].get("/api/me", headers=hdr(**kw))


def test_verified_first_login_binds_and_is_audited(env):
    r = me(env, email="alice@example.com", sub="sb-alice")
    assert r.status_code == 200 and r.json()["user_id"] == "alice@example.com"
    assert bound_id(env, "alice@example.com") == "sb-alice"
    from g_aegisbot import audit
    assert any(a.action == "login_bound" and "via email" in a.detail for a in audit.recent(env["sf"], 20))


def test_an_email_the_trusted_lookup_does_not_confirm_cannot_claim_a_profile(env):
    env["checker"].result = False
    r = me(env, email="alice@example.com", sub="sb-attacker")
    assert r.status_code == 403 and "confirm your email" in r.json()["detail"]
    assert bound_id(env, "alice@example.com") is None                     # still claimable by the real owner
    env["checker"].result = True
    assert me(env, email="alice@example.com", sub="sb-alice").status_code == 200


def test_the_answer_is_identical_for_an_unconfirmed_email_an_unknown_email_and_an_admin_profile(env):
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("root@example.com", is_admin=True)
    env["checker"].result = False
    answers = [me(env, email=e, sub=f"sb-{i}").json()["detail"]
               for i, e in enumerate(["alice@example.com", "nobody@example.com", "root@example.com"])]
    assert len(set(answers)) == 1                                          # nothing reveals which profiles exist


def test_a_claim_in_the_token_is_never_enough_even_if_it_says_verified(env):
    env["checker"].result = False
    for extra in ({"user_metadata": {"email_verified": True}}, {"email_verified": True},
                  {"app_metadata": {"email_verified": True}}):
        r = me(env, email="alice@example.com", sub="sb-attacker", **extra)
        assert r.status_code == 403, extra
    assert bound_id(env, "alice@example.com") is None


def test_bound_users_log_in_without_any_lookup_even_when_the_auth_service_is_down(env):
    assert me(env, email="alice@example.com", sub="sb-alice").status_code == 200
    env["checker"].calls.clear()
    env["checker"].result = lambda sub, email: (_ for _ in ()).throw(RuntimeError("auth service down"))
    for _ in range(3):
        assert me(env, email="alice@example.com", sub="sb-alice").status_code == 200
    assert env["checker"].calls == []                                      # no first-login binding was attempted


def test_legacy_profiles_keyed_by_the_signed_sub_need_no_email_lookup(env):
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("sb-legacy-2")
    env["checker"].result = False
    assert me(env, email="", sub="sb-legacy-2").status_code == 200
    assert env["checker"].calls == []


def test_without_a_configured_lookup_email_binding_is_off_and_nothing_is_claimed(env, tmp_path):
    from dataclasses import replace
    app = create_app(replace(env["settings"], supabase_url=None, supabase_anon_key=None), env["sf"], env["box"],
                     billing_client=env["billing"], clock=lambda: NOW)
    c = TestClient(app)
    assert not env["settings"].email_binding_enabled
    r = c.get("/api/me", headers=hdr(email="alice@example.com", sub="sb-alice"))
    assert r.status_code == 403 and bound_id(env, "alice@example.com") is None
    # operator binding still works, and then the login is fine without any lookup
    from g_aegisbot.admin import Admin, run
    uid = "11111111-2222-3333-4444-555555555555"
    assert run(["bind-login", "alice@example.com", uid], Admin(env["sf"], env["box"], out=lambda x: None)) == 0
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub=uid)).status_code == 200


def test_settings_enable_email_binding_only_with_both_the_url_and_the_public_key():
    from g_aegisbot.api.settings import load_api_settings
    assert not load_api_settings({"SUPABASE_URL": "https://x.supabase.co"}).email_binding_enabled
    assert not load_api_settings({"SUPABASE_ANON_KEY": "anon"}).email_binding_enabled
    s = load_api_settings({"SUPABASE_URL": "https://x.supabase.co/", "SUPABASE_ANON_KEY": "anon"})
    assert s.email_binding_enabled and s.supabase_url == "https://x.supabase.co"


# ---- the real checker against a mocked Supabase Auth server
import httpx

AUTH_URL = "https://proj.supabase.co"
ALICE_UID = "0a1b2c3d-0000-4000-8000-00000000a11c"


def real_checker_app(env, handler):
    from g_aegisbot.api.auth import SupabaseEmailChecker
    seen = []

    def recording(request):
        seen.append(request)
        return handler(request)

    checker = SupabaseEmailChecker(AUTH_URL, "anon-public-key", client=httpx.Client(transport=httpx.MockTransport(recording)))
    app = create_app(env["settings"], env["sf"], env["box"], billing_client=env["billing"], clock=lambda: NOW,
                     email_checker=checker)
    return TestClient(app), seen


def user_json(sub=ALICE_UID, email="alice@example.com", confirmed="2026-03-01T10:00:00Z", **extra):
    body = {"id": sub, "email": email, "email_confirmed_at": confirmed, **extra}
    return httpx.Response(200, json=body)


def test_user_metadata_email_verified_true_cannot_authorize_a_claim(env):
    """The token says verified in user_metadata, but Supabase Auth itself says the email is NOT confirmed."""
    c, seen = real_checker_app(env, lambda req: user_json(confirmed=None, user_metadata={"email_verified": True}))
    r = c.get("/api/me", headers=hdr(email="alice@example.com", sub=ALICE_UID, user_metadata={"email_verified": True},
                                     email_verified=True))
    assert r.status_code == 403 and bound_id(env, "alice@example.com") is None and len(seen) == 1


def test_a_confirmed_email_from_the_trusted_lookup_claims_the_right_profile(env):
    c, seen = real_checker_app(env, lambda req: user_json())
    tok = token(email="alice@example.com", sub=ALICE_UID)
    r = c.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["user_id"] == "alice@example.com"
    assert bound_id(env, "alice@example.com") == ALICE_UID
    req = seen[0]
    assert str(req.url) == f"{AUTH_URL}/auth/v1/user" and req.headers["authorization"] == f"Bearer {tok}"
    assert req.headers["apikey"] == "anon-public-key"                       # the public key, never a service-role key
    # a second request is served from the binding: no further lookup
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200 and len(seen) == 1


@pytest.mark.parametrize("answer", [
    user_json(sub="00000000-0000-4000-8000-0000000000ff"),                  # a different user id than the token's
    user_json(email="mallory@example.com"),                                 # a different email than the one being bound
    user_json(email=None),
    user_json(confirmed=None),
    user_json(confirmed=""),
    user_json(confirmed="   "),
    user_json(confirmed=True),                                              # must be a timestamp string, not a flag
    httpx.Response(200, json={"id": ALICE_UID, "email": "alice@example.com",
                              "user_metadata": {"email_verified": True}}),   # metadata is never consulted
    httpx.Response(200, json=[{"id": ALICE_UID}]),
    httpx.Response(200, text="not json"),
    httpx.Response(401, json={"msg": "invalid JWT"}),
    httpx.Response(403, json={}),
    httpx.Response(404, json={}),
    httpx.Response(429, json={}),
    httpx.Response(500, json={}),
    httpx.Response(302, headers={"location": "https://evil.example/steal"}),   # redirects are not followed
])
def test_inconsistent_or_negative_auth_answers_reject_the_claim(env, answer):
    c, seen = real_checker_app(env, lambda req: answer)
    r = c.get("/api/me", headers=hdr(email="alice@example.com", sub=ALICE_UID))
    assert r.status_code == 403 and bound_id(env, "alice@example.com") is None
    assert len(seen) == 1                                                    # one call, and nothing followed a redirect


@pytest.mark.parametrize("boom", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("boom")])
def test_an_auth_lookup_failure_rejects_binding_and_a_later_success_works(env, boom):
    state = {"down": True}

    def handler(request):
        if state["down"]:
            raise boom
        return user_json()

    c, _ = real_checker_app(env, handler)
    r = c.get("/api/me", headers=hdr(email="alice@example.com", sub=ALICE_UID))
    assert r.status_code == 403 and bound_id(env, "alice@example.com") is None      # fails closed
    state["down"] = False
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub=ALICE_UID)).status_code == 200


def test_the_email_comparison_ignores_case_and_whitespace_but_nothing_else(env):
    c, _ = real_checker_app(env, lambda req: user_json(email=" Alice@Example.COM "))
    assert c.get("/api/me", headers=hdr(email="ALICE@example.com", sub=ALICE_UID)).status_code == 200


def test_a_real_checker_is_never_called_for_bound_users(env):
    c, seen = real_checker_app(env, lambda req: user_json())
    assert c.get("/api/me", headers=hdr(email="alice@example.com", sub=ALICE_UID)).status_code == 200
    seen.clear()
    for _ in range(3):
        assert c.get("/api/me", headers=hdr(email="whatever@example.com", sub=ALICE_UID)).status_code == 200
    assert seen == []


def test_two_logins_racing_for_one_profile_cannot_both_win(env):
    """Both logins read the profile as unbound, then both try to claim it. Only the first UPDATE can succeed."""
    from g_aegisbot.api import identity
    cand_a, _ = identity.find_unbound(env["sf"], sub="sb-a", email="alice@example.com")
    cand_b, _ = identity.find_unbound(env["sf"], sub="sb-b", email="alice@example.com")
    assert cand_a.id == cand_b.id
    assert identity.claim_profile(env["sf"], cand_a.id, "sb-a", "alice@example.com") is True
    assert identity.claim_profile(env["sf"], cand_b.id, "sb-b", "alice@example.com") is False
    assert bound_id(env, "alice@example.com") == "sb-a"


def test_many_concurrent_claims_produce_exactly_one_owner(env):
    from concurrent.futures import ThreadPoolExecutor
    from g_aegisbot.api import identity
    cand, _ = identity.find_unbound(env["sf"], sub="x", email="bob@example.com")
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: identity.claim_profile(env["sf"], cand.id, f"sb-{i}", "bob@example.com"), range(8)))
    assert results.count(True) == 1
    winner = bound_id(env, "bob@example.com")
    assert winner in {f"sb-{i}" for i in range(8)} and results[int(winner[3:])] is True


def test_the_api_rejects_the_loser_of_a_race_even_if_it_already_saw_the_profile_unbound(env, monkeypatch):
    from g_aegisbot.api import identity
    real = identity.find_unbound
    seen = {}

    def stale_read(sf, *, sub, email):
        cand, how = real(sf, sub=sub, email=email)
        seen[sub] = cand
        return cand, how

    monkeypatch.setattr(identity, "find_unbound", stale_read)
    first = me(env, email="alice@example.com", sub="sb-first")
    assert first.status_code == 200
    # the second login is handed the same stale "unbound" candidate that the first one read
    monkeypatch.setattr(identity, "find_unbound", lambda sf, *, sub, email: (seen["sb-first"], "email"))
    second = me(env, email="alice@example.com", sub="sb-second")
    assert second.status_code == 403 and "already linked" in second.json()["detail"]
    assert bound_id(env, "alice@example.com") == "sb-first"


def test_a_second_account_cannot_claim_an_already_bound_profile(env):
    assert me(env, email="alice@example.com", sub="sb-alice").status_code == 200
    r = me(env, email="alice@example.com", sub="sb-second")
    assert r.status_code == 403 and bound_id(env, "alice@example.com") == "sb-alice"


def test_an_ordinary_login_cannot_claim_an_admin_profile_by_email(env):
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("root@example.com", is_admin=True)
    r = me(env, email="root@example.com", sub="sb-attacker")           # verified email, but the profile is an admin one
    assert r.status_code == 403 and "linked by the operator" in r.json()["detail"]
    assert bound_id(env, "root@example.com") is None
    assert env["checker"].calls == []                                   # an admin profile never even reaches the lookup


def test_an_admin_profile_works_after_the_operator_binds_it_and_not_for_anyone_else(env):
    from g_aegisbot.admin import Admin, run
    with env["sf"]() as s:
        ProfileRepository(s, env["box"]).create("root@example.com", is_admin=True)
    out = []
    root_uid = "6f1e2d3c-0000-4000-8000-0000000000aa"
    code = run(["bind-login", "root@example.com", root_uid], Admin(env["sf"], env["box"], out=out.append, actor="op"))
    assert code == 0 and bound_id(env, "root@example.com") == root_uid
    ok = me(env, email="root@example.com", sub=root_uid)
    assert ok.status_code == 200 and ok.json()["is_admin"] is True
    assert me(env, email="root@example.com", sub="sb-attacker").status_code == 403


def test_a_bound_user_still_logs_in_after_an_email_change_even_with_an_unconfirmed_new_address(env):
    assert me(env, email="alice@example.com", sub="sb-alice").status_code == 200
    env["checker"].result = False
    r = me(env, email="alice.new@example.com", sub="sb-alice")
    assert r.status_code == 200 and r.json()["user_id"] == "alice@example.com"


def test_binding_changes_nothing_about_roles_subscription_or_ownership(env):
    with env["sf"]() as s:
        before = s.scalar(select(UserProfile).where(UserProfile.user_id == "bob@example.com"))
        snap = (before.is_admin, before.subscription_status, before.subscription_expires_at, before.trading_mode,
                before.leverage, str(before.max_wallet_cap_usdt), before.user_id)
    assert me(env, email="bob@example.com", sub="sb-bob").status_code == 200
    with env["sf"]() as s:
        after = s.scalar(select(UserProfile).where(UserProfile.user_id == "bob@example.com"))
        assert (after.is_admin, after.subscription_status, after.subscription_expires_at, after.trading_mode,
                after.leverage, str(after.max_wallet_cap_usdt), after.user_id) == snap
        assert after.auth_user_id == "sb-bob"


# ------------------------------------------------------------------ bind-login is operator-only: nothing in the API can bind
def test_no_api_route_can_bind_or_rebind_a_login(env):
    app = env["client"].app
    paths = [getattr(r, "path", "") for r in app.routes]
    assert not [p for p in paths if "bind" in p.lower() or "auth_user" in p.lower()]
    import ast
    import inspect
    import g_aegisbot.api.app as app_module
    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(app_module))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not [m for m in imported if m.startswith("g_aegisbot.admin")]       # the API never imports the operator CLI
    assert not [n for n in dir(app_module) if "bind_login" in n.lower()]


def test_admin_and_user_endpoints_ignore_an_attempt_to_set_auth_user_id(env):
    boss = hdr(email="boss@example.com", sub="uid-boss")
    victim = "00000000-0000-4000-8000-0000000000bb"
    r = env["client"].post("/api/admin/users", headers=boss,
                           json={"user_id": "newbie@example.com", "subscription_days": 5, "auth_user_id": victim})
    assert r.status_code in (200, 201)
    assert bound_id(env, "newbie@example.com") is None
    r = env["client"].patch("/api/admin/users/bob@example.com", headers=boss, json={"auth_user_id": victim, "leverage": 4})
    assert r.status_code == 200 and bound_id(env, "bob@example.com") is None
    r = env["client"].put("/api/me/settings", headers=hdr(email="alice@example.com"), json={"auth_user_id": victim})
    assert bound_id(env, "alice@example.com") != victim


@pytest.mark.parametrize("boom", [RuntimeError("surprise"), KeyError("id"), TypeError("bad shape"), UnicodeError("x")])
def test_any_unexpected_exception_inside_the_email_checker_is_a_refusal_never_a_pass(boom):
    from g_aegisbot.api.auth import SupabaseEmailChecker
    c = SupabaseEmailChecker(AUTH_URL, "anon-public-key", client=httpx.Client(transport=httpx.MockTransport(lambda r: user_json())))
    c.lookup = lambda token: (_ for _ in ()).throw(boom)
    assert c.confirmed("t", sub=ALICE_UID, email="alice@example.com") is False


def test_the_checker_confirms_only_when_every_field_agrees():
    from g_aegisbot.api.auth import SupabaseEmailChecker
    ok = SupabaseEmailChecker(AUTH_URL, "k", client=httpx.Client(transport=httpx.MockTransport(lambda r: user_json())))
    assert ok.confirmed("t", sub=ALICE_UID, email="alice@example.com") is True
    assert ok.confirmed("t", sub=ALICE_UID, email="") is False                  # an empty email is never bound


def test_no_api_route_can_list_or_resolve_entry_orders(env):
    """Resolving a stuck entry order is an operator action on the server (admin CLI), never an HTTP route."""
    paths = [getattr(r, "path", "").lower() for r in env["client"].app.routes]
    assert not [p for p in paths if any(w in p for w in ("entry", "entries", "journal", "intent", "resolve", "reconcil"))]
    for method in ("get", "post", "put", "patch", "delete"):
        for path in ("/api/admin/entries", "/api/admin/resolve-entry", "/api/me/entries", "/api/admin/journal"):
            r = env["client"].request(method, path, headers=hdr(email="boss@example.com", sub="uid-boss"))
            assert r.status_code in (404, 405), (method, path, r.status_code)


# ------------------------------------------------------------------ key verification record (live-mode safeguard)
def test_the_key_upload_records_the_verification_time_with_the_ciphertext(env):
    c = env["client"]
    assert get_profile(env).keys_verified_at is None
    assert c.put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr()).status_code == 200
    p = get_profile(env)
    assert p.has_credentials and p.keys_verified_at.replace(tzinfo=timezone.utc) == NOW


def test_a_refused_key_leaves_the_previous_verification_untouched(env):
    c = env["client"]
    c.put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=hdr())
    before = get_profile(env)
    env["state"]["check_result"] = KeyCheck(False, ["WITHDRAWALS ARE ENABLED"])
    r = c.put("/api/me/keys", json={"api_key": "C" * 24, "api_secret": "D" * 24}, headers=hdr())
    after = get_profile(env)
    assert r.status_code == 422 and (after.encrypted_api_key, after.keys_verified_at) == (before.encrypted_api_key, before.keys_verified_at)


def test_api_live_activation_needs_current_verification_a_cap_and_confirmation(env):
    c, boss = env["client"], hdr(email="boss@example.com", sub="uid-boss")
    bob = hdr(email="bob@example.com", sub="uid-bob")
    c.put("/api/me/keys", json={"api_key": KEY, "api_secret": SEC}, headers=bob)
    env["clock"]["now"] = NOW + timedelta(days=8)                                          # verification is now stale
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live", "confirm_real_money": True})
    assert r.status_code == 400 and "older than 7 days" in r.json()["detail"]
    env["clock"]["now"] = NOW + timedelta(days=6)
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live"})
    assert r.status_code == 400 and "confirmation" in r.json()["detail"]                   # confirmation is still required
    r = c.patch("/api/admin/users/bob@example.com", headers=boss, json={"trading_mode": "live", "confirm_real_money": True})
    assert r.status_code == 200 and r.json()["trading_mode"] == "live"
