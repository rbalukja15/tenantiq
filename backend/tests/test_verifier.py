"""TDD for the injectable token verifier (app.auth.verifier).

The verifier is constructed with two injected seams so these tests need no DB and no network:
- key_resolver(token, tenant) -> verification key (here: the local test public key)
- tenant_lookup(iss) -> tenant (here: a fake with the expected issuer/client_id)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.verifier import TenantTokenVerifier, TokenError
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER


def make_verifier(public_pem: bytes, *, tenant: object | None = None) -> TenantTokenVerifier:
    tenant = tenant or SimpleNamespace(oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID)
    return TenantTokenVerifier(
        key_resolver=lambda token, t: public_pem,
        tenant_lookup=lambda iss: tenant if iss == tenant.oidc_issuer else None,
    )


def test_valid_token_returns_claims_and_tenant(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    claims, tenant = make_verifier(public_pem).verify(mint_token(sub="alice"))
    assert claims["sub"] == "alice"
    assert tenant.oidc_issuer == TEST_ISSUER


def test_expired_beyond_leeway_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(expires_in=-120))


def test_expired_within_leeway_accepted(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    claims, _ = make_verifier(public_pem).verify(mint_token(expires_in=-30))
    assert claims["sub"]


def test_unknown_issuer_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(issuer="https://evil.test/realms/x"))


def test_issuer_mismatch_after_routing_rejected(rsa_keys, mint_token):
    """Token signed correctly but its iss differs from the routed tenant's issuer -> reject."""
    _, public_pem = rsa_keys
    tenant = SimpleNamespace(
        oidc_issuer="https://other.test/realms/z", oidc_client_id=TEST_CLIENT_ID
    )
    verifier = TenantTokenVerifier(
        key_resolver=lambda token, t: public_pem,
        tenant_lookup=lambda iss: tenant,  # routes any iss to a tenant with a DIFFERENT issuer
    )
    with pytest.raises(TokenError):
        verifier.verify(mint_token(issuer=TEST_ISSUER))


def test_bad_signature_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(signing_key=other_pem))


def test_alg_none_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(algorithm="none"))


def test_wrong_audience_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(audience="some-other-client"))


def test_missing_required_claim_rejected(rsa_keys, mint_token):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify(mint_token(sub=None))


def test_malformed_token_rejected(rsa_keys):
    _, public_pem = rsa_keys
    with pytest.raises(TokenError):
        make_verifier(public_pem).verify("not.a.jwt")
