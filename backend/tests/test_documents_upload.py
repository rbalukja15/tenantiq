"""TDD for the document upload endpoint (issue #10).

A tenant uploads a PDF/text file; the API validates type + size, stores the raw bytes under a
tenant-scoped path, and persists a PENDING Document visible only to that tenant.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Document, Tenant
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


def a_token(mint_token) -> str:
    return mint_token(sub="alice")


def b_token(mint_token) -> str:
    return mint_token(sub="bob", issuer=B_ISSUER, audience=B_CLIENT)


def txt(name="notes.txt", body=b"hello world", content_type="text/plain") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type=content_type)


def test_upload_text_file_creates_pending_document(api, tenant_a, mint_token):
    resp = api.post(
        "/api/documents", {"file": txt()}, format="multipart", **bearer(a_token(mint_token))
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["status"] == "pending"
    assert body["title"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == len(b"hello world")
    assert body["original_filename"] == "notes.txt"
    with tenant_context(tenant_a):
        doc = Document.objects.get(id=body["id"])
        assert doc.tenant_id == tenant_a.id
        assert doc.file.read() == b"hello world"
        assert doc.file.name.startswith(f"tenants/{tenant_a.id}/")


def test_upload_defaults_title_to_filename(api, tenant_a, mint_token):
    f = txt(name="report.pdf", body=b"%PDF-1.4 fake", content_type="application/pdf")
    resp = api.post(
        "/api/documents", {"file": f}, format="multipart", **bearer(a_token(mint_token))
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["title"] == "report.pdf"


def test_upload_honors_explicit_title(api, tenant_a, mint_token):
    resp = api.post(
        "/api/documents",
        {"file": txt(), "title": "Q3 Notes"},
        format="multipart",
        **bearer(a_token(mint_token)),
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["title"] == "Q3 Notes"


def test_upload_without_file_is_rejected(api, tenant_a, mint_token):
    resp = api.post(
        "/api/documents", {"title": "no file"}, format="multipart", **bearer(a_token(mint_token))
    )
    assert resp.status_code == 400


def test_upload_disallowed_type_is_rejected(api, tenant_a, mint_token):
    png = SimpleUploadedFile("evil.png", b"\x89PNG\r\n", content_type="image/png")
    resp = api.post(
        "/api/documents", {"file": png}, format="multipart", **bearer(a_token(mint_token))
    )
    assert resp.status_code == 400


def test_upload_oversized_is_rejected(api, tenant_a, mint_token, settings):
    settings.TENANTIQ_MAX_UPLOAD_BYTES = 10
    resp = api.post(
        "/api/documents",
        {"file": txt(body=b"x" * 11)},
        format="multipart",
        **bearer(a_token(mint_token)),
    )
    assert resp.status_code == 400


def test_upload_requires_authentication(api, tenant_a):
    assert api.post("/api/documents", {"file": txt()}, format="multipart").status_code == 401


def test_uploaded_document_is_tenant_scoped(api, tenant_a, tenant_b, mint_token):
    up = api.post(
        "/api/documents",
        {"file": txt(name="acme.txt")},
        format="multipart",
        **bearer(a_token(mint_token)),
    )
    assert up.status_code == 201, up.content
    a_list = api.get("/api/documents", **bearer(a_token(mint_token)))
    assert [d["title"] for d in a_list.json()] == ["acme.txt"]
    b_list = api.get("/api/documents", **bearer(b_token(mint_token)))
    assert b_list.json() == []
