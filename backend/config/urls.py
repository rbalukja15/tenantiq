"""Root URL configuration."""

from django.urls import path

from app.views import DocumentListView, MeView

urlpatterns = [
    path("api/me", MeView.as_view(), name="me"),
    path("api/documents", DocumentListView.as_view(), name="documents"),
]
