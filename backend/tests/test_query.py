"""TDD for the streaming query path (#48).

Two layers, mirroring the design (ADR-0009):

- **The event stream** (`stream_grounded_answer`) — hermetic. Given an ``AssembledContext`` and an
  injected streaming LLM, it yields token deltas, then a terminal citations event that resolves the
  ``[n]`` markers the model wrote to real source chunks; a mid-stream LLM failure becomes an error
  event; a no-context question refuses without touching the model.
- **The HTTP surface** (`POST /api/query`) — Postgres-gated. Proves the acceptance criterion end to
  end: an authenticated client gets token frames closing with schema-valid citations that resolve to
  real chunk IDs; the DB transaction is not held open during generation (no query runs while the body
  streams); and a tenant can never surface or cite another tenant's chunks.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from app.generation import (
    CitationsEvent,
    ErrorEvent,
    LLMResult,
    TokenEvent,
    stream_grounded_answer,
)
from app.models import Chunk, Document, Tenant
from app.rag import AssembledContext, Source, build_grounded_prompt
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


# --- helpers --------------------------------------------------------------------------------------


def _source(number: int, *, chunk_id: int, text: str = "some source text") -> Source:
    return Source(
        number=number,
        chunk_id=chunk_id,
        document_id=chunk_id // 10,
        document_title="Doc",
        chunk_index=number - 1,
        start_offset=0,
        end_offset=len(text),
        similarity=0.9,
        text=text,
    )


def _context(
    sources: tuple[Source, ...], question: str = "what are the terms?"
) -> AssembledContext:
    system, user = build_grounded_prompt(question, sources)
    return AssembledContext(
        question=question, sources=sources, system_prompt=system, user_prompt=user
    )


class _StreamingLLM:
    """Streams a fixed sequence of text chunks; records that it was asked (and only once)."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls = 0

    def stream(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        self.calls += 1
        yield from self.chunks

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:  # noqa: ARG002
        raise AssertionError("the streaming path must use stream(), not generate()")


class _FailingStreamingLLM:
    """Streams a token, then raises mid-stream — the transient-LLM-failure path."""

    def stream(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        yield "Partial answer so f"
        raise RuntimeError("upstream model exploded")


class _ExplodingLLM:
    """Fails if touched at all — proves the no-context path never reaches the model."""

    def stream(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        raise AssertionError("the LLM must not be called when there is no context")


class _PromptEchoingLLM:
    """Streams the grounded prompt straight back, so any source text it was given surfaces in the
    answer. Used to give the isolation test teeth: if retrieval ever leaked another tenant's chunk,
    that chunk's text would appear in the stream."""

    def stream(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        yield user_prompt

    def generate(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        raise AssertionError("unused")


# --- event stream (hermetic) ----------------------------------------------------------------------


def test_stream_yields_tokens_then_a_terminal_citations_event():
    ctx = _context((_source(1, chunk_id=11), _source(2, chunk_id=22)))
    llm = _StreamingLLM(["Net thirty ", "days [1], ", "returns [2]."])

    events = list(stream_grounded_answer(ctx, llm=llm))

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    citations = [e for e in events if isinstance(e, CitationsEvent)]
    # Tokens stream first, exactly the model's deltas, in order.
    assert [t.text for t in tokens] == ["Net thirty ", "days [1], ", "returns [2]."]
    # Exactly one citations event, and it's last.
    assert len(citations) == 1 and isinstance(events[-1], CitationsEvent)
    # The [1] and [2] markers resolved to the real source chunks.
    assert [c.chunk_id for c in citations[0].citations] == [11, 22]


def test_stream_citations_resolve_from_the_prose_markers_and_drop_unknowns():
    ctx = _context((_source(1, chunk_id=11),))
    # The model writes [1] (real) and [9] (never retrieved) — only [1] must resolve.
    llm = _StreamingLLM(["See [1] and also [9]."])

    events = list(stream_grounded_answer(ctx, llm=llm))
    citations = next(e for e in events if isinstance(e, CitationsEvent)).citations

    assert [c.number for c in citations] == [1]
    assert [c.chunk_id for c in citations] == [11]


def test_no_context_refuses_without_calling_the_model():
    ctx = _context(sources=())  # nothing retrieved
    events = list(stream_grounded_answer(ctx, llm=_ExplodingLLM()))

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    citations = next(e for e in events if isinstance(e, CitationsEvent))
    assert "".join(t.text for t in tokens)  # a non-empty refusal was streamed
    assert citations.citations == ()  # ... with no citations
    assert not any(isinstance(e, ErrorEvent) for e in events)


def test_mid_stream_llm_failure_becomes_an_error_event_after_the_partial():
    ctx = _context((_source(1, chunk_id=11),))
    events = list(stream_grounded_answer(ctx, llm=_FailingStreamingLLM()))

    assert isinstance(events[0], TokenEvent) and events[0].text == "Partial answer so f"
    assert isinstance(events[-1], ErrorEvent)
    # A failed generation must not emit a (misleading) citations event.
    assert not any(isinstance(e, CitationsEvent) for e in events)


# --- HTTP surface (Postgres/pgvector) -------------------------------------------------------------


def _tenant(slug: str) -> Tenant:
    from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

    issuer = TEST_ISSUER if slug == "acme" else f"https://keycloak.test/realms/{slug}"
    client_id = TEST_CLIENT_ID if slug == "acme" else f"tenantiq-{slug}"
    return Tenant.objects.create(
        slug=slug, name=slug.title(), oidc_issuer=issuer, oidc_client_id=client_id
    )


def _seed(tenant: Tenant, texts: list[str]) -> None:
    from app.embeddings import get_embedder

    embedder = get_embedder()
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, text in enumerate(texts):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                start_offset=0,
                end_offset=len(text),
                embedding=embedder.embed_query(text),
                embedding_model=embedder.model,
            )


@pytest.fixture
def configured_auth(settings, rsa_keys):
    from app.auth.tenancy import tenant_for_issuer
    from app.auth.verifier import TenantTokenVerifier

    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )


def _parse_sse(body: bytes) -> list[tuple[str, dict]]:
    """Parse an SSE byte stream into a list of (event, data-dict) frames."""
    frames: list[tuple[str, dict]] = []
    for block in body.decode().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if event is not None:
            frames.append((event, data))
    return frames


def _consume(response) -> list[tuple[str, dict]]:
    return _parse_sse(b"".join(response.streaming_content))


@requires_postgres
def test_query_endpoint_streams_an_answer_that_closes_with_real_citations(
    configured_auth, mint_token
):
    acme = _tenant("acme")
    _seed(acme, ["Invoice payment terms are net thirty days after receipt."])

    client = APIClient()
    response = client.post(
        "/api/query",
        {"question": "when is payment due?"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {mint_token(sub='alice')}",
    )

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    frames = _consume(response)
    assert [e for e, _ in frames if e == "token"]  # at least one token frame
    citation_frames = [d for e, d in frames if e == "citations"]
    assert len(citation_frames) == 1  # exactly one, terminal
    with tenant_context(acme):
        real_ids = set(Chunk.objects.values_list("id", flat=True))
    cited_ids = {c["chunk_id"] for c in citation_frames[0]["citations"]}
    assert cited_ids and cited_ids <= real_ids  # every citation resolves to a real chunk


@requires_postgres
def test_query_endpoint_accepts_the_sse_accept_header(configured_auth, mint_token):
    # A real SSE client (fetch / EventSource) sends `Accept: text/event-stream`. The endpoint must
    # honour it — DRF's default JSON-only renderer set would otherwise 406 before the stream starts.
    _seed(_tenant("acme"), ["Invoice terms are net thirty days."])
    response = APIClient().post(
        "/api/query",
        {"question": "terms?"},
        format="json",
        HTTP_ACCEPT="text/event-stream",
        HTTP_AUTHORIZATION=f"Bearer {mint_token(sub='alice')}",
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")


@requires_postgres
def test_query_endpoint_requires_authentication():
    assert APIClient().post("/api/query", {"question": "hi"}, format="json").status_code == 401


@requires_postgres
def test_query_endpoint_holds_no_db_transaction_open_during_generation(configured_auth, mint_token):
    # Acceptance: retrieval happens in the request; generation (streaming the body) must issue no
    # further queries, so no DB transaction is held open while the model streams.
    _seed(_tenant("acme"), ["Refund policy: returns accepted within fourteen days."])
    client = APIClient()
    response = client.post(
        "/api/query",
        {"question": "refund window?"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {mint_token(sub='alice')}",
    )
    assert response.status_code == 200
    with CaptureQueriesContext(connection) as captured:
        _consume(response)  # drive the streaming body to completion
    assert len(captured) == 0  # generation touched the database zero times


@requires_postgres
def test_query_endpoint_cannot_surface_or_cite_another_tenants_chunks(
    configured_auth, mint_token, settings
):
    # Stream via an LLM that echoes the sources it was given, so a retrieval leak would surface the
    # other tenant's text in the answer — giving the surface-text assertion real teeth. `ACMESECRET`
    # is a sentinel present only in Acme's chunk, absent from Globex's data and from the question.
    settings.TENANTIQ_LLM_FACTORY = lambda: _PromptEchoingLLM()
    acme = _tenant("acme")
    globex = _tenant("globex")
    _seed(acme, ["ACMESECRET Acme confidential revenue figures and roadmap."])
    _seed(globex, ["Globex quarterly revenue summary for the team."])

    token = mint_token(sub="bob", issuer=globex.oidc_issuer, audience=globex.oidc_client_id)
    response = APIClient().post(
        "/api/query",
        {"question": "quarterly revenue figures roadmap"},  # lexical pull toward Acme, no sentinel
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert response.status_code == 200
    frames = _consume(response)
    with tenant_context(acme):
        acme_ids = set(Chunk.objects.values_list("id", flat=True))
    cited_ids = {c["chunk_id"] for e, d in frames if e == "citations" for c in d["citations"]}
    assert not (cited_ids & acme_ids)  # never cites Acme's chunks...
    body = "".join(json.dumps(d) for _, d in frames)
    assert "ACMESECRET" not in body  # ...and Acme's chunk text never reaches the stream
