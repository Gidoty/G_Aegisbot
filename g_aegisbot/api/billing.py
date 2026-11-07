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
class _Num(str):
    """A JSON number kept as the exact text the sender wrote, so '10.0' is never silently rewritten to '10'."""


def _parse_raw(body: str | bytes):
    return json.loads(body, parse_float=_Num, parse_int=_Num, parse_constant=_reject_constant)


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def canonical(obj) -> str:
    """THE canonical form that is signed: object keys sorted at every level, no whitespace (separators ',' and ':'),
    non-ASCII characters unescaped, numbers exactly as received. This is the only form accepted; there are no fallbacks."""
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(obj[k]) for k in sorted(obj)) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(canonical(v) for v in obj) + "]"
    if isinstance(obj, _Num):
        return str(obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    return json.dumps(obj)                                    # Python numbers handed in directly (tests, tools)


def sign(body: "str | bytes | dict", secret: str) -> str:
    """HMAC-SHA512 (hex) of the canonical form of a request body. Strings and bytes keep their number literals."""
    obj = body if isinstance(body, dict) else _parse_raw(body)
    return hmac.new(secret.encode(), canonical(obj).encode(), hashlib.sha512).hexdigest()


def verify_signature(body: "str | bytes | dict", signature: str | None, secret: str) -> bool:
    """Constant-time check of the x-nowpayments-sig header against the canonical form of the RAW request body."""
    if not signature or not secret:
        return False
    try:
        expected = sign(body, secret)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature.strip().lower())


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
