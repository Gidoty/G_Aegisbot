"""NOWPayments crypto billing: invoice creation, IPN signature check, idempotent crediting."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select, update

from g_aegisbot import audit
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.trading_models import Payment
from g_aegisbot.services import extend_subscription

log = logging.getLogger("aegis.billing")


class BillingError(Exception):
    pass


# ---------------------------------------------------------------- signature
def js_number(x: float) -> str:
    """Render a number exactly as JavaScript's JSON.stringify would (10.0 -> '10', 1e-7 -> '1e-7')."""
    if x == 0:
        return "0"
    sign, digits, exp = Decimal(repr(abs(x))).as_tuple()
    all_digits = "".join(map(str, digits))
    stripped = all_digits.lstrip("0")
    point = len(all_digits) + exp - (len(all_digits) - len(stripped))      # decimal point position (JS 'n')
    s = stripped.rstrip("0") or "0"
    k = len(s)
    neg = "-" if x < 0 else ""
    if k <= point <= 21:
        out = s + "0" * (point - k)
    elif 0 < point <= 21:
        out = s[:point] + "." + s[point:]
    elif -6 < point <= 0:
        out = "0." + "0" * (-point) + s
    else:
        e = point - 1
        out = s[0] + ("." + s[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return neg + out


def _js_canonical(obj) -> str:
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _js_canonical(obj[k]) for k in sorted(obj)) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_js_canonical(v) for v in obj) + "]"
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, int):
        return str(obj)
    return js_number(float(obj))


def canonical_candidates(payload: dict) -> list[str]:
    """The ways the sender may have serialised the sorted body. Any match proves the HMAC, so trying
    several costs nothing in security: the shared secret is still required to produce a match."""
    return [
        json.dumps(payload, separators=(",", ":"), sort_keys=True),                      # NOWPayments' Python example
        json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False),
        _js_canonical(payload),                                                           # JSON.stringify semantics
    ]


def sign(payload: dict, secret: str, variant: int = 0) -> str:
    return hmac.new(secret.encode(), canonical_candidates(payload)[variant].encode(), hashlib.sha512).hexdigest()


def verify_signature(payload: dict, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    sig = signature.strip().lower()
    return any(hmac.compare_digest(hmac.new(secret.encode(), c.encode(), hashlib.sha512).hexdigest(), sig)
               for c in canonical_candidates(payload))


# ---------------------------------------------------------------- client
class NowPaymentsClient:
    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient | None = None):
        self.api_key, self.base_url, self.http = api_key, base_url, http

    async def create_invoice(self, *, order_id: str, price_usd: float, description: str, ipn_url: str,
                             success_url: str | None, cancel_url: str | None) -> dict:
        body = {"price_amount": price_usd, "price_currency": "usd", "order_id": order_id,
                "order_description": description, "ipn_callback_url": ipn_url}
        if success_url:
            body["success_url"] = success_url
        if cancel_url:
            body["cancel_url"] = cancel_url
        client = self.http or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.post(f"{self.base_url}/invoice", json=body, headers={"x-api-key": self.api_key})
        except httpx.HTTPError as exc:
            raise BillingError(f"could not reach NOWPayments ({type(exc).__name__})") from exc
        finally:
            if self.http is None:
                await client.aclose()
        if resp.status_code >= 400:
            raise BillingError(f"NOWPayments refused the invoice (HTTP {resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        if not data.get("invoice_url"):
            raise BillingError("NOWPayments returned no invoice_url")
        return data


# ---------------------------------------------------------------- database side
def new_payment(sf, user_id: str, price_usd: float, days: int, now: datetime) -> Payment:
    with sf() as s:
        row = Payment(order_id=f"aeg-{uuid.uuid4().hex[:20]}", user_id=user_id, price_usd=price_usd, days=days,
                      status="created", created_at=now, updated_at=now)
        s.add(row)
        s.commit()
        return row


def attach_invoice(sf, order_id: str, invoice_id: str | None) -> None:
    with sf() as s:
        s.execute(update(Payment).where(Payment.order_id == order_id).values(invoice_id=str(invoice_id) if invoice_id else None))
        s.commit()


@dataclass
class IpnResult:
    action: str     # credited | updated | duplicate | ignored | rejected
    detail: str = ""


def process_ipn(sf, payload: dict, now: datetime) -> IpnResult:
    """Apply one verified IPN. Safe to deliver any number of times: the subscription is extended once."""
    order_id = payload.get("order_id")
    with sf() as s:
        row = s.scalar(select(Payment).where(Payment.order_id == str(order_id))) if order_id else None
        if row is None:
            return IpnResult("ignored", "unknown order_id")
        status = str(payload.get("payment_status") or "").lower()
        row.status, row.updated_at = status or row.status, now
        if payload.get("payment_id") is not None:
            row.payment_id = str(payload["payment_id"])
        if status != "finished":
            s.commit()
            if status == "partially_paid":
                log.warning("Payment %s for %s is only partially paid; not credited. Review it with the admin.",
                            row.order_id, row.user_id)
            return IpnResult("updated", status)
        try:
            ok_price = abs(float(payload.get("price_amount")) - row.price_usd) < 0.01
        except (TypeError, ValueError):
            ok_price = False
        if not ok_price or str(payload.get("price_currency", "")).lower() != "usd":
            s.commit()
            log.error("Payment %s finished but the amount/currency does not match the invoice; NOT credited.", row.order_id)
            return IpnResult("rejected", "amount or currency mismatch")
        profile = s.scalar(select(UserProfile).where(UserProfile.user_id == row.user_id))
        if profile is None:
            s.commit()
            log.error("Payment %s finished but profile %s no longer exists; NOT credited.", row.order_id, row.user_id)
            return IpnResult("rejected", "profile missing")
        claimed = s.execute(update(Payment).where(Payment.id == row.id, Payment.credited.is_(False))
                            .values(credited=True, credited_at=now)).rowcount
        if claimed != 1:                                   # another delivery already credited it
            s.commit()
            return IpnResult("duplicate", "already credited")
        extend_subscription(profile, row.days, now)
        s.commit()
        user, days = row.user_id, row.days
    audit.record(sf, "nowpayments", "billing-credit", user, f"+{days} days, order {order_id}")
    return IpnResult("credited", f"+{days} days")
