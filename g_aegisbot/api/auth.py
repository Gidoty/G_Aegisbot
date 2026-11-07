"""Supabase JWT verification.

Supports both Supabase signing modes: a shared HS256 secret (older projects) and asymmetric keys
published as JWKS (RS256/ES256). The accepted algorithm is decided by what is configured, never by
what the token claims, so a forged 'alg' cannot downgrade verification.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt


log = logging.getLogger("aegis.auth")


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str      # lowercased; empty if the token carries none. Claimed by the token, NOT proof of ownership.


class EmailCheckFailed(Exception):
    """The trusted lookup could not establish that the email is confirmed. The message is safe to log (no email, no token)."""


class SupabaseEmailChecker:
    """Establishes, from Supabase Auth itself, that a login owns a CONFIRMED email address.

    The JWT is not enough: its `email` and `user_metadata` are not proof (user_metadata is editable by the user). So
    the token is sent back to the project's own Auth server (GET {SUPABASE_URL}/auth/v1/user, the call supabase-js
    makes for getUser) and the answer is read from server-controlled fields:

      * `id` must equal the token's `sub`;
      * `email` must equal the email we are about to bind on;
      * `email_confirmed_at` must be set.

    Anything else (network error, timeout, non-200, redirect, malformed or inconsistent answer) is a refusal.
    Nothing here ever reads `user_metadata`. Caveat that no API call can remove: if the project has "Confirm email"
    switched off, Supabase stamps `email_confirmed_at` at sign-up without any proof, so keep it switched on."""

    def __init__(self, base_url: str, anon_key: str, *, client=None, timeout: float = 5.0):
        import httpx
        self.url = base_url.rstrip("/") + "/auth/v1/user"
        self.anon_key = anon_key
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def lookup(self, token: str) -> dict:
        try:
            resp = self.client.get(self.url, headers={"apikey": self.anon_key, "Authorization": f"Bearer {token}"})
        except Exception as exc:
            raise EmailCheckFailed(f"auth service unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise EmailCheckFailed(f"auth service answered HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise EmailCheckFailed("auth service returned a non-JSON answer") from exc
        if not isinstance(body, dict):
            raise EmailCheckFailed("auth service returned an unexpected answer")
        return body

    def confirmed(self, token: str, *, sub: str, email: str) -> bool:
        """True only when Supabase says this exact user (sub) owns this exact email and has confirmed it."""
        try:
            user = self.lookup(token)
            if not isinstance(user.get("id"), str) or user["id"] != sub:
                raise EmailCheckFailed("auth service returned a different user id than the token")
            got = user.get("email")
            if not email or not isinstance(got, str) or got.strip().lower() != email:
                raise EmailCheckFailed("auth service returned a different email than the one being bound")
            stamp = user.get("email_confirmed_at")
            if not isinstance(stamp, str) or not stamp.strip():
                raise EmailCheckFailed("the email address is not confirmed")
            return True
        except EmailCheckFailed as exc:
            log.warning("email confirmation refused: %s", exc)
            return False
        except Exception as exc:                                    # never let a surprise turn into a pass
            log.warning("email confirmation refused: unexpected %s", type(exc).__name__)
            return False


class Authenticator:
    def __init__(self, *, secret: str | None = None, jwks_client=None, audience: str = "authenticated",
                 issuer: str | None = None, leeway: int = 10):
        if not secret and jwks_client is None:
            raise ValueError("configure SUPABASE_JWT_SECRET or SUPABASE_URL / SUPABASE_JWKS_URL")
        self.secret, self.jwks, self.audience, self.issuer, self.leeway = secret, jwks_client, audience, issuer, leeway

    @classmethod
    def from_settings(cls, s) -> "Authenticator":
        client = jwt.PyJWKClient(s.jwks_url, cache_keys=True, lifespan=3600) if s.jwks_url else None
        return cls(secret=s.jwt_secret, jwks_client=client, audience=s.jwt_audience, issuer=s.jwt_issuer)

    def verify(self, token: str) -> Identity:
        try:
            alg = jwt.get_unverified_header(token).get("alg")
            if alg == "HS256" and self.secret:
                key, algs = self.secret, ["HS256"]
            elif alg in ("RS256", "ES256") and self.jwks is not None:
                key, algs = self.jwks.get_signing_key_from_jwt(token).key, [alg]
            else:
                raise AuthError("unsupported token type")
            claims = jwt.decode(token, key, algorithms=algs, audience=self.audience, issuer=self.issuer,
                                leeway=self.leeway, options={"require": ["exp", "sub"]})
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError) as exc:
            raise AuthError(type(exc).__name__) from exc
        return Identity(sub=str(claims["sub"]), email=str(claims.get("email") or "").strip().lower())
