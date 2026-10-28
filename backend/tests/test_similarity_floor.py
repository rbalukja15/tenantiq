"""The shipped similarity floor: measured, and reachable through configuration (#88, ADR-0021).

Every refusal test before these built its empty context by hand, and no test ran retrieval at the
shipped ``TENANTIQ_RETRIEVAL_MIN_SIMILARITY``. So a default of 0.0, which drops nothing, passed a
green suite while the refusal path (and the UI state built for it) was unreachable in a default
deployment. Nothing here overrides the floor except to show what the shipped value changed.

Two halves, because the suite cannot run the embedder the value was measured on. The first pins the
value inside the window the evaluation measured on mxbai-embed-large: below it the product answers
questions it has no evidence for, above it the product refuses questions it can answer. The second
ingests a real corpus and retrieves at the shipped value with the hashing embedder, to prove the
floor is wired through configuration to the refusal. Hashing similarities count shared words, so
each question shares either no word with the corpus or nearly all of them, never something between.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.db import connection

from app.ingestion import run_ingestion
from app.models import Document, Tenant
from app.rag import retrieve_context
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)

# What `make eval` measured on mxbai-embed-large, the default local embedder, on 24 August 2026
# (docs/evaluation.md, "Choosing the similarity floor"). A new measurement updates both places.
BEST_UNANSWERABLE_SIMILARITY = 0.441
WORST_ANSWERABLE_SIMILARITY = 0.567

CORPUS = {
    "terms.txt": "Invoices are payable within forty-five days of the invoice date.",
    "travel.txt": "Hotel nights in London are capped at two hundred pounds.",
}
UNANSWERABLE = "Who painted Mona Lisa?"  # no word in common with the corpus
ANSWERABLE = "Within how many days of the invoice date are invoices payable?"


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def _ingest(tenant: Tenant, documents: dict[str, str]) -> None:
    """Push each document through the real ingestion pipeline, as an upload would."""
    for name, text in documents.items():
        body = text.encode("utf-8")
        with tenant_context(tenant):
            document = Document.objects.create(
                title=name,
                content_type="text/plain",
                original_filename=name,
                size_bytes=len(body),
                file=ContentFile(body, name=name),
            )
        run_ingestion(document.id, tenant.id)
        with tenant_context(tenant):
            document.refresh_from_db()
        assert document.status == Document.Status.READY, document.error


def test_the_shipped_floor_sits_inside_the_window_the_evaluation_measured(settings):
    floor = settings.TENANTIQ_RETRIEVAL_MIN_SIMILARITY

    assert floor > BEST_UNANSWERABLE_SIMILARITY, (
        f"a floor of {floor} admits the best-scoring question the corpus cannot answer "
        f"({BEST_UNANSWERABLE_SIMILARITY}), so the product answers it from unrelated passages"
    )
    assert floor < WORST_ANSWERABLE_SIMILARITY, (
        f"a floor of {floor} drops the worst-scoring question the corpus can answer "
        f"({WORST_ANSWERABLE_SIMILARITY}), so the product refuses a question it can answer"
    )


@requires_postgres
def test_a_question_the_corpus_cannot_answer_is_refused_at_the_shipped_floor():
    acme = _tenant("acme")
    _ingest(acme, CORPUS)

    with tenant_context(acme):
        shipped = retrieve_context(UNANSWERABLE)
        unfloored = retrieve_context(UNANSWERABLE, min_similarity=0.0)

    assert not shipped.has_context
    assert shipped.sources == ()
    # Not an empty search: at 0.0 the same question gets the corpus back as its "evidence", which
    # is what every default deployment did until #88. It is the floor that refused.
    assert unfloored.has_context


@requires_postgres
def test_a_question_the_corpus_answers_keeps_its_sources_at_the_shipped_floor():
    acme = _tenant("acme")
    _ingest(acme, CORPUS)

    with tenant_context(acme):
        context = retrieve_context(ANSWERABLE)

    assert context.has_context
    assert "forty-five days" in context.sources[0].text
    # Only the passage that answers it clears the floor: the unrelated one is dropped, not padded.
    assert [s.document_title for s in context.sources] == ["terms.txt"]
