"""TDD for citation resolution: ``GET /api/chunks/<id>`` (#51).

A streamed answer closes with a citations frame carrying ``chunk_id`` and offsets but **no text**
(``app.generation.Citation``) — deliberately, since the answer stream should not re-send passages the
client may never open. This endpoint is how #19 turns a citation into visible evidence: the stored
chunk text, its character span, and the document it came from.

That makes it the endpoint where the project's central claim is checked. The text served here is what
the reader is told the answer was grounded in, so it must be the stored chunk verbatim — never
re-derived, never trimmed — and it must be reachable only by the tenant that owns it.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Chunk, Document, Tenant
from app.tenant_context import tenant_context
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

B_ISSUER = "https://keycloak.test/realms/globex"
B_CLIENT = "tenantiq-globex"

SOURCE = "Payment is due within 30 days of invoice. Late payment accrues 2% monthly interest."


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


def make_chunk(tenant: Tenant, *, title: str = "terms.txt", start: int = 0, end: int = 41):
    """A chunk whose ``text`` is exactly ``SOURCE[start:end]``, as #45 guarantees."""
    with tenant_context(tenant):
        document = Document.objects.create(title=title, status=Document.Status.READY)
        return Chunk.objects.create(
            document=document,
            index=0,
            text=SOURCE[start:end],
            char_count=end - start,
            start_offset=start,
            end_offset=end,
            embedding=[0.1] * 768,
            embedding_model="test-model",
        )


def test_resolves_a_citation_to_the_evidence(api, tenant_a, mint_token):
    chunk = make_chunk(tenant_a)

    resp = api.get(f"/api/chunks/{chunk.id}", **a_token(mint_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == chunk.id
    assert body["text"] == SOURCE[0:41]
    assert body["start_offset"] == 0
    assert body["end_offset"] == 41
    assert body["index"] == 0
    assert body["document"] == {"id": chunk.document_id, "title": "terms.txt"}


def test_the_served_text_is_the_stored_chunk_verbatim(api, tenant_a, mint_token):
    # The whole product claim in one assertion: what the reader is shown as the grounding is the
    # exact span at the offsets it is labelled with (#45), not a re-derived or tidied-up quote.
    chunk = make_chunk(tenant_a, start=42, end=len(SOURCE))

    body = api.get(f"/api/chunks/{chunk.id}", **a_token(mint_token)).json()

    assert body["text"] == SOURCE[body["start_offset"] : body["end_offset"]]


def test_never_serves_the_embedding(api, tenant_a, mint_token):
    # The vector is internal, it is large, and it is the tenant's content in another form.
    chunk = make_chunk(tenant_a)

    body = api.get(f"/api/chunks/{chunk.id}", **a_token(mint_token)).json()

    assert "embedding" not in body
    assert "embedding_model" not in body


def test_another_tenants_chunk_is_404(api, tenant_a, tenant_b, mint_token):
    chunk = make_chunk(tenant_a)

    resp = api.get(f"/api/chunks/{chunk.id}", **b_token(mint_token))

    assert resp.status_code == 404
    assert "Payment" not in resp.content.decode()


def test_an_unknown_chunk_is_404(api, tenant_a, mint_token):
    assert api.get("/api/chunks/999999", **a_token(mint_token)).status_code == 404


def test_a_foreign_chunk_and_a_missing_chunk_are_indistinguishable(
    api, tenant_a, tenant_b, mint_token
):
    # Same status and same body, or the endpoint becomes an oracle for "does this chunk id exist",
    # which leaks another tenant's corpus size and ingestion activity.
    chunk = make_chunk(tenant_a)

    foreign = api.get(f"/api/chunks/{chunk.id}", **b_token(mint_token))
    missing = api.get("/api/chunks/999999", **b_token(mint_token))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_requires_authentication(api, tenant_a):
    chunk = make_chunk(tenant_a)
    assert api.get(f"/api/chunks/{chunk.id}").status_code == 401
