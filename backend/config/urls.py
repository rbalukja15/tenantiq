"""Root URL configuration."""

from django.urls import path

from app.views import MeView

urlpatterns = [
    path("api/me", MeView.as_view(), name="me"),
]
