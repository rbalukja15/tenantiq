"""Tenancy models.

Identity note: an OIDC ``sub`` is only unique *within* an issuer, so the real identity key
is ``(oidc_issuer, oidc_sub)`` — never email (mutable, sometimes unverified). See ADR-0002.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

from app.tenant_context import NoActiveTenant, get_current_tenant_id


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


class TenantScopedManager(models.Manager):
    """Default manager for tenant-owned models (Layer 1, ADR-0002).

    Filters every queryset to the active tenant, and *raises* :class:`NoActiveTenant` when none is
    set — so "forgot to scope" is a loud error, never a silent all-tenant read. System paths that
    legitimately span tenants use the explicit ``all_objects`` manager instead.
    """

    def get_queryset(self):
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            raise NoActiveTenant(
                f"{self.model.__name__} accessed with no active tenant. Use tenant_context()/"
                "activate_tenant(), or the explicit all_objects manager for system access."
            )
        return super().get_queryset().filter(tenant_id=tenant_id)


class TenantOwnedModel(models.Model):
    """Abstract base for every tenant-owned table: a non-null ``tenant`` FK, the tenant-scoped
    default manager, and (on Postgres) row-level security added per table in the 0003 migration.

    ``base_manager_name`` points Django internals and reverse FK lookups at the unfiltered
    ``all_objects`` so they never trip the raising default manager.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        db_index=True,
        editable=False,
    )

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"

    def save(self, *args, **kwargs):
        current = get_current_tenant_id()
        if self.tenant_id is None:
            if current is None:
                raise NoActiveTenant(f"Cannot save {type(self).__name__} with no active tenant.")
            self.tenant_id = current
        elif current is not None and self.tenant_id != current:
            raise NoActiveTenant(
                f"Refusing to save {type(self).__name__} for tenant {self.tenant_id} while the "
                f"active tenant is {current}."
            )
        super().save(*args, **kwargs)


def tenant_document_path(instance: "Document", filename: str) -> str:
    """Store uploads under a per-tenant, non-guessable path so files are isolated on disk too."""
    return f"tenants/{instance.tenant_id}/documents/{uuid.uuid4()}/{filename}"


class Document(TenantOwnedModel):
    """A tenant's uploaded document and its ingestion status. The raw file lives on the configured
    storage under a tenant-scoped path; metadata (and, in M2, chunks + embeddings) live in
    Postgres. Status advances PENDING -> PROCESSING -> READY/FAILED as the pipeline runs (#11/#12).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    title = models.CharField(max_length=255)
    file = models.FileField(upload_to=tenant_document_path, max_length=500, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveBigIntegerField(default=0)
    original_filename = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title


class Chunk(TenantOwnedModel):
    """A contiguous piece of a document's extracted text — the unit that gets embedded (#12) and
    retrieved (M3). Tenant-owned, so it inherits both isolation layers."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    index = models.PositiveIntegerField()
    text = models.TextField()
    char_count = models.PositiveIntegerField(default=0)
    token_estimate = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["document", "index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "index"], name="unique_document_chunk_index"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id}#{self.index}"
