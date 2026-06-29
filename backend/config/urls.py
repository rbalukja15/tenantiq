"""Root URL configuration."""

from django.urls import path

from app.views import DocumentListCreateView, MeView

urlpatterns = [
    path("api/me", MeView.as_view(), name="me"),
    path("api/documents", DocumentListCreateView.as_view(), name="documents"),
]
