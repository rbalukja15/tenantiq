"""DRF authenticator: Bearer JWT -> verified claims -> tenant + user + membership.

Attaches ``request.tenant``/``request.tenant_membership`` as a view-layer convenience. The
authoritative per-request tenant *context* (contextvar + Postgres GUC for RLS) is set by the
request middleware in #8, which reuses the same resolution seam.
"""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework import authentication, exceptions

from app.auth.tenancy import get_or_create_user_and_membership
from app.auth.verifier import TokenError
from app.tenant_context import activate_tenant


class TenantOIDCAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header:
            return None  # no credentials -> permission layer returns 401 (see authenticate_header)
        parts = header.split()
        if parts[0].lower() != self.keyword.lower():
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Invalid Authorization header.")
        try:
            claims, tenant = self._verifier().verify(parts[1])
        except TokenError as exc:
            raise exceptions.AuthenticationFailed("Invalid or expired token.") from exc
        user, membership = get_or_create_user_and_membership(claims, tenant)
        request.tenant = tenant
        request.tenant_membership = membership
        # Activate both isolation layers for the rest of this request: the contextvar (Layer 1)
        # and, on Postgres, the SET LOCAL GUC the RLS policy reads (Layer 2). See ADR-0002.
        activate_tenant(tenant)
        return (user, parts[1])

    def authenticate_header(self, request) -> str:
        # Required so an unauthenticated request gets 401 (not 403).
        return f'{self.keyword} realm="api"'

    @staticmethod
    def _verifier():
        # Read the factory on every call so tests can override_settings to inject a local-key
        # verifier; never memoize at import time.
        factory = settings.TENANTIQ_TOKEN_VERIFIER_FACTORY
        if isinstance(factory, str):
            factory = import_string(factory)
        return factory()
