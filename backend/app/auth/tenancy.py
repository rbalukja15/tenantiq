"""Tenant resolution + user provisioning from verified token claims.

``tenant_for_issuer`` is the shared seam: the #7 verifier/authenticator and the #8 request
middleware all resolve a tenant the same way — by its unique OIDC issuer — so the mapping is
written and tested once.
"""

from __future__ import annotations

import hashlib

from django.contrib.auth import get_user_model
from django.db import transaction

from app.models import Tenant, TenantMembership


def tenant_for_issuer(issuer: str) -> Tenant | None:
    if not issuer:
        return None
    return Tenant.objects.filter(oidc_issuer=issuer, is_active=True).first()


def _synthesize_username(sub: str, issuer: str) -> str:
    # Unique per (issuer, sub): two Keycloak realms can share a host, so key on the full issuer.
    issuer_hash = hashlib.sha256(issuer.encode()).hexdigest()[:12]
    return f"{sub}.{issuer_hash}"[:150]


def get_or_create_user_and_membership(claims: dict, tenant: Tenant):
    """Provision the ``(issuer, sub)`` user and ensure their membership in ``tenant``.

    Idempotent: repeated logins reuse the same user + membership (backed by the DB unique
    constraints). Email is descriptive only — identity is ``(oidc_issuer, oidc_sub)``.
    """
    user_model = get_user_model()
    sub = claims["sub"]
    issuer = claims["iss"]
    with transaction.atomic():
        user, _ = user_model.objects.get_or_create(
            oidc_issuer=issuer,
            oidc_sub=sub,
            defaults={
                "username": _synthesize_username(sub, issuer),
                "email": claims.get("email", ""),
            },
        )
        membership, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return user, membership
