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

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile

from app.embeddings import get_embedder
from app.eval.claims import Claim, split_claims
from app.eval.dataset import Dataset, Question, load_dataset
from app.eval.judge import JudgedSource, Verdict, get_judge
from app.eval.metrics import (
    hit_at_k,
    is_relevant,
    mean,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.generation import (
    CitationsEvent,
    ErrorEvent,
    TokenEvent,
    get_llm,
    stream_grounded_answer,
)
from app.ingestion import run_ingestion
from app.models import Chunk, Document, Tenant
from app.rag import retrieve_context
from app.retrieval import nearest_chunks
from app.tenant_context import tenant_context

#: The scratch tenant the corpus is ingested into. The name is deliberately unusable as a real
#: workspace slug so it can never collide with a customer's.
EVAL_TENANT_SLUG = "eval-harness-scratch"
EVAL_TENANT_ISSUER = "https://eval.invalid/realms/eval-harness"

#: Cut-offs the report tabulates. `1` is what actually matters for a grounded answer's first source;
#: `5` is the default `TENANTIQ_RETRIEVAL_TOP_K`, i.e. what the product really shows.
DEFAULT_CUTOFFS = (1, 3, 5)

logger = logging.getLogger(__name__)


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
class AnswerResult:
    """One generated answer, broken into claims and judged (#22)."""

    question: Question
    answer: str
    claims: tuple[Claim, ...]
    #: One verdict per *cited* claim, in claim order. Uncited claims are not judged — there is
    #: nothing to judge them against, and their failure is the missing citation itself.
    verdicts: tuple[Verdict, ...]
    refused: bool
    #: Why this question produced no measurement, if it did not. A model call that timed out or a
    #: backend that fell over is a gap in coverage, not evidence about grounding, so it is reported
    #: separately and contributes to no score. Empty when the answer was measured.
    error: str = ""

    @property
    def measured(self) -> bool:
        return not self.error

    @property
    def cited_claims(self) -> tuple[Claim, ...]:
        return tuple(claim for claim in self.claims if claim.is_cited)

    @property
    def uncited_claims(self) -> tuple[Claim, ...]:
        return tuple(claim for claim in self.claims if not claim.is_cited)

    @property
    def uncited_numeric_claims(self) -> tuple[Claim, ...]:
        """Sentences stating a figure with nothing to back it — the worst thing an answer here can
        contain, given that the product's first rule is that the LLM never computes numbers."""
        return tuple(claim for claim in self.claims if claim.has_number and not claim.is_cited)

    @property
    def invented_citations(self) -> tuple[int, ...]:
        """Source numbers the prose cites that were never offered to the model.

        ``generation._resolve_citations`` already drops these from the resolved citation list, so the
        API never returns a dangling citation — but the *prose keeps the marker*, and an answer that
        reads "as set out in [7]" when there was no source 7 is the failure this project claims is
        impossible. Nothing sees it unless something reads the text.
        """
        return tuple(number for claim in self.claims for number in claim.invented)

    @property
    def supported(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.is_supported)

    @property
    def unsupported(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.is_unsupported)

    @property
    def unclear(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.is_unclear)


@dataclass(frozen=True)
class FaithfulnessReport:
    """Aggregate grounding quality over the whole answer set (#22)."""

    judge_model: str
    generator_model: str
    answers: tuple[AnswerResult, ...]

    @property
    def measured(self) -> tuple[AnswerResult, ...]:
        """The answers that produced a measurement. Every score is over these, never over the
        questions whose model call failed — a timeout says nothing about grounding, and letting it
        count as either supported or unsupported would put infrastructure noise in the headline."""
        return tuple(answer for answer in self.answers if answer.measured)

    @property
    def failed(self) -> tuple[AnswerResult, ...]:
        return tuple(answer for answer in self.answers if not answer.measured)

    @property
    def total_claims(self) -> int:
        return sum(len(answer.claims) for answer in self.measured)

    @property
    def total_cited_claims(self) -> int:
        return sum(len(answer.cited_claims) for answer in self.measured)

    @property
    def citation_coverage(self) -> float:
        """Share of claims that cite anything at all. The grounding contract requires every claim to
        carry the source it rests on, so this is a contract-compliance number, not a style one."""
        return self.total_cited_claims / self.total_claims if self.total_claims else 0.0

    @property
    def faithfulness(self) -> float:
        """Of the claims that *were* cited, the share the judge found supported."""
        judged = self.total_cited_claims
        return sum(a.supported for a in self.measured) / judged if judged else 0.0

    @property
    def grounded(self) -> float:
        """Of **all** claims, the share that are both cited and supported.

        The headline number, and deliberately the harsher one: an uncited claim is ungrounded whether
        or not a source happens to exist that would have backed it. Reporting only `faithfulness`
        would let an answer that cites one sentence out of six score 1.00.
        """
        return (
            sum(a.supported for a in self.measured) / self.total_claims
            if self.total_claims
            else 0.0
        )

    @property
    def unsupported_claims(self) -> int:
        return sum(answer.unsupported for answer in self.measured)

    @property
    def unclear_claims(self) -> int:
        return sum(answer.unclear for answer in self.measured)

    @property
    def uncited_numeric_claims(self) -> int:
        return sum(len(answer.uncited_numeric_claims) for answer in self.measured)

    @property
    def invented_citations(self) -> int:
        return sum(len(answer.invented_citations) for answer in self.measured)

    @property
    def judge_is_the_generator(self) -> bool:
        """Self-assessment, which is known-optimistic and has to be said out loud."""
        return self.judge_model == self.generator_model


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
    #: Present only when the run was asked for it: generating and judging an answer per question
    #: costs two model calls each, which is minutes rather than seconds (#22).
    faithfulness: "FaithfulnessReport | None" = None

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


def evaluate(
    *,
    top_k: int | None = None,
    dataset: Dataset | None = None,
    faithfulness: bool = False,
) -> Report:
    """Run the whole evaluation and return its report. Cleans up the scratch tenant either way.

    ``faithfulness`` adds the #22 pass: generate a real answer per question and have a judge rule on
    every claim in it. It runs inside the same ingestion as the retrieval pass — embedding the corpus
    twice to measure two things about it would be wasteful and, worse, would let the two halves of the
    report describe two different corpora.
    """
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
            grounding = _judge_answers(data, k) if faithfulness else None
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
            warnings=_warnings(embedder, grounding),
            faithfulness=grounding,
        )
    finally:
        _drop_scratch_tenant()


