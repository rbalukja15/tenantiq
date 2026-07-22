"""Extract plain text from an uploaded document so it can be chunked (#11).

A parser boundary: any failure to read a (possibly malformed, attacker-supplied) file is turned
into :class:`ParseError`, which the ingestion task maps to a ``FAILED`` document — a bad upload
never crashes the worker.
"""

from __future__ import annotations

from django.conf import settings
from pypdf import PdfReader
from pypdf.errors import PyPdfError

TEXT_TYPES = {"text/plain", "text/markdown"}
PDF_TYPE = "application/pdf"


class ParseError(Exception):
    """Raised when a file cannot be parsed into text. Its message is user-safe by construction — it
    is authored here, never a raw library/exception string — so ingestion can surface it directly.
    """


def extract_text(file, content_type: str) -> str:
    if content_type in TEXT_TYPES:
        return _read_text(file)
    if content_type == PDF_TYPE:
        return _read_pdf(file)
    raise ParseError(f"Unsupported content type: {content_type!r}")


def _read_text(file) -> str:
    data = file.read()
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    max_chars = settings.TENANTIQ_MAX_EXTRACTED_CHARS
    if len(text) > max_chars:
        raise ParseError("The document is too large to process.")
    return text


def _read_pdf(file) -> str:
    try:
        reader = PdfReader(file)
        page_count = len(reader.pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise ParseError("Could not read PDF.") from exc

    if page_count > settings.TENANTIQ_MAX_PDF_PAGES:
        # Bail on the page count *before* extracting — a many-thousand-page PDF must not run the
        # extractor at all. Bounds are permanent failures; a retry would hit the same wall.
        raise ParseError("The PDF has too many pages to process.")

    max_chars = settings.TENANTIQ_MAX_EXTRACTED_CHARS
    parts: list[str] = []
    total = 0
    try:
        for page in reader.pages:
            piece = page.extract_text() or ""
            total += len(piece) + 1  # +1 for the joining newline
            if total > max_chars:
                raise ParseError("The document is too large to process.")
            parts.append(piece)
    except (PyPdfError, OSError, ValueError) as exc:
        raise ParseError("Could not read PDF.") from exc
    return "\n".join(parts)
