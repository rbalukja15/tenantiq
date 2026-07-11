"""Recursive, token-aware text splitter (ADR-0003, fidelity #45).

Splits on the largest natural boundary that fits — paragraphs, then lines, then sentences, then
words, then a hard character cut — and packs the pieces into ~target-token chunks with a small
overlap so context isn't lost across boundaries. Sizing uses a chars-per-token estimate to stay
dependency-free; #12 can swap in the embedding model's real tokenizer.

**Fidelity (#45):** the splitter works entirely in *offsets* into the source and never mutates the
text — every chunk is an exact ``source[start_offset:end_offset]`` slice, with separators left
attached. This is what lets a citation quote a chunk verbatim and address it by offset; a
space-rejoined approximation (the previous implementation) could not. Overlap is expressed as
consecutive slices sharing a range, so each slice stays individually verbatim.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4
# Boundary preference, largest natural break first (ADR-0003): paragraph, line, sentence, word.
# A hard character cut is the implicit last resort when none of these appear in the window.
_BOUNDARIES = ["\n\n", "\n", ". ", " "]


def estimate_tokens(text: str) -> int:
    """Approximate token count: ceil(non-whitespace-trimmed length / CHARS_PER_TOKEN)."""
    n = len(text.strip())
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if n else 0


def chunk_text(text: str, *, target_tokens: int, overlap_tokens: int) -> list[dict]:
    """Split ``text`` into ordered, overlapping chunks that are exact slices of ``text``.

    Each returned chunk carries ``start_offset``/``end_offset`` such that
    ``text[start_offset:end_offset] == chunk["text"]`` — offsets are relative to the ``text`` passed
    in (the extracted source), so they stay valid as citation anchors.
    """
    target_chars = max(1, target_tokens * CHARS_PER_TOKEN)
    overlap_chars = max(0, overlap_tokens * CHARS_PER_TOKEN)
    spans = _chunk_spans(text, target_chars, overlap_chars)
    return [
        {
            "index": i,
            "text": text[start:end],
            "char_count": end - start,
            "token_estimate": estimate_tokens(text[start:end]),
            "start_offset": start,
            "end_offset": end,
        }
        for i, (start, end) in enumerate(spans)
    ]


def _chunk_spans(text: str, target_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Compute ``(start, end)`` offset spans; ``text[start:end]`` is each chunk, verbatim."""
    n = len(text)
    spans: list[tuple[int, int]] = []
    start = _skip_whitespace(text, 0, n)
    while start < n:
        hard_end = start + target_chars
        if hard_end >= n:
            spans.append((start, _rstrip(text, start, n)))
            break
        cut = _boundary_cut(text, start, hard_end)  # split point (just past the separator)
        spans.append((start, _rstrip(text, start, cut)))
        nxt = _skip_whitespace(text, _next_start(text, start, cut, overlap_chars), n)
        # Forward progress is guaranteed by _next_start (> start); the guard is a belt-and-suspenders
        # backstop so a future edit that broke that invariant would end the loop, not hang.
        start = nxt if nxt > start else n
    return spans


def _boundary_cut(text: str, start: int, hard_end: int) -> int:
    """Offset just past the best separator in ``text[start:hard_end]``, or ``hard_end`` (hard cut).

    Prefers the largest natural boundary (paragraph → line → sentence → word) and, within a given
    boundary, the *latest* occurrence that fits — so a chunk holds as much whole structure as the
    target allows. The separator stays attached to the chunk (any trailing whitespace it introduces
    is trimmed by :func:`_rstrip` at the call site).
    """
    for separator in _BOUNDARIES:
        idx = text.rfind(separator, start, hard_end)
        if idx > start:
            return idx + len(separator)
    return hard_end


def _next_start(text: str, start: int, cut: int, overlap_chars: int) -> int:
    """Where the next chunk begins: back up from ``cut`` by the overlap, snapped to a word boundary.

    With no overlap the next chunk starts at the split point. With overlap it starts ``overlap_chars``
    earlier (but always past ``start`` for forward progress), advanced to the next word boundary so
    the repeated context begins on a whole word rather than mid-token.
    """
    if overlap_chars <= 0:
        return cut
    raw = max(start + 1, cut - overlap_chars)
    space = text.find(" ", raw, cut)
    return space + 1 if space != -1 else raw


def _skip_whitespace(text: str, i: int, n: int) -> int:
    while i < n and text[i].isspace():
        i += 1
    return i


def _rstrip(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end
