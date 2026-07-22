"""Shared test fixtures.

A locally generated RSA key + token minter make auth tests hermetic: no live Keycloak.
The verifier injected in tests trusts this key, so we exercise the real verification logic
(issuer/audience/exp/alg checks) without any network.
"""

from __future__ import annotations

import os
import time
from typing import Callable

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_ISSUER = "https://keycloak.test/realms/acme"
TEST_CLIENT_ID = "tenantiq-acme"


# --- CI guard: the Postgres-only isolation proofs must never silently skip (#50) ------------------
#
# The flagship RLS proofs are marked `requires_postgres` and skip off Postgres. That is right locally
# (SQLite has no RLS) but dangerous in CI: one env regression and "isolation is sacred" is unproven
# while CI stays green. When TENANTIQ_REQUIRE_POSTGRES is set (the CI Postgres job sets it), fail the
# run if the suite isn't on Postgres or any Postgres-only test was skipped.

_skipped_postgres_tests: list[str] = []


def _postgres_guard_enabled() -> bool:
    return os.environ.get("TENANTIQ_REQUIRE_POSTGRES", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def pytest_runtest_logreport(report) -> None:
    if not (report.skipped and report.when == "setup"):
        return
    longrepr = getattr(report, "longrepr", None)
    reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else ""
    if "postgres" in reason.lower():
        _skipped_postgres_tests.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    if not _postgres_guard_enabled():
        return
    from django.db import connection

    problems = []
    if connection.vendor != "postgresql":
        problems.append(f"the suite ran against '{connection.vendor}', not Postgres")
    if _skipped_postgres_tests:
        problems.append(
            f"{len(_skipped_postgres_tests)} Postgres-only test(s) skipped "
            f"({', '.join(_skipped_postgres_tests)})"
        )
    if problems:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "TENANTIQ_REQUIRE_POSTGRES: isolation proofs must run — " + "; ".join(problems),
                red=True,
            )


@pytest.fixture(autouse=True)
def _isolate_tenant_context():
    """Keep the current-tenant contextvar from leaking across tests (it is process-global)."""
    from app.tenant_context import clear_current_tenant

    clear_current_tenant()
    yield
    clear_current_tenant()


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
