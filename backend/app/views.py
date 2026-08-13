"""API views."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from app.generation import (
    CitationsEvent,
    ErrorEvent,
    TokenEvent,
    get_llm,
    stream_grounded_answer,
)
from app.models import Chunk, Document, Tenant
from app.rag import retrieve_context
from app.serializers import ChunkSerializer, DocumentSerializer
from app.tasks import ingest_document
from app.throttling import (
    QUERY_QUOTA_THROTTLES,
    PublicDiscoveryRateThrottle,
    TenantQueryRateThrottle,
    TenantReadRateThrottle,
    TenantUploadRateThrottle,
)
from app.usage import estimate_tokens, record_query_usage, usage_summary

logger = logging.getLogger(__name__)


class MeView(APIView):
    """Who am I + which tenant. The frontend's session probe and the auth test surface.

    Deliberately does not expose tenant OIDC config (e.g. client id).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [TenantReadRateThrottle]

    def get(self, request: Request) -> Response:
        tenant = request.tenant
        return Response(
            {
                "username": request.user.username,
                "email": request.user.email,
                "tenant": {"id": str(tenant.id), "slug": tenant.slug, "name": tenant.name},
            }
        )


class TenantDiscoveryView(APIView):
    """Public per-tenant OIDC discovery: ``GET /api/tenants/discovery?slug=`` (#18, ADR-0013 §2).

    Resolves the login flow's chicken-and-egg. Per-tenant OIDC configuration lives only in the
    database (ADR-0002) and every other endpoint needs a token, so a browser on the login page has no
    way to learn *which* realm and client to authenticate against — it needs that configuration
    *before* it can obtain a token. This hands it exactly two values and nothing else.

    **This is the project's only unauthenticated endpoint**, hence the deliberate shape:

    - ``authentication_classes = []``. Not merely "authentication optional": running the tenant
      authenticator here would make a stale or expired token in the browser turn discovery into a
      401, stranding the very user who is trying to log in again.
    - Both returned values are **public by definition** in OIDC — the issuer is fetched
      unauthenticated at ``/.well-known/openid-configuration``, and a public client id is not a
      secret. Nothing else about the tenant (id, name, document counts, membership) is exposed.
    - Unknown, inactive, and missing-slug all return the **same** 404 body. A distinct status or
      message for any of them would report which slugs exist as customers and which have churned.

    The accepted cost, recorded in ADR-0013: this is a tenant-slug *enumeration* oracle. Enumerating
    names is judged acceptable; enumerating data stays impossible, because no tenant-owned row is
    reachable from here.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicDiscoveryRateThrottle]

    def get(self, request: Request) -> Response:
        slug = (request.query_params.get("slug") or "").strip()
        # Exact match on the trimmed slug: SlugField is unique, so this is unambiguous. A
        # case-insensitive lookup would be friendlier but could match two distinct rows.
        tenant = Tenant.objects.filter(slug=slug, is_active=True).first() if slug else None
        if tenant is None:
            raise NotFound()
        return Response({"issuer": tenant.oidc_issuer, "client_id": tenant.oidc_client_id})


class DocumentListCreateView(generics.ListCreateAPIView):
    """List the caller's documents, or upload a new one. ``Document.objects`` is tenant-scoped, so
    the list can only ever return the caller's rows, and an upload is bound to the caller's tenant
    (ADR-0002, #8). The raw file is validated and stored; the row starts at PENDING (#10)."""

    permission_classes = [IsAuthenticated]
    serializer_class = DocumentSerializer

    def get_throttles(self):
        # An upload (POST) enqueues worker load, so it is bounded by the tighter 'upload' budget; a
        # listing (GET) is cheap and uses the 'read' budget. Same tenant, two separate scopes.
        throttle = (
            TenantUploadRateThrottle if self.request.method == "POST" else TenantReadRateThrottle
        )
        return [throttle()]

    def get_queryset(self):
        return Document.objects.order_by("created_at")

    def perform_create(self, serializer: DocumentSerializer) -> None:
        document = serializer.save(tenant=self.request.tenant)
        tenant_id = self.request.tenant.id
        # Enqueue only after the row is committed, so the worker can't race the request's
        # transaction (ATOMIC_REQUESTS). The worker has no request, so we pass the tenant id.
        transaction.on_commit(lambda: ingest_document.delay(document.id, tenant_id))


class DocumentDetailView(generics.RetrieveDestroyAPIView):
    """Inspect or delete one document: ``GET``/``DELETE /api/documents/<id>`` (#51).

    Both resolve through the tenant-scoped ``Document.objects`` manager (ADR-0002), so another
    tenant's id is a 404 — the same response as an id that never existed, so neither verb can be used
    to probe for the existence of someone else's document.

    **Delete means gone**: the row, the chunks it was split into (FK cascade), and the raw file on
    storage. Leaving chunks behind would be the worst of the three — their vectors stay in the index,
    so a "deleted" document would go on being retrieved and cited.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = DocumentSerializer

    def get_throttles(self):
        # A DELETE is charged against the tighter *upload* budget rather than the read budget: it is
        # the other endpoint that writes to storage, and a destructive path should not be as cheap to
        # hammer as a listing.
        throttle = (
            TenantUploadRateThrottle if self.request.method == "DELETE" else TenantReadRateThrottle
        )
        return [throttle()]

    def get_queryset(self):
        return Document.objects.all()

    def perform_destroy(self, document: Document) -> None:
        """Delete the row first, then the stored file once that delete has actually committed.

        Ordering matters, and both alternatives are worse:

        - *File first, then the row.* If the row delete then fails, the file is gone but the document
          still lists — pointing at bytes that no longer exist.
        - *Row first, then the file, inline.* ``ATOMIC_REQUESTS`` means the row delete is not durable
          until the response commits, so an error after this point rolls the row back and leaves the
          document listing with its file already destroyed.

        Deleting on commit makes a rollback a complete no-op — nothing is destroyed unless the row is
        genuinely gone. The residual risk is a crash in the moment between commit and the callback,
        which orphans the file but never loses a referenced one.
        """
        storage, name = document.file.storage, document.file.name
        document.delete()
        if name:
            transaction.on_commit(lambda: _delete_stored_file(storage, name))


def _delete_stored_file(storage, name: str) -> None:
    """Remove a deleted document's bytes, logging rather than raising if storage refuses.

    This runs after the response has been committed, so raising could not tell the caller anything —
    the document is already deleted and the client has its 204. An orphaned file is a cleanup problem
    (hence the exception-level log); turning it into an unhandled error in the commit hook would not
    delete it either.
    """
    try:
        storage.delete(name)
    except Exception:  # noqa: BLE001 — a storage failure must not surface after a committed delete
        logger.exception("failed to delete stored file %s for a deleted document", name)


class ChunkDetailView(generics.RetrieveAPIView):
    """Resolve a citation to its evidence: ``GET /api/chunks/<id>`` (#51, consumed by #19).

    A streamed answer's citations frame carries ``chunk_id`` and offsets but no text, so this is what
    turns a ``[1]`` in the prose into a passage the reader can check. ``Chunk.objects`` is
    tenant-scoped, so a citation can only ever be resolved by the tenant whose corpus produced it —
    and since the chunk is scoped, the document reached through it is that tenant's by construction.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [TenantReadRateThrottle]
    serializer_class = ChunkSerializer

    def get_queryset(self):
        return Chunk.objects.select_related("document")


class DocumentRetryView(APIView):
    """Re-run ingestion for a FAILED document (issue #13).

    The document is fetched through the tenant-scoped ``Document.objects`` manager, so a caller can
    only ever retry their own tenant's document — another tenant's id resolves to 404, never a
    cross-tenant action. Only a FAILED document is retryable; anything else is a 409.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [TenantUploadRateThrottle]  # re-enqueues ingestion -> upload budget

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


def _try_parse(raw: str, *, end_of_day: bool) -> "tuple[datetime, bool] | None":
    """Parse ``raw`` as an ISO timestamp, or as a bare calendar date. ``None`` if neither.

    Returns ``(instant, was_date_only)``. A bare date used as an **end** bound resolves to the *last*
    instant of that day, not midnight: ``?end=2026-07-31`` means "through the 31st", and resolving it
    to 00:00 would silently drop that whole day's spend from a month report.

    Date-only input is detected by the absence of a time separator, *not* by letting
    ``parse_datetime`` fail: since Django 4.1 it delegates to ``datetime.fromisoformat``, which happily
    parses a bare date as midnight — which would silently defeat the ``end_of_day`` handling above.
    """
    if ":" not in raw:  # no time component -> a bare calendar date
        day = parse_date(raw)
        if day is None:
            return None
        time = datetime.max.time() if end_of_day else datetime.min.time()
        return datetime.combine(day, time), True
    parsed = parse_datetime(raw)
    return (parsed, False) if parsed is not None else None


def _parse_instant(raw: str | None, *, end_of_day: bool = False) -> "datetime | None":
    """Parse an optional ISO-8601 query parameter into an aware datetime.

    A naive value is assumed UTC (the project runs ``USE_TZ`` with ``TIME_ZONE='UTC'``), so a caller
    can pass a plain date. A malformed value raises ``ValueError``, which the view turns into a 400 —
    a bad query string must never surface as a 500. ``end_of_day`` makes a bare date inclusive of the
    whole day (see :func:`_try_parse`).

    One tolerance: in a query string ``+`` decodes to a space, so an unencoded ISO offset arrives as
    ``2026-07-26T12:00:00 00:00``. That is what a client sending a plain ``datetime.isoformat()``
    produces, so we retry with the offset's ``+`` restored rather than rejecting valid-looking input.
    """
    if not raw:
        return None
    result = _try_parse(raw, end_of_day=end_of_day)
    if result is None and " " in raw:
        head, _, tail = raw.rpartition(" ")
        result = _try_parse(f"{head}+{tail}", end_of_day=end_of_day)
    if result is None:
        raise ValueError(f"Could not parse '{raw}' as an ISO-8601 date or timestamp.")
    parsed, _was_date_only = result
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


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
    # The expensive path: a per-tenant burst rate plus daily/monthly volume quotas (#49). All are
    # keyed on the tenant, so one tenant's spend can never eat into another's budget. The quota
    # throttles only *gate* here; the request is *charged* against the quota below, once we know it
    # is actually being served (a request 429'd by the rate throttle must not burn quota).
    throttle_classes = [TenantQueryRateThrottle, *QUERY_QUOTA_THROTTLES]
    content_negotiation_class = _IgnoreClientContentNegotiation

    def post(self, request: Request) -> Response | StreamingHttpResponse:
        question = request.data.get("question") if isinstance(request.data, dict) else None
        if not isinstance(question, str) or not question.strip():
            return Response(
                {"detail": "A non-empty 'question' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # We have cleared every throttle gate and are about to serve: charge the query quota now, so
        # only served requests draw it down (never a bad-request 400 or a rate-limited 429).
        for quota in QUERY_QUOTA_THROTTLES:
            quota().record(request)
        context = retrieve_context(question)  # tenant-scoped, inside the request transaction
        # Resolve the LLM here so accounting can record the model that *actually* serves the answer:
        # with no Anthropic key configured the factory returns the local Ollama fallback, and billing
        # that at Anthropic prices would invent spend (#17).
        llm = get_llm()
        response = StreamingHttpResponse(
            self._stream_and_account(context, request.tenant, llm),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # ask proxies (nginx) not to buffer the stream
        return response

    @staticmethod
    def _stream_and_account(context, tenant, llm) -> Iterator[str]:
        """Stream the SSE frames, then record what the request cost (#17).

        The ``tenant`` is captured from the request because this generator runs *after* the view
        returns: ``ATOMIC_REQUESTS`` has committed and the middleware has cleared the tenant
        contextvar, so there is no ambient tenant left to infer (``record_query_usage`` re-establishes
        it). Accounting happens in a ``finally`` so a client that disconnects mid-stream — or a model
        that fails after emitting tokens — is still charged for the tokens it did produce.

        Two cases are deliberately **not** charged, because no tokens were spent:

        - a refusal (no retrieved context) never calls the model at all;
        - a generation that failed before emitting anything (bad key, provider down) — charging the
          prompt there would let a client's retry loop manufacture spend during an outage.

        ``model`` is the model the resolved client actually uses, not the configured Anthropic name,
        so an answer served by the local Ollama fallback is priced as that model.
        """
        produced: list[str] = []
        try:
            for event in stream_grounded_answer(context, llm=llm):
                if isinstance(event, TokenEvent):
                    produced.append(event.text)
                yield _sse_frame(event)
        finally:
            if context.has_context and produced:
                try:
                    record_query_usage(
                        tenant,
                        model=getattr(llm, "model", "") or settings.TENANTIQ_LLM_MODEL,
                        input_tokens=estimate_tokens(context.system_prompt)
                        + estimate_tokens(context.user_prompt),
                        output_tokens=estimate_tokens("".join(produced)),
                    )
                except Exception:  # noqa: BLE001 — accounting must never corrupt a served answer
                    # Every token has already been sent to the client. Losing a usage row is bad (it
                    # is logged at exception level for follow-up); truncating or erroring a response
                    # the client already received in full would be worse.
                    logger.exception("failed to record query usage for tenant %s", tenant.id)


class UsageView(APIView):
    """Report the caller's token usage and estimated cost over a time range (#17, ADR-0012).

    Reads through the tenant-scoped ``UsageRecord`` manager (``app.usage.usage_summary``), so a tenant
    can only ever aggregate **its own** spend — cost data leaks usage volume and behaviour, so it gets
    the same isolation as document content. ``start``/``end`` are optional ISO-8601 timestamps; the
    default window is the last 30 days. A malformed or inverted range is a 400, never a 500.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [TenantReadRateThrottle]  # a cheap read

    def get(self, request: Request) -> Response:
        try:
            start = _parse_instant(request.query_params.get("start"))
            # A bare end date means "through that whole day", not "up to its midnight".
            end = _parse_instant(request.query_params.get("end"), end_of_day=True)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        # Validate against the *resolved* window, so a start after the defaulted end (e.g. a start in
        # the future with no end) is rejected rather than silently reporting "no spend".
        if start is not None and start > (end or timezone.now()):
            return Response(
                {"detail": "'start' must not be after 'end'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        summary = usage_summary(start=start, end=end)
        return Response(
            {
                "start": summary["start"].isoformat(),
                "end": summary["end"].isoformat(),
                "requests": summary["requests"],
                "input_tokens": summary["input_tokens"],
                "output_tokens": summary["output_tokens"],
                # Serialized as a string so the Decimal keeps its exact value — a JSON float would
                # reintroduce the representation error the Decimal column exists to avoid.
                "estimated_cost_usd": str(summary["estimated_cost_usd"]),
                "currency": "USD",
            }
        )
