"""Injectable per-tenant OIDC token verification.

Routing safety (ADR-0002): the *unverified* ``iss`` is read only to choose which tenant and
JWKS to verify against. Every trust decision is then made from server-stored ``Tenant`` config
and the post-verification claims — including a re-assertion that ``iss == tenant.oidc_issuer``
so a token signed by tenant B's IdP cannot be replayed as tenant A.

The verifier takes two injected seams so it is testable with a local key and no network:
- ``key_resolver(token, tenant) -> key``  (production: a JWKS signing key)
- ``tenant_lookup(iss) -> tenant | None`` (production: a DB lookup by ``oidc_issuer``)
"""

from __future__ import annotations

import logging
from typing import Callable

import jwt

logger = logging.getLogger(__name__)

LEEWAY_SECONDS = 60
ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iss", "aud", "sub"]

KeyResolver = Callable[[str, object], object]
TenantLookup = Callable[[str], object]


class TokenError(Exception):
    """Any invalid / expired / untrusted token. Maps to HTTP 401."""


def _decode_and_validate(token: str, key: object, *, issuer: str, audience: str) -> dict:
    """The single JWT validation path shared by every verifier."""
    try:
        return jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            issuer=issuer,
            audience=audience,
            leeway=LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS, "verify_signature": True},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError(str(exc)) from exc


class TenantTokenVerifier:
    def __init__(self, key_resolver: KeyResolver, tenant_lookup: TenantLookup) -> None:
        self._key_resolver = key_resolver
        self._tenant_lookup = tenant_lookup

    def verify(self, token: str) -> tuple[dict, object]:
        issuer = self._peek_issuer(token)
        tenant = self._tenant_lookup(issuer)
        if tenant is None:
            raise TokenError(f"no active tenant for issuer {issuer!r}")
        try:
            key = self._key_resolver(token, tenant)
        except TokenError:
            raise
        except Exception as exc:  # JWKS fetch / kid miss / network — fail closed.
            logger.warning("signing-key resolution failed for iss=%s: %s", issuer, exc)
            raise TokenError("unable to resolve signing key") from exc
        claims = _decode_and_validate(
            token, key, issuer=tenant.oidc_issuer, audience=tenant.oidc_client_id
        )
        if claims.get("iss") != tenant.oidc_issuer:
            raise TokenError("issuer mismatch after verification")
        return claims, tenant

    @staticmethod
    def _peek_issuer(token: str) -> str:
        try:
            unverified = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError("malformed token") from exc
        issuer = unverified.get("iss")
        if not issuer:
            raise TokenError("token has no issuer")
        return issuer


# --- Production wiring (exercised against a real Keycloak; tests inject a local-key verifier) ---

_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _jwks_key_resolver(token: str, tenant: object) -> object:
    issuer = tenant.oidc_issuer.rstrip("/")
    client = _jwks_clients.get(issuer)
    if client is None:
        client = jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs", lifespan=300, timeout=5)
        _jwks_clients[issuer] = client
    return client.get_signing_key_from_jwt(token).key


def build_default_verifier() -> TenantTokenVerifier:
    """Factory referenced by settings.TENANTIQ_TOKEN_VERIFIER_FACTORY in production."""
    from app.auth.tenancy import tenant_for_issuer

    return TenantTokenVerifier(key_resolver=_jwks_key_resolver, tenant_lookup=tenant_for_issuer)
