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


# --- #45: every chunk is an exact, offset-addressable slice of the source text ---


def _multi_paragraph_document() -> str:
    """A realistic multi-paragraph document comfortably larger than the default chunk target."""
    paragraphs = []
    for p in range(10):
        sentences = [
            f"Paragraph {p} sentence {s} discusses quarterly revenue and invoice payment terms."
            for s in range(8)
        ]
        paragraphs.append(" ".join(sentences))
    return "\n\n".join(paragraphs)


def test_every_chunk_is_an_exact_offset_slice_of_the_source():
    # The core #45 guarantee: stored text must be a verbatim slice of the source (so citations can
    # quote it and offsets address it), not a separator-stripped, space-rejoined approximation.
    source = _multi_paragraph_document()
    chunks = chunk_text(source, target_tokens=800, overlap_tokens=100)

    assert len(chunks) > 1  # actually split, so the bug can manifest
    for c in chunks:
        assert c["text"] in source  # faithful substring (fails on main: 0 chunks are substrings)
        assert (
            source[c["start_offset"] : c["end_offset"]] == c["text"]
        )  # exactly offset-addressable
        assert c["end_offset"] > c["start_offset"]


def test_chunks_preserve_sentence_punctuation():
    # A separator-discarding splitter drops the sentence periods; a faithful slice keeps them.
    source = _multi_paragraph_document()
    chunks = chunk_text(source, target_tokens=800, overlap_tokens=100)
    assert any("." in c["text"] for c in chunks)
    assert all(c["text"] == source[c["start_offset"] : c["end_offset"]] for c in chunks)


def test_offsets_advance_across_chunks():
    source = _multi_paragraph_document()
    chunks = chunk_text(source, target_tokens=800, overlap_tokens=100)
    for earlier, later in zip(chunks, chunks[1:]):
        assert later["start_offset"] > earlier["start_offset"]  # forward progress
        assert (
            later["start_offset"] <= earlier["end_offset"]
        )  # contiguous or overlapping, no gap-jump


def _drop_whitespace(text: str) -> str:
    return "".join(text.split())


def _long_single_paragraph() -> str:
    # One paragraph (no blank lines) far larger than the chunk target, so the splitter must break it
    # on sentence boundaries. A separator-*discarding* splitter drops those sentence periods to fit;
    # a faithful slicer keeps them. This is the input that makes the completeness check bite.
    return " ".join(f"Sentence number {i} reports revenue and invoices." for i in range(300))


def test_zero_overlap_slices_tile_the_source_without_loss():
    # Faithfulness is two-sided: chunks are exact slices *and* they cover the whole source. With no
    # overlap, concatenating the ordered slices reproduces every non-whitespace character of the
    # source, in order — only inter-chunk whitespace is dropped. The sentence periods survive here
    # exactly because chunks are verbatim slices; the old ". "-splitting implementation dropped them,
    # so this assertion genuinely fails on main.
    source = _long_single_paragraph()
    chunks = chunk_text(source, target_tokens=800, overlap_tokens=0)
    assert len(chunks) > 1
    joined = "".join(c["text"] for c in chunks)
    assert _drop_whitespace(joined) == _drop_whitespace(source)  # no non-whitespace content lost
