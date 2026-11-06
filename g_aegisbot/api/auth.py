"""Supabase JWT verification.

Supports both Supabase signing modes: a shared HS256 secret (older projects) and asymmetric keys
published as JWKS (RS256/ES256). The accepted algorithm is decided by what is configured, never by
what the token claims, so a forged 'alg' cannot downgrade verification.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str      # lowercased; empty if the token carries none


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
        return Identity(sub=str(claims["sub"]), email=str(claims.get("email") or "").lower())
