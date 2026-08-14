"""Rendering an evaluation report (#21).

Pure: takes a :class:`~app.eval.harness.Report` and returns text or a JSON-ready dict. Kept apart
from the harness so the formatting can be tested without a database, and so a run's numbers can be
re-rendered without re-running it.

Every rendering leads with the **provenance** — embedder, model, dimension, k, chunk settings,
corpus size — because a retrieval number without them is not a result, it is a rumour. A figure
produced by the hashing embedder and one produced by a real model differ by more than noise, and the
report is the only place that distinction is ever recorded.
"""

from __future__ import annotations

from app.eval.harness import Report

_RULE = "─" * 78


def as_dict(report: Report) -> dict:
    """The machine-readable form, for `--json` and for diffing two runs."""
    return {
        "configuration": {
            "embedder_class": report.embedder_class,
            "embedder_model": report.embedder_model,
            "embedding_dim": report.embedding_dim,
            "top_k": report.top_k,
            "min_similarity": report.min_similarity,
            "chunk_target_tokens": report.chunk_target_tokens,
            "chunk_overlap_tokens": report.chunk_overlap_tokens,
        },
        "corpus": {
            "documents": report.document_count,
            "chunks": report.chunk_count,
            "characters": report.corpus_chars,
            "questions": len(report.results),
            "out_of_corpus_questions": len(report.out_of_corpus),
        },
        "means": {
            **{f"hit@{k}": round(report.hit_rate(k), 4) for k in report.cutoffs},
            **{f"precision@{k}": round(report.precision(k), 4) for k in report.cutoffs},
            **{f"recall@{k}": round(report.recall(k), 4) for k in report.cutoffs},
            "mrr": round(report.mrr, 4),
        },
        "separation": {
            "worst_in_corpus_top_similarity": _round(report.worst_in_corpus_similarity),
            "best_out_of_corpus_top_similarity": _round(report.best_out_of_corpus_similarity),
            "gap": _round(report.separation),
        },
        "questions": [
            {
                "id": r.question.id,
                "question": r.question.question,
                "relevant_chunks_in_corpus": r.total_relevant,
                "relevance_by_rank": list(r.relevance),
                "top_similarity": round(r.top_similarity, 4),
                "reciprocal_rank": round(r.reciprocal_rank, 4),
                "retrieved_documents": list(r.retrieved_titles),
            }
            for r in report.results
        ],
        "out_of_corpus": [
            {
                "id": r.question.id,
                "question": r.question.question,
                "top_similarity": round(r.top_similarity, 4),
            }
            for r in report.out_of_corpus
        ],
        "warnings": list(report.warnings),
        "seconds": round(report.seconds, 2),
    }


