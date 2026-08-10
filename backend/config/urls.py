"""Root URL configuration."""

from django.urls import path

from app.views import (
    DocumentListCreateView,
    DocumentRetryView,
    MeView,
    QueryView,
    TenantDiscoveryView,
    UsageView,
)

urlpatterns = [
    # The only unauthenticated route: the login page needs a tenant's OIDC issuer + client id
    # *before* it can obtain a token (#18, ADR-0013 §2).
    path("api/tenants/discovery", TenantDiscoveryView.as_view(), name="tenant-discovery"),
    path("api/me", MeView.as_view(), name="me"),
    path("api/documents", DocumentListCreateView.as_view(), name="documents"),
    path("api/documents/<int:pk>/retry", DocumentRetryView.as_view(), name="document-retry"),
    path("api/query", QueryView.as_view(), name="query"),
    path("api/usage", UsageView.as_view(), name="usage"),
]
