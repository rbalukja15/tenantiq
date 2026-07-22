"""TDD for text extraction from uploaded files (app.parsing)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.parsing import ParseError, extract_text

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_plain_text():
    assert extract_text(io.BytesIO(b"hello\nworld"), "text/plain") == "hello\nworld"


def test_extract_markdown():
    assert "Title" in extract_text(io.BytesIO(b"# Title\n\nBody"), "text/markdown")


def test_extract_pdf_text():
    with open(FIXTURES / "sample.pdf", "rb") as f:
        text = extract_text(f, "application/pdf")
    assert "TenantIQ" in text


def test_unsupported_type_raises():
    with pytest.raises(ParseError):
        extract_text(io.BytesIO(b"\x89PNG"), "image/png")


def test_corrupt_pdf_raises():
    with pytest.raises(ParseError):
        extract_text(io.BytesIO(b"not a real pdf at all"), "application/pdf")


def test_oversized_text_is_a_parse_error(settings):
    # A pathological upload must be rejected before it can be chunked/embedded (#47) — with a
    # user-safe message, not a leaked internal error.
    settings.TENANTIQ_MAX_EXTRACTED_CHARS = 100
    with pytest.raises(ParseError, match="too large"):
        extract_text(io.BytesIO(b"x" * 101), "text/plain")


def test_pdf_with_too_many_pages_is_a_parse_error(settings):
    # The single-page fixture exceeds a 0-page cap, so the extractor bails on the page count before
    # doing any extraction work.
    settings.TENANTIQ_MAX_PDF_PAGES = 0
    with open(FIXTURES / "sample.pdf", "rb") as f:
        with pytest.raises(ParseError, match="too many pages"):
            extract_text(f, "application/pdf")


def test_pdf_extracted_text_over_the_char_cap_is_a_parse_error(settings):
    # Even a low-page PDF must be bounded on *extracted* size, not just page count.
    settings.TENANTIQ_MAX_EXTRACTED_CHARS = 1
    with open(FIXTURES / "sample.pdf", "rb") as f:
        with pytest.raises(ParseError, match="too large"):
            extract_text(f, "application/pdf")
