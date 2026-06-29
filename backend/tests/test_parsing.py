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
