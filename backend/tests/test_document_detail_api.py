"""TDD for document detail + delete: ``GET``/``DELETE /api/documents/<id>`` (#51).

#20's management UI needs to inspect one document and to delete it; deletion is the sharper of the
two, because a tenant asking for their document to be gone means *gone* — the row, the chunks it was
split into, and the raw file on storage. A delete that leaves any of the three behind is a broken
promise, and the vector left behind would still be retrievable.

Every test here is also an isolation test: the endpoints resolve through the tenant-scoped
``Document.objects`` manager (ADR-0002), so another tenant's id must be indistinguishable from one
that never existed — and, for ``DELETE``, must leave that tenant's data untouched.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.db import connection
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.embeddings import get_embedder
from app.models import Chunk, Document, Tenant
from app.retrieval import nearest_chunks
from app.tenant_context import tenant_context
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

B_ISSUER = "https://keycloak.test/realms/globex"
B_CLIENT = "tenantiq-globex"


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    """Keep uploaded files out of the repo — each test writes to its own tmp dir."""
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def configured_auth(settings, rsa_keys):
    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(
        slug="acme", name="Acme", oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID
    )


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=B_ISSUER, oidc_client_id=B_CLIENT
    )


@pytest.fixture
def api(configured_auth) -> APIClient:
    return APIClient()


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def a_token(mint_token) -> dict:
    return bearer(mint_token(sub="alice"))


def b_token(mint_token) -> dict:
    return bearer(mint_token(sub="bob", issuer=B_ISSUER, audience=B_CLIENT))


def make_document(tenant: Tenant, *, title: str, body: bytes = b"a contract", chunks: int = 2):
    """A document with a real file on storage and real chunks — the three things a delete must clear."""
    with tenant_context(tenant):
        document = Document.objects.create(
            title=title,
            content_type="text/plain",
            size_bytes=len(body),
            original_filename=f"{title}.txt",
            status=Document.Status.READY,
        )
        document.file.save(f"{title}.txt", ContentFile(body), save=True)
        for index in range(chunks):
            Chunk.objects.create(
                document=document,
                index=index,
                text=f"{title} chunk {index}",
                char_count=len(f"{title} chunk {index}"),
                start_offset=index * 10,
                end_offset=index * 10 + len(f"{title} chunk {index}"),
            )
    return document


def stored(document: Document) -> bool:
    """Is the raw file still on storage?"""
    return document.file.storage.exists(document.file.name)


# --- detail ---------------------------------------------------------------------------------


def test_detail_returns_the_callers_document(api, tenant_a, mint_token):
    document = make_document(tenant_a, title="acme-doc")

    resp = api.get(f"/api/documents/{document.id}", **a_token(mint_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == document.id
    assert body["title"] == "acme-doc"
    assert body["status"] == "ready"


def test_detail_serves_the_same_shape_as_the_list(api, tenant_a, mint_token):
    # #20 renders a row and a detail pane from one type. Divergence here is a frontend bug later.
    document = make_document(tenant_a, title="acme-doc")

    listed = api.get("/api/documents", **a_token(mint_token)).json()[0]
    detail = api.get(f"/api/documents/{document.id}", **a_token(mint_token)).json()

    assert listed.keys() == detail.keys()


def test_detail_of_another_tenants_document_is_404(api, tenant_a, tenant_b, mint_token):
    document = make_document(tenant_a, title="acme-doc")

    resp = api.get(f"/api/documents/{document.id}", **b_token(mint_token))

    assert resp.status_code == 404


def test_detail_of_an_unknown_document_is_404(api, tenant_a, mint_token):
    assert api.get("/api/documents/999999", **a_token(mint_token)).status_code == 404


def test_detail_requires_authentication(api, tenant_a):
    document = make_document(tenant_a, title="acme-doc")
    assert api.get(f"/api/documents/{document.id}").status_code == 401


# --- delete ---------------------------------------------------------------------------------


def test_delete_removes_the_row(api, tenant_a, mint_token):
    document = make_document(tenant_a, title="acme-doc")

    resp = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert resp.status_code == 204
    assert not Document.all_objects.filter(id=document.id).exists()


def test_delete_removes_the_chunks(api, tenant_a, mint_token):
    # The vectors are the point: a chunk left behind stays retrievable and can still be cited, so a
    # "deleted" document would go on answering questions.
    document = make_document(tenant_a, title="acme-doc", chunks=3)
    assert Chunk.all_objects.filter(document_id=document.id).count() == 3

    api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert not Chunk.all_objects.filter(document_id=document.id).exists()


def test_delete_removes_the_stored_file(
    api, tenant_a, mint_token, django_capture_on_commit_callbacks
):
    document = make_document(tenant_a, title="acme-doc")
    assert stored(document)

    with django_capture_on_commit_callbacks(execute=True):
        api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert not stored(document)


def test_the_file_is_destroyed_only_once_the_row_delete_commits(
    api, tenant_a, mint_token, django_capture_on_commit_callbacks
):
    # The ordering decision, asserted rather than described: the bytes are not touched inline, so a
    # request that rolls back after this point leaves the document whole instead of listing a row
    # whose file has already been destroyed.
    document = make_document(tenant_a, title="acme-doc")

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        resp = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert resp.status_code == 204
    assert stored(document)  # still there while the transaction is open...
    assert len(callbacks) == 1  # ...and queued to go the moment it commits


def test_delete_of_another_tenants_document_is_404_and_changes_nothing(
    api, tenant_a, tenant_b, mint_token
):
    # The isolation proof for the one destructive endpoint in the API: not merely "B is refused",
    # but "A's row, A's chunks and A's file are all still there afterwards".
    document = make_document(tenant_a, title="acme-doc", chunks=2)

    resp = api.delete(f"/api/documents/{document.id}", **b_token(mint_token))

    assert resp.status_code == 404
    assert Document.all_objects.filter(id=document.id).exists()
    assert Chunk.all_objects.filter(document_id=document.id).count() == 2
    assert stored(document)


def test_deleting_is_idempotent(api, tenant_a, mint_token):
    # The row is gone either way, and the second call is a clean 404 rather than a 500 from code
    # that assumed the file or the row was still there.
    document = make_document(tenant_a, title="acme-doc")

    first = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))
    second = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert first.status_code == 204
    assert second.status_code == 404


def test_delete_leaves_other_documents_alone(api, tenant_a, mint_token):
    # Files live under a per-document uuid path; this catches a delete that walks a directory.
    doomed = make_document(tenant_a, title="doomed")
    kept = make_document(tenant_a, title="kept", chunks=2)

    api.delete(f"/api/documents/{doomed.id}", **a_token(mint_token))

    assert Document.all_objects.filter(id=kept.id).exists()
    assert Chunk.all_objects.filter(document_id=kept.id).count() == 2
    assert stored(kept)


def test_delete_of_a_document_with_no_stored_file_still_succeeds(api, tenant_a, mint_token):
    # A document can exist without a file: the row is created before the upload is saved, and a
    # failed upload can leave the field empty. Deleting it must not 500 on the missing file.
    with tenant_context(tenant_a):
        document = Document.objects.create(title="no-file")

    resp = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert resp.status_code == 204
    assert not Document.all_objects.filter(id=document.id).exists()


def test_delete_while_ingestion_is_in_flight_is_allowed(api, tenant_a, mint_token):
    # Deliberate: PROCESSING is not a lock. Ingestion can wedge (that is what #55's sweeper is for),
    # and a document that cannot be deleted while wedged is a document a tenant cannot delete at all.
    # The worker is made to tolerate the vanishing row instead — see test_ingestion_of_a_deleted_*.
    document = make_document(tenant_a, title="in-flight")
    Document.all_objects.filter(id=document.id).update(status=Document.Status.PROCESSING)

    resp = api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    assert resp.status_code == 204
    assert not Document.all_objects.filter(id=document.id).exists()


def test_delete_requires_authentication(api, tenant_a):
    document = make_document(tenant_a, title="acme-doc")

    assert api.delete(f"/api/documents/{document.id}").status_code == 401
    assert Document.all_objects.filter(id=document.id).exists()


# --- what deletion actually has to mean -------------------------------------------------------

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


@requires_postgres
def test_a_deleted_document_can_no_longer_be_retrieved_or_cited(api, tenant_a, mint_token):
    """The product-level assertion the row counts only imply (#51).

    Every other delete test checks bookkeeping — rows gone, file gone. This one checks the thing a
    tenant actually means by "delete it": that the document stops being able to answer questions.
    A chunk left behind keeps its embedding in the shared index, so retrieval would go on surfacing
    it and an answer would go on citing a document the tenant believes they removed.
    """
    embedder = get_embedder()
    with tenant_context(tenant_a):
        document = Document.objects.create(title="terms", status=Document.Status.READY)
        text = "Payment is due within 30 days of invoice."
        Chunk.objects.create(
            document=document,
            index=0,
            text=text,
            char_count=len(text),
            embedding=embedder.embed_query(text),
            embedding_model=embedder.model,
        )
        assert nearest_chunks("when is payment due", k=5)  # retrievable before

    api.delete(f"/api/documents/{document.id}", **a_token(mint_token))

    with tenant_context(tenant_a):
        assert nearest_chunks("when is payment due", k=5) == []  # and gone after
