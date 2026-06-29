"""API views."""

from __future__ import annotations

from django.db import transaction
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from app.models import Document
from app.serializers import DocumentSerializer
from app.tasks import ingest_document


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


class DocumentListCreateView(generics.ListCreateAPIView):
    """List the caller's documents, or upload a new one. ``Document.objects`` is tenant-scoped, so
    the list can only ever return the caller's rows, and an upload is bound to the caller's tenant
    (ADR-0002, #8). The raw file is validated and stored; the row starts at PENDING (#10)."""

    permission_classes = [IsAuthenticated]
    serializer_class = DocumentSerializer

    def get_queryset(self):
        return Document.objects.order_by("created_at")

    def perform_create(self, serializer: DocumentSerializer) -> None:
        document = serializer.save(tenant=self.request.tenant)
        tenant_id = self.request.tenant.id
        # Enqueue only after the row is committed, so the worker can't race the request's
        # transaction (ATOMIC_REQUESTS). The worker has no request, so we pass the tenant id.
        transaction.on_commit(lambda: ingest_document.delay(document.id, tenant_id))
