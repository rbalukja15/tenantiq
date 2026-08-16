"""The retrieval evaluation harness (#21).

Ingests the evaluation corpus into a scratch tenant **through the real ingestion pipeline**, runs
every question through **the real retrieval path**, and scores the results against the dataset's
marker phrases. Reimplementing either side would measure a parallel universe: the numbers are only
worth anything if extraction, PII redaction, chunking, embedding and the pgvector search are the same
code the product runs.

Two properties are load-bearing and easy to lose:

- **The relevant-set size is derived, never asserted.** How many chunks in the corpus answer a given
  question depends on how the corpus happened to be chunked, so it is counted at run time by
  scanning every chunk. A dataset that hard-coded it would go quietly wrong the first time anyone
  touched ``TENANTIQ_CHUNK_TARGET_TOKENS``.
- **A marker that survives validation but matches no *ingested* chunk is a hard error.** Validation
  reads the raw corpus; ingestion redacts PII and chunks the text before it is stored. If redaction
  rewrites a phrase a marker depends on, every question using it would score zero and look like a
  retrieval failure. That has to fail loudly, naming the marker.

The scratch tenant is deleted afterwards, and any leftover from an interrupted run is removed first,
so the harness cannot silently accumulate corpora or measure yesterday's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile

from app.embeddings import get_embedder
from app.eval.dataset import Dataset, Question, load_dataset
from app.eval.metrics import (
    hit_at_k,
    is_relevant,
    mean,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.ingestion import run_ingestion
from app.models import Chunk, Document, Tenant
from app.retrieval import nearest_chunks
from app.tenant_context import tenant_context

#: The scratch tenant the corpus is ingested into. The name is deliberately unusable as a real
#: workspace slug so it can never collide with a customer's.
EVAL_TENANT_SLUG = "eval-harness-scratch"
EVAL_TENANT_ISSUER = "https://eval.invalid/realms/eval-harness"

#: Cut-offs the report tabulates. `1` is what actually matters for a grounded answer's first source;
#: `5` is the default `TENANTIQ_RETRIEVAL_TOP_K`, i.e. what the product really shows.
DEFAULT_CUTOFFS = (1, 3, 5)


class EvaluationError(RuntimeError):
    """The run could not produce a trustworthy measurement."""


@dataclass(frozen=True)
class QuestionResult:
    question: Question
    total_relevant: int
    relevance: tuple[bool, ...]
    top_similarity: float
    retrieved_titles: tuple[str, ...]

    def hit(self, k: int) -> bool:
        return hit_at_k(self.relevance, k)

    def precision(self, k: int) -> float:
        return precision_at_k(self.relevance, k)

    def recall(self, k: int) -> float:
        return recall_at_k(self.relevance, self.total_relevant, k)

    @property
    def reciprocal_rank(self) -> float:
        return reciprocal_rank(self.relevance)


@dataclass(frozen=True)
class OutOfCorpusResult:
    question: Question
    top_similarity: float


@dataclass(frozen=True)
class Report:
    embedder_model: str
    embedder_class: str
    embedding_dim: int
    top_k: int
    min_similarity: float
    chunk_target_tokens: int
    chunk_overlap_tokens: int
    document_count: int
    chunk_count: int
    corpus_chars: int
    results: tuple[QuestionResult, ...]
    out_of_corpus: tuple[OutOfCorpusResult, ...]
    cutoffs: tuple[int, ...] = DEFAULT_CUTOFFS
    seconds: float = 0.0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def hit_rate(self, k: int) -> float:
        return mean(1.0 if r.hit(k) else 0.0 for r in self.results)

    def precision(self, k: int) -> float:
        return mean(r.precision(k) for r in self.results)

    def recall(self, k: int) -> float:
        return mean(r.recall(k) for r in self.results)

    @property
    def mrr(self) -> float:
        return mean(r.reciprocal_rank for r in self.results)

    @property
    def worst_in_corpus_similarity(self) -> float | None:
        """The lowest top-hit similarity across in-corpus questions — the bottom of the window any
        similarity floor has to stay under, or it starts refusing questions the corpus can answer.
        """
        return min((r.top_similarity for r in self.results), default=None)

    @property
    def best_out_of_corpus_similarity(self) -> float | None:
        """The highest top-hit similarity across questions the corpus cannot answer — the top of the
        window a floor has to stay above, or it admits noise as evidence."""
        return max((r.top_similarity for r in self.out_of_corpus), default=None)

    @property
    def separation(self) -> float | None:
        """Positive means some threshold separates answerable from unanswerable; negative means none
        does, and no amount of tuning ``TENANTIQ_RETRIEVAL_MIN_SIMILARITY`` will."""
        low, high = self.worst_in_corpus_similarity, self.best_out_of_corpus_similarity
        if low is None or high is None:
            return None
        return low - high


def evaluate(*, top_k: int | None = None, dataset: Dataset | None = None) -> Report:
    """Run the whole evaluation and return its report. Cleans up the scratch tenant either way."""
    data = dataset or load_dataset()
    k = top_k if top_k is not None else settings.TENANTIQ_RETRIEVAL_TOP_K
    embedder = get_embedder()
    started = time.monotonic()

    _drop_scratch_tenant()
    tenant = Tenant.objects.create(
        slug=EVAL_TENANT_SLUG,
        name="Evaluation harness (scratch)",
        oidc_issuer=EVAL_TENANT_ISSUER,
        oidc_client_id="eval-harness",
        is_active=False,  # never a signable-into workspace, even for the moments it exists
    )
    try:
        with tenant_context(tenant):
            _ingest_corpus(data)
            corpus = list(Chunk.objects.select_related("document").all())
            if not corpus:
                raise EvaluationError("The corpus ingested to zero chunks; nothing to measure.")
            results = tuple(_score(question, corpus, k) for question in data.questions)
            out_of_corpus = tuple(
                OutOfCorpusResult(question=question, top_similarity=_top_similarity(question, k))
                for question in data.out_of_corpus
            )
        return Report(
            embedder_model=embedder.model,
            embedder_class=type(embedder).__name__,
            embedding_dim=embedder.dim,
            top_k=k,
            min_similarity=settings.TENANTIQ_RETRIEVAL_MIN_SIMILARITY,
            chunk_target_tokens=settings.TENANTIQ_CHUNK_TARGET_TOKENS,
            chunk_overlap_tokens=settings.TENANTIQ_CHUNK_OVERLAP_TOKENS,
            document_count=len(data.documents),
            chunk_count=len(corpus),
            corpus_chars=data.total_chars,
            results=results,
            out_of_corpus=out_of_corpus,
            seconds=time.monotonic() - started,
            warnings=_warnings(embedder),
        )
    finally:
        _drop_scratch_tenant()


def _warnings(embedder) -> tuple[str, ...]:
    """The one caveat that decides whether these numbers mean anything at all."""
    if type(embedder).__name__ != "HashingEmbedder":
        return ()
    return (
        f"This run used {type(embedder).__name__} ({embedder.model!r}), which encodes lexical "
        "overlap and no semantics whatsoever. The numbers below prove the harness works; they are "
        "NOT retrieval quality, and must never be recorded as a baseline. Run against the real "
        "embedder (`make eval`, which executes inside the compose stack) for a meaningful result.",
    )


def _ingest_corpus(data: Dataset) -> None:
    """Push every corpus document through the real ingestion pipeline, synchronously."""
    tenant = Tenant.objects.get(slug=EVAL_TENANT_SLUG)
    for source in data.documents:
        payload = source.text.encode("utf-8")
        document = Document.objects.create(
            title=source.name,
            original_filename=source.name,
            content_type="text/plain",
            size_bytes=len(payload),
        )
        document.file.save(source.name, ContentFile(payload), save=True)
        run_ingestion(document.id, tenant.id)

    failed = list(Document.objects.exclude(status=Document.Status.READY))
    if failed:
        detail = ", ".join(
            f"{d.title} ({d.status}: {d.error or 'no reason recorded'})" for d in failed
        )
        raise EvaluationError(f"Corpus did not ingest cleanly: {detail}")


def _score(question: Question, corpus: list[Chunk], k: int) -> QuestionResult:
    # Derived, not asserted: how many chunks answer this question depends on how the corpus was
    # chunked, which is a property of the configuration under test.
    total_relevant = sum(1 for chunk in corpus if is_relevant(chunk.text, question.markers))
    if total_relevant == 0:
        raise EvaluationError(
            f"{question.id}: no ingested chunk contains any of its markers, though the raw corpus "
            "does. Ingestion rewrote the text — PII redaction is the usual cause — so this question "
            "would score zero for a reason that has nothing to do with retrieval. "
            f"Markers: {list(question.markers)}"
        )

    retrieved = nearest_chunks(question.question, k=k)
    return QuestionResult(
        question=question,
        total_relevant=total_relevant,
        relevance=tuple(is_relevant(chunk.text, question.markers) for chunk in retrieved),
        top_similarity=_similarity(retrieved[0]) if retrieved else 0.0,
        retrieved_titles=tuple(chunk.document.title for chunk in retrieved),
    )


def _top_similarity(question: Question, k: int) -> float:
    retrieved = nearest_chunks(question.question, k=k)
    return _similarity(retrieved[0]) if retrieved else 0.0


def _similarity(chunk: Chunk) -> float:
    return 1.0 - float(chunk.distance)


def _drop_scratch_tenant() -> None:
    """Remove the scratch tenant, its rows and its stored files.

    Runs before and after every evaluation. Before, because an interrupted run would otherwise leave
    a corpus behind and the next run would measure both copies; after, because leaving a tenant full
    of documents in the database of whoever ran `make eval` is not a thing a tool should do.
    """
    tenant = Tenant.objects.filter(slug=EVAL_TENANT_SLUG).first()
    if tenant is None:
        return
    with tenant_context(tenant):
        for document in Document.objects.all():
            storage, name = document.file.storage, document.file.name
            if name:
                storage.delete(name)
        # The tenant-owned rows are deleted *here*, inside the tenant context, and not left to a
        # cascade from `tenant.delete()`. `Tenant` is not tenant-owned, so deleting it runs with no
        # `app.current_tenant` set — and the RLS policy on `app_document` then matches zero rows,
        # deletes nothing, and the foreign key blocks the tenant delete with an IntegrityError.
        # Layer 2 doing exactly its job: a delete with no tenant in context reaches no rows.
        Chunk.objects.all().delete()
        Document.objects.all().delete()
    tenant.delete()