def _judge_answers(data: Dataset, k: int) -> FaithfulnessReport:
    """Generate a real answer per question and have the judge rule on every claim in it (#22).

    Both halves go through the product's own code — ``retrieve_context`` and ``generate_answer`` — so
    what is measured is the answer a user would actually have received, including the similarity floor
    and the citation-resolution enforcement, rather than a reconstruction of them.
    """
    judge = get_judge()
    generator = get_llm()
    answers: list[AnswerResult] = []

    for question in data.questions:
        # Per question, not per run. Two model calls each over eighteen questions is a long enough
        # window that something will eventually time out, and the first version of this let a single
        # timeout throw away every answer already collected. A failed question is recorded as a gap
        # in coverage and the run carries on.
        try:
            context = retrieve_context(question.question, k=k)
            text, refused = _stream_answer(context)
            claims = split_claims(text, available_sources=len(context.sources))
            cited = [(claim.index, claim.text, claim.cited) for claim in claims if claim.is_cited]
            sources = tuple(
                JudgedSource(number=source.number, text=source.text) for source in context.sources
            )
            verdicts = judge.judge(cited, sources) if cited else ()
        except Exception as exc:  # noqa: BLE001 — any backend failure is a gap, never a verdict
            logger.warning("faithfulness: %s produced no measurement (%s)", question.id, exc)
            answers.append(
                AnswerResult(
                    question=question,
                    answer="",
                    claims=(),
                    verdicts=(),
                    refused=False,
                    error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                )
            )
            continue
        answers.append(
            AnswerResult(
                question=question,
                answer=text,
                claims=claims,
                verdicts=verdicts,
                refused=refused,
            )
        )

    return FaithfulnessReport(
        judge_model=getattr(judge, "model", type(judge).__name__),
        generator_model=getattr(generator, "model", type(generator).__name__),
        answers=tuple(answers),
    )


def _warnings(embedder, grounding: "FaithfulnessReport | None" = None) -> tuple[str, ...]:
    """The caveats that decide whether these numbers mean anything at all."""
    notes: list[str] = []
    if type(embedder).__name__ == "HashingEmbedder":
        notes.append(
            f"This run used {type(embedder).__name__} ({embedder.model!r}), which encodes lexical "
            "overlap and no semantics whatsoever. The retrieval numbers below prove the harness "
            "works; they are NOT retrieval quality, and must never be recorded as a baseline. Run "
            "against the real embedder (`make eval`, which executes inside the compose stack)."
        )
    if grounding is not None and grounding.judge_model == "fake-judge-v1":
        notes.append(
            "The faithfulness numbers came from FakeJudge, which checks lexical containment rather "
            "than reading anything. They exercise the scoring pipeline and are not a grounding "
            "measurement."
        )
    elif grounding is not None and grounding.judge_is_the_generator:
        notes.append(
            f"The judge and the generator are the same model ({grounding.judge_model!r}). That is "
            "self-assessment, which is known-optimistic: treat the faithfulness figure as a floor on "
            "how bad things are, not as a measurement. Set TENANTIQ_EVAL_JUDGE_MODEL (or "
            "TENANTIQ_EVAL_JUDGE_OLLAMA_MODEL) to a different model to break the tie."
        )
    return tuple(notes)


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


def _stream_answer(context) -> tuple[str, bool]:
    """Produce the answer **the product actually serves**, and return ``(text, refused)``.

    This deliberately uses ``stream_grounded_answer`` rather than ``generate_answer``, and the
    distinction turned out to be the difference between measuring grounding and measuring nothing.

    The two paths cite differently. The non-streaming path asks for structured
    ``{answer, citations}`` output, so the citation list is **answer-level** and the prose need not
    contain a single ``[n]`` marker — a per-*claim* grounding score is not even expressible on it. The
    streaming path (#48), which is what the SSE endpoint and therefore every real user receives,
    resolves citations from inline markers in the prose. Measuring prose markers on the structured
    path reported a citation coverage of 0.05, which said nothing about the model and everything about
    the harness reading the wrong output.

    A mid-stream failure arrives as an ``ErrorEvent`` rather than an exception, so it is raised here
    to be recorded as a gap in coverage by the caller.
    """
    chunks: list[str] = []
    refused = False
    for event in stream_grounded_answer(context):
        if isinstance(event, TokenEvent):
            chunks.append(event.text)
        elif isinstance(event, CitationsEvent):
            refused = event.refused
        elif isinstance(event, ErrorEvent):
            raise EvaluationError(event.message)
    return "".join(chunks), refused


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
