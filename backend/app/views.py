"""API views."""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from app.models import Document


class MeView(APIView):
    """Who am I + which tenant. The frontend's session probe and the auth test surface.

    Deliberately does not expose tenant OIDC config (e.g. client id).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        tenant = request.tenant
        return Response(
            {
                "username": request.user.username,
                "email": request.user.email,
                "tenant": {"id": str(tenant.id), "slug": tenant.slug, "name": tenant.name},
            }
        )


class DocumentListView(APIView):
    """List the caller's documents. ``Document.objects`` is tenant-scoped, so this is the same
    query for every tenant yet can only ever return the caller's own rows (ADR-0002, #8)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        documents = Document.objects.order_by("created_at").values("id", "title", "created_at")
        return Response(list(documents))
