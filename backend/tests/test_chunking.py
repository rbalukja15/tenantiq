"""TDD for the recursive, token-aware text splitter (app.chunking) — ADR-0003.

Pure logic, no DB: proves chunks respect the target size, overlap duplicates boundary context,
and oversized unbroken text falls back to a hard split.
"""

from __future__ import annotations

from app.chunking import chunk_text, estimate_tokens


def test_estimate_tokens_zero_for_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("   \n ") == 0


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("ab") <= estimate_tokens("abcd") <= estimate_tokens("abcdefghij")


def test_empty_text_yields_no_chunks():
    assert chunk_text("", target_tokens=800, overlap_tokens=100) == []
    assert chunk_text("   \n\n  ", target_tokens=800, overlap_tokens=100) == []


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("hello world", target_tokens=800, overlap_tokens=100)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "hello world"
    assert chunks[0]["index"] == 0
    assert chunks[0]["char_count"] == len("hello world")
    assert chunks[0]["token_estimate"] >= 1


def test_long_text_splits_into_ordered_chunks_within_target():
    text = " ".join(f"w{i}" for i in range(200))
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=0)
    assert len(chunks) > 1
    assert [c["index"] for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c["token_estimate"] <= 12  # target 10 + small word-boundary tolerance


def test_oversized_unbroken_text_is_hard_split():
    chunks = chunk_text("x" * 1000, target_tokens=10, overlap_tokens=0)  # no separators at all
    assert len(chunks) > 1
    for c in chunks:
        assert c["char_count"] <= 40  # target_tokens * 4 chars/token


def test_overlap_duplicates_boundary_content():
    text = " ".join(f"w{i}" for i in range(200))
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=4)
    words = [w for c in chunks for w in c["text"].split()]
    assert len(words) > len(set(words))  # overlap repeated some words across chunk boundaries


def test_zero_overlap_has_no_duplication():
    text = " ".join(f"w{i}" for i in range(200))
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=0)
    words = [w for c in chunks for w in c["text"].split()]
    assert len(words) == len(set(words))  # every word appears exactly once
