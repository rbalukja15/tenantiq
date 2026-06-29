"""Extract plain text from an uploaded document so it can be chunked (#11).

A parser boundary: any failure to read a (possibly malformed, attacker-supplied) file is turned
into :class:`ParseError`, which the ingestion task maps to a ``FAILED`` document — a bad upload
never crashes the worker.
"""

from __future__ import annotations

from pypdf import PdfReader
from pypdf.errors import PyPdfError

TEXT_TYPES = {"text/plain", "text/markdown"}
PDF_TYPE = "application/pdf"


class ParseError(Exception):
    """Raised when a file cannot be parsed into text."""


def extract_text(file, content_type: str) -> str:
    if content_type in TEXT_TYPES:
        return _read_text(file)
    if content_type == PDF_TYPE:
        return _read_pdf(file)
    raise ParseError(f"Unsupported content type: {content_type!r}")


def _read_text(file) -> str:
    data = file.read()
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def _read_pdf(file) -> str:
    try:
        reader = PdfReader(file)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise ParseError("Could not read PDF.") from exc
