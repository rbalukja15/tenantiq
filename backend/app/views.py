"""API views."""

from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
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


class DocumentRetryView(APIView):
    """Re-run ingestion for a FAILED document (issue #13).

    The document is fetched through the tenant-scoped ``Document.objects`` manager, so a caller can
    only ever retry their own tenant's document — another tenant's id resolves to 404, never a
    cross-tenant action. Only a FAILED document is retryable; anything else is a 409.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        document = get_object_or_404(Document.objects, pk=pk)
        # Atomically claim the FAILED -> PENDING transition. Doing it as one conditional UPDATE
        # means two concurrent retries can't both pass a "status == FAILED" check and enqueue two
        # ingestion tasks for the same document — only the update matching FAILED wins.
        claimed = Document.objects.filter(pk=document.pk, status=Document.Status.FAILED).update(
            status=Document.Status.PENDING, error="", updated_at=timezone.now()
        )
        if not claimed:
            return Response(
                {"detail": "Only a failed document can be retried."},
                status=status.HTTP_409_CONFLICT,
            )
        document.refresh_from_db()
        tenant_id = request.tenant.id
        transaction.on_commit(lambda: ingest_document.delay(document.id, tenant_id))
        return Response(DocumentSerializer(document).data)
