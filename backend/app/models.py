"""Tenancy models.

Identity note: an OIDC ``sub`` is only unique *within* an issuer, so the real identity key
is ``(oidc_issuer, oidc_sub)`` — never email (mutable, sometimes unverified). See ADR-0002.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user so we can key identity on the OIDC subject + issuer.

    Defined up front (before the first migration) because swapping ``AUTH_USER_MODEL`` later
    is famously painful. Extra fields are blank-able so non-OIDC users (e.g. a created
    superuser) remain valid.
    """

    oidc_issuer = models.CharField(max_length=500, blank=True)
    oidc_sub = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["oidc_issuer", "oidc_sub"],
                name="unique_oidc_identity",
                condition=models.Q(oidc_sub__gt=""),
            ),
        ]


class Tenant(models.Model):
    """An isolated customer. ``oidc_issuer`` is unique and is how a token routes to a tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True, max_length=63)
    name = models.CharField(max_length=255)
    oidc_issuer = models.URLField(unique=True, max_length=500)
    oidc_client_id = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.slug} ({self.name})"


class TenantMembership(models.Model):
    """Links a user to a tenant. The canonical "this user belongs to this tenant" record."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="unique_user_tenant"),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.tenant.slug}"
