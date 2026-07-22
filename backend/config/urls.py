"""Root URL configuration."""

from django.urls import path

from app.views import DocumentListCreateView, DocumentRetryView, MeView, QueryView

urlpatterns = [
    path("api/me", MeView.as_view(), name="me"),
    path("api/documents", DocumentListCreateView.as_view(), name="documents"),
    path("api/documents/<int:pk>/retry", DocumentRetryView.as_view(), name="document-retry"),
    path("api/query", QueryView.as_view(), name="query"),
]
