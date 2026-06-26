"""Shared test fixtures.

A locally generated RSA key + token minter make auth tests hermetic: no live Keycloak.
The verifier injected in tests trusts this key, so we exercise the real verification logic
(issuer/audience/exp/alg checks) without any network.
"""

from __future__ import annotations

import time
from typing import Callable

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_ISSUER = "https://keycloak.test/realms/acme"
TEST_CLIENT_ID = "tenantiq-acme"


@pytest.fixture(scope="session")
def rsa_keys() -> tuple[bytes, bytes]:
    """(private_pem, public_pem) for RS256 signing/verification in tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def mint_token(rsa_keys) -> Callable[..., str]:
    """Mint a signed JWT. Override any claim; pass value None to omit it; pass a different
    signing_key or algorithm='none' to forge invalid tokens."""
    private_pem, _ = rsa_keys

    def _mint(
        *,
        issuer: str | None = TEST_ISSUER,
        audience: str | None = TEST_CLIENT_ID,
        sub: str | None = "user-123",
        expires_in: int = 3600,
        algorithm: str = "RS256",
        signing_key: bytes | None = None,
        **extra: object,
    ) -> str:
        now = int(time.time())
        raw = {
            "iss": issuer,
            "aud": audience,
            "sub": sub,
            "iat": now,
            "exp": now + expires_in,
            **extra,
        }
        payload = {k: v for k, v in raw.items() if v is not None}
        if algorithm == "none":
            return jwt.encode(payload, key=None, algorithm="none")
        return jwt.encode(payload, signing_key or private_pem, algorithm=algorithm)

    return _mint
