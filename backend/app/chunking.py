"""Recursive, token-aware text splitter (ADR-0003).

Splits on the largest natural boundary that fits — paragraphs, then lines, then sentences, then
words, then a hard character cut — and packs the pieces into ~target-token chunks with a small
overlap so context isn't lost across boundaries. Sizing uses a chars-per-token estimate to stay
dependency-free; #12 can swap in the embedding model's real tokenizer.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4
_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


def estimate_tokens(text: str) -> int:
    """Approximate token count: ceil(non-whitespace-trimmed length / CHARS_PER_TOKEN)."""
    n = len(text.strip())
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if n else 0


def chunk_text(text: str, *, target_tokens: int, overlap_tokens: int) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    target_chars = max(1, target_tokens * CHARS_PER_TOKEN)
    overlap_chars = max(0, overlap_tokens * CHARS_PER_TOKEN)
    pieces = _recursive_split(text, target_chars, _SEPARATORS)
    chunks = _merge(pieces, target_chars, overlap_chars)
    return [
        {
            "index": i,
            "text": chunk,
            "char_count": len(chunk),
            "token_estimate": estimate_tokens(chunk),
        }
        for i, chunk in enumerate(chunks)
    ]


def _recursive_split(text: str, target_chars: int, separators: list[str]) -> list[str]:
    """Break text into pieces no larger than target_chars, trying each separator in turn."""
    if len(text) <= target_chars:
        return [text] if text else []
    sep, rest = separators[0], separators[1:]
    if sep == "":  # last resort: hard character cut
        return [text[i : i + target_chars] for i in range(0, len(text), target_chars)]
    pieces: list[str] = []
    for part in text.split(sep):
        if not part:
            continue
        if len(part) <= target_chars:
            pieces.append(part)
        else:
            pieces.extend(_recursive_split(part, target_chars, rest))
    return pieces


def _merge(pieces: list[str], target_chars: int, overlap_chars: int) -> list[str]:
    """Greedily pack pieces into chunks up to target_chars, carrying an overlap tail forward."""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > target_chars:
            chunks.append(current)
            tail = _overlap_tail(current, overlap_chars)
            current = f"{tail} {piece}".strip() if tail else piece
        else:
            current = f"{current} {piece}".strip() if current else piece
    if current:
        chunks.append(current)
    return chunks


def _overlap_tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0:
        return ""
    tail = text[-overlap_chars:]
    if tail != text and " " in tail:  # start the overlap on a word boundary when possible
        tail = tail[tail.index(" ") + 1 :]
    return tail
