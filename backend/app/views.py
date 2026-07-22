"""API views."""

from __future__ import annotations

import json
from dataclasses import asdict

from django.db import transaction
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from app.generation import (
    CitationsEvent,
    ErrorEvent,
    TokenEvent,
    stream_grounded_answer,
)
from app.models import Document
from app.rag import retrieve_context
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


class _IgnoreClientContentNegotiation(BaseContentNegotiation):
    """Accept any ``Accept`` header. An SSE client sends ``Accept: text/event-stream``, which DRF's
    default JSON-only renderer set would reject with a 406 during request-side negotiation — before
    the view even runs. The streamed 200 body owns its own framing and bypasses rendering; the
    default renderer is used only to render the JSON error responses this view returns."""

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


def _sse_frame(event: TokenEvent | CitationsEvent | ErrorEvent) -> str:
    """Serialize one query event as a Server-Sent Events frame (``event:`` + JSON ``data:``)."""
    if isinstance(event, TokenEvent):
        return f"event: token\ndata: {json.dumps({'text': event.text})}\n\n"
    if isinstance(event, CitationsEvent):
        payload = {"citations": [asdict(citation) for citation in event.citations]}
        return f"event: citations\ndata: {json.dumps(payload)}\n\n"
    return f"event: error\ndata: {json.dumps({'message': event.message})}\n\n"


class QueryView(APIView):
    """Answer a question grounded in the caller's documents, streamed over SSE (#48, ADR-0009).

    **Retrieval runs here**, inside the request's tenant transaction — ``Document``/``Chunk`` are
    tenant-scoped (ADR-0002), so a query can only ever be grounded in the caller's chunks.
    **Generation streams outside** that transaction: the ``StreamingHttpResponse`` body is produced
    after this method returns and ``ATOMIC_REQUESTS`` has committed, so no DB connection is held open
    during the (slow) model call. The client consumes token frames over ``fetch`` +
    ``ReadableStream`` — native ``EventSource`` can't send the ``Authorization`` header — closing with
    a citations frame whose entries resolve to real chunk IDs.
    """

    permission_classes = [IsAuthenticated]
    content_negotiation_class = _IgnoreClientContentNegotiation

    def post(self, request: Request) -> Response | StreamingHttpResponse:
        question = request.data.get("question") if isinstance(request.data, dict) else None
        if not isinstance(question, str) or not question.strip():
            return Response(
                {"detail": "A non-empty 'question' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        context = retrieve_context(question)  # tenant-scoped, inside the request transaction
        response = StreamingHttpResponse(
            (_sse_frame(event) for event in stream_grounded_answer(context)),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # ask proxies (nginx) not to buffer the stream
        return response