def as_text(report: Report) -> str:
    lines: list[str] = []
    add = lines.append

    for warning in report.warnings:
        add("!" * 78)
        add(_wrap(warning))
        add("!" * 78)
        add("")

    add("TenantIQ — retrieval evaluation (#21)")
    add(_RULE)
    add(
        f"  embedder        {report.embedder_model} via {report.embedder_class}, "
        f"{report.embedding_dim}-dim"
    )
    add(f"  top_k           {report.top_k}")
    add(f"  min_similarity  {report.min_similarity}")
    add(
        f"  chunking        {report.chunk_target_tokens} target / "
        f"{report.chunk_overlap_tokens} overlap tokens"
    )
    add(
        f"  corpus          {report.document_count} documents, {report.chunk_count} chunks, "
        f"{report.corpus_chars:,} characters"
    )
    add(
        f"  questions       {len(report.results)} in-corpus, "
        f"{len(report.out_of_corpus)} out-of-corpus"
    )
    add("")

    header = f"  {'question':<26}{'rel':>4}{'rank of hits':>14}{'RR':>7}{'top sim':>9}"
    add(header)
    add(f"  {'-' * 74}")
    for result in report.results:
        ranks = [str(i) for i, hit in enumerate(result.relevance, start=1) if hit] or ["—"]
        add(
            f"  {result.question.id:<26}{result.total_relevant:>4}{','.join(ranks):>14}"
            f"{result.reciprocal_rank:>7.2f}{result.top_similarity:>9.3f}"
        )
    add("")

    add("  Means")
    add(f"  {'-' * 74}")
    add("  " + "".join(f"{'':<10}" + "".join(f"@{k:<8}" for k in report.cutoffs)))
    add("  " + f"{'hit':<10}" + "".join(f"{report.hit_rate(k):<9.2f}" for k in report.cutoffs))
    add(
        "  "
        + f"{'precision':<10}"
        + "".join(f"{report.precision(k):<9.2f}" for k in report.cutoffs)
    )
    add("  " + f"{'recall':<10}" + "".join(f"{report.recall(k):<9.2f}" for k in report.cutoffs))
    add(f"  {'MRR':<10}{report.mrr:.2f}")
    add("")
    add(
        _wrap(
            "precision@k is bounded above by (relevant chunks / k): most questions here have a single "
            "relevant chunk, so precision@5 cannot exceed 0.20 however perfect retrieval is. Read hit@k "
            "and MRR for whether the right passage was found, and recall@k for whether all of it was."
        )
    )
    add("")

    add("  Similarity separation")
    add(f"  {'-' * 74}")
    low, high, gap = (
        report.worst_in_corpus_similarity,
        report.best_out_of_corpus_similarity,
        report.separation,
    )
    if low is None or high is None:
        add("  not measured (no out-of-corpus questions in this run)")
    else:
        add(f"  worst in-corpus top similarity      {low:.3f}")
        add(f"  best out-of-corpus top similarity   {high:.3f}")
        add(f"  gap                                 {gap:+.3f}")
        add("")
        if gap > 0:
            add(
                _wrap(
                    f"A similarity floor anywhere in ({high:.3f}, {low:.3f}) refuses every question "
                    "this corpus cannot answer while still answering every one it can."
                )
            )
            add("")
            add(_wrap(_verdict_on_configured_floor(report.min_similarity, low, high)))
        else:
            add(
                _wrap(
                    "The ranges overlap: no single similarity floor separates answerable questions from "
                    "unanswerable ones on this corpus. Tuning the threshold cannot fix that — it needs a "
                    "better embedder or a different refusal signal."
                )
            )
        add("")
        add(
            _wrap(
                "The floor does not affect the metrics above: they are measured on raw nearest-neighbour "
                "results, because a threshold that hid a relevant chunk would show up as a retrieval "
                "failure rather than as the tuning choice it is. This section is how the floor gets "
                "chosen, not something the floor was applied to."
            )
        )
    add("")
    add(f"  completed in {report.seconds:.1f}s")
    return "\n".join(lines)


def _verdict_on_configured_floor(configured: float, low: float, high: float) -> str:
    """Where the floor this run was configured with actually sits relative to that window.

    Written as three explicit cases rather than one sentence, because the interesting ones are the
    two failures and they say opposite things: too low admits noise as evidence, too high refuses
    questions the corpus can answer. An earlier version of this report asserted a single outcome and
    was simply wrong whenever the environment set a floor.
    """
    if configured <= high:
        return (
            f"The configured TENANTIQ_RETRIEVAL_MIN_SIMILARITY is {configured}, at or below the "
            f"best unanswerable question's score ({high:.3f}) — so it refuses nothing, and every "
            "question gets sources whether or not the corpus has an answer."
        )
    if configured >= low:
        return (
            f"The configured TENANTIQ_RETRIEVAL_MIN_SIMILARITY is {configured}, at or above the "
            f"worst answerable question's score ({low:.3f}) — so it refuses questions this corpus "
            "can actually answer. Lower it into the window above."
        )
    return (
        f"The configured TENANTIQ_RETRIEVAL_MIN_SIMILARITY is {configured}, which sits inside that "
        "window: on this dataset it separates answerable from unanswerable questions exactly."
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _wrap(text: str, width: int = 74, indent: str = "  ") -> str:
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(indent + line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(indent + line)
    return "\n".join(out)
