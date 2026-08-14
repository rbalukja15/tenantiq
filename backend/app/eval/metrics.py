"""Retrieval metrics (#21).

Pure functions over a *relevance judgement per retrieved chunk, in rank order* — a list of booleans
and nothing else. Keeping them at that level is deliberate: it means the metrics know nothing about
tenants, embeddings, or the database, so they can be tested exhaustively against hand-written
rankings where the right answer is obvious by inspection. Everything database-shaped lives in
``run.py``.

**What each number is for**, because a table of four metrics invites reading the wrong one:

- ``hit@k`` — did *anything* useful come back in the top k? The most interpretable number here, and
  the one that matters most for this product: the prompt is assembled from the top k, so a miss at k
  means the answer cannot be grounded at all.
- ``recall@k`` — of the chunks that *should* have come back, how many did? The metric that notices
  when a question has evidence in two places and retrieval only found one.
- ``precision@k`` — of what was returned, how much was relevant. Read it with its ceiling in mind:
  when a question has one relevant chunk, precision@5 cannot exceed 0.2 however perfect retrieval
  is, so a low value here is often arithmetic rather than a defect.
- ``MRR`` — how far down the list the first relevant chunk sat. Rank is not cosmetic: sources are
  numbered in the prompt in retrieval order, so a relevant chunk at position 5 competes with four
  irrelevant ones for the model's attention.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces and trim.

    Used **only** for comparison, never for storage. The corpus is hard-wrapped and chunk text is
    stored verbatim (#45), so a marker phrase written naturally in the dataset would otherwise fail
    to match purely because a newline fell in the middle of it — a false negative that would look
    like a retrieval failure rather than the formatting artefact it is.
    """
    return _WHITESPACE.sub(" ", text).strip()


def is_relevant(chunk_text: str, markers: Sequence[str]) -> bool:
    """Whether ``chunk_text`` contains any of the question's marker phrases."""
    haystack = normalize(chunk_text)
    return any(normalize(marker) in haystack for marker in markers)


def precision_at_k(relevance: Sequence[bool], k: int) -> float:
    """Fraction of the top ``k`` *returned* results that were relevant.

    The denominator is how many results there actually are in the top k, not ``k`` itself. On a
    corpus smaller than ``k`` the two differ, and dividing by ``k`` would report a system that
    returned three results and got all three right as 0.6 — penalising it for the corpus being
    small rather than for anything it did.
    """
    window = list(relevance)[:k]
    if not window:
        return 0.0
    return sum(window) / len(window)


def recall_at_k(relevance: Sequence[bool], total_relevant: int, k: int) -> float:
    """Fraction of all relevant chunks in the corpus that appeared in the top ``k``.

    ``total_relevant`` must be positive: a question with no relevant chunk anywhere is a broken
    dataset entry, and returning 0.0 (or 1.0) for it would fold that breakage into the score
    instead of surfacing it. The caller checks first and fails loudly.
    """
    if total_relevant <= 0:
        raise ValueError("recall is undefined when no chunk in the corpus is relevant")
    return sum(list(relevance)[:k]) / total_relevant


def hit_at_k(relevance: Sequence[bool], k: int) -> bool:
    """Whether at least one relevant chunk appeared in the top ``k``."""
    return any(list(relevance)[:k])


def reciprocal_rank(relevance: Sequence[bool]) -> float:
    """``1 / rank`` of the first relevant result, or 0.0 if none was relevant."""
    for position, relevant in enumerate(relevance, start=1):
        if relevant:
            return 1.0 / position
    return 0.0


def mean(values: Iterable[float]) -> float:
    """Arithmetic mean, with an empty sequence reported as 0.0 rather than raising.

    An empty run is a real state (every question filtered out by a ``--only`` selector, say), and
    the runner reports the question count beside every mean, so a 0.0 here can never be mistaken
    for a measured result.
    """
    items = list(values)
    return sum(items) / len(items) if items else 0.0
