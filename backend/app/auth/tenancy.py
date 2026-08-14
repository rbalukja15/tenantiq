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


#: Claims that may carry a human label, best first. ``preferred_username`` is what Keycloak puts a
#: person's login name in; ``name`` is their full name; ``email`` is the last resort because it is
#: an address rather than a name, but it is still something they recognise as themselves.
DISPLAY_NAME_CLAIMS = ("preferred_username", "name", "email")


def display_name_from(claims: dict) -> str:
    """The IdP's own label for this person, or ``""`` when the token carries none.

    Empty is a real, expected answer — a token is only *required* to carry ``sub`` and ``iss``, and a
    minimally-configured client scope may send nothing else. Returning the synthesized username as a
    fallback is precisely the bug this exists to fix (#84): it puts an opaque
    ``<uuid>.<issuer-hash>`` where a name belongs. The caller says "signed in" without a name
    instead, which is at least true.
    """
    for claim in DISPLAY_NAME_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()[:255]
    return ""


def get_or_create_user_and_membership(claims: dict, tenant: Tenant):
    """Provision the ``(issuer, sub)`` user and ensure their membership in ``tenant``.

    Idempotent: repeated logins reuse the same user + membership (backed by the DB unique
    constraints). Email and display name are descriptive only — identity is
    ``(oidc_issuer, oidc_sub)``, and neither is ever used to look a user up.
    """
    user_model = get_user_model()
    sub = claims["sub"]
    issuer = claims["iss"]
    display_name = display_name_from(claims)
    with transaction.atomic():
        user, _ = user_model.objects.get_or_create(
            oidc_issuer=issuer,
            oidc_sub=sub,
            defaults={
                "username": _synthesize_username(sub, issuer),
                "email": claims.get("email", ""),
                "display_name": display_name,
            },
        )
        # Renaming yourself at the IdP has to reach the UI, so the stored label is refreshed from
        # every token — but only *written* when it actually changed, so the common case stays a read.
        # Guarded on a non-empty value: a token that simply omits the claim must not blank out a name
        # an earlier, fuller token established.
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            user.save(update_fields=["display_name"])
        membership, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return user, membership
