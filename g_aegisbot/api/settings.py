from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ApiSettings:
    jwt_secret: str | None = None          # Supabase "JWT secret" (HS256 projects)
    jwks_url: str | None = None            # asymmetric Supabase projects: <url>/auth/v1/.well-known/jwks.json
    jwt_audience: str = "authenticated"
    jwt_issuer: str | None = None
    # Trusted email-confirmation lookup (first-login binding by email). Both are needed; without them automatic
    # email binding is OFF and every profile must be bound by the operator (admin bind-login). The anon key is the
    # project's PUBLIC key (the same one the dashboard ships), not a secret and not the service-role key.
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    cors_origins: tuple[str, ...] = ()
    docs: bool = False
    runner_stale_seconds: int = 180
    # billing (NOWPayments). Disabled unless the key, IPN secret and price are all set.
    now_api_key: str | None = None
    now_ipn_secret: str | None = None
    now_base_url: str = "https://api.nowpayments.io/v1"
    public_api_url: str | None = None      # where NOWPayments can reach this API, for the IPN callback
    dashboard_url: str | None = None
    price_usd: float = 0.0
    subscription_days: int = 30

    @property
    def auth_configured(self) -> bool:
        return bool(self.jwt_secret or self.jwks_url)

    @property
    def email_binding_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def billing_enabled(self) -> bool:
        return bool(self.now_api_key and self.now_ipn_secret and self.price_usd > 0 and self.public_api_url)


def load_api_settings(env=None) -> ApiSettings:
    e = os.environ if env is None else env
    supabase = (e.get("SUPABASE_URL") or "").rstrip("/")
    return ApiSettings(
        jwt_secret=e.get("SUPABASE_JWT_SECRET") or None,
        jwks_url=e.get("SUPABASE_JWKS_URL") or (f"{supabase}/auth/v1/.well-known/jwks.json" if supabase else None),
        jwt_issuer=e.get("SUPABASE_JWT_ISSUER") or (f"{supabase}/auth/v1" if supabase else None),
        supabase_url=supabase or None,
        supabase_anon_key=e.get("SUPABASE_ANON_KEY") or None,
        cors_origins=tuple(o.strip() for o in (e.get("AEGIS_CORS_ORIGINS") or "").split(",") if o.strip()),
        docs=(e.get("AEGIS_API_DOCS") or "").lower() in ("1", "true", "yes"),
        now_api_key=e.get("NOWPAYMENTS_API_KEY") or None,
        now_ipn_secret=e.get("NOWPAYMENTS_IPN_SECRET") or None,
        now_base_url=(e.get("NOWPAYMENTS_BASE_URL") or "https://api.nowpayments.io/v1").rstrip("/"),
        public_api_url=(e.get("AEGIS_PUBLIC_API_URL") or "").rstrip("/") or None,
        dashboard_url=(e.get("AEGIS_DASHBOARD_URL") or "").rstrip("/") or None,
        price_usd=float(e.get("SUBSCRIPTION_PRICE_USD") or 0),
        subscription_days=int(e.get("SUBSCRIPTION_DAYS") or 30),
    )
