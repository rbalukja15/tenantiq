"""The evaluation harness end to end (#21).

Postgres-only: the harness runs the *real* retrieval path, and vector search is a pgvector feature.
That is the point — an evaluation that measured a reimplementation of retrieval would report on a
parallel universe.

What is asserted here is not retrieval *quality* — the suite's embedder is the hashing one, which
encodes lexical overlap and nothing else, and asserting a score against it would bake a meaningless
number into CI. What is asserted is that the measurement is **sound**: the corpus really goes through
ingestion, the relevant set is derived from what was actually stored, a dataset that cannot be
measured fails loudly rather than scoring zero, and the scratch tenant does not outlive the run.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection

from app.eval.harness import EVAL_TENANT_SLUG, EvaluationError, evaluate
from app.eval.dataset import load_dataset
from app.eval.report import as_dict, as_text
from app.models import Chunk, Document, Tenant

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


def _dataset(tmp_path, documents: dict[str, str], questions: list[dict], out_of_corpus=()):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        (corpus / name).write_text(text, encoding="utf-8")
    for question in questions:
        question.setdefault("expected_document", next(iter(documents), ""))
    (tmp_path / "questions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "corpus_dir": "corpus",
                "questions": questions,
                "out_of_corpus": list(out_of_corpus),
            }
        ),
        encoding="utf-8",
    )
    return load_dataset(tmp_path)


@requires_postgres
def test_it_measures_a_corpus_it_ingested_itself(tmp_path):
    data = _dataset(
        tmp_path,
        {
            "terms.txt": "Invoices are payable within forty-five days of the invoice date.",
            "handbook.txt": "Hotel nights in London are capped at two hundred pounds.",
        },
        [
            {"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]},
            {
                "id": "hotel",
                "question": "What is the hotel cap?",
                "markers": ["two hundred pounds"],
            },
        ],
    )

    report = evaluate(dataset=data, top_k=2)

    assert report.document_count == 2
    assert report.chunk_count >= 2
    assert len(report.results) == 2
    # Each question has exactly one relevant chunk here, derived by scanning what was stored.
    assert {r.total_relevant for r in report.results} == {1}


@requires_postgres
def test_the_scratch_tenant_does_not_outlive_the_run(tmp_path):
    # Leaving a tenant full of documents in the database of whoever ran `make eval` is not a thing a
    # measurement tool should do — and the next run would then measure two copies of the corpus.
    data = _dataset(
        tmp_path,
        {"a.txt": "The quick brown fox jumps over the lazy dog."},
        [{"id": "q", "question": "What jumps?", "markers": ["quick brown fox"]}],
    )

    evaluate(dataset=data, top_k=1)

    assert not Tenant.objects.filter(slug=EVAL_TENANT_SLUG).exists()
    assert Document.all_objects.count() == 0
    assert Chunk.all_objects.count() == 0


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_cleanup_survives_row_level_security_with_real_transactions(tmp_path):
    """The cleanup bug that every other test in this file was blind to.

    `Tenant` is not tenant-owned, so `tenant.delete()` runs with no `app.current_tenant` set — and
    the RLS policy on `app_document` then matches zero rows, cascades nothing, and the foreign key
    raises. In production that killed every run.

    Every other test here missed it because pytest wraps each test in a single transaction, so the
    `SET LOCAL` GUC from the last `tenant_context` was *still active* when the tenant was deleted.
    The isolation layer was being satisfied by a test-harness artefact. `transaction=True` uses real
    commits, which is the only way this fails when it is broken.
    """
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    evaluate(dataset=data, top_k=1)  # must not raise IntegrityError

    assert not Tenant.objects.filter(slug=EVAL_TENANT_SLUG).exists()


@requires_postgres
def test_a_leftover_corpus_from_an_interrupted_run_is_cleared_first(tmp_path):
    # Otherwise a crashed run leaves a corpus behind and the next one silently measures both.
    Tenant.objects.create(
        slug=EVAL_TENANT_SLUG,
        name="stale",
        oidc_issuer="https://eval.invalid/realms/eval-harness",
        oidc_client_id="eval-harness",
    )
    data = _dataset(
        tmp_path,
        {"a.txt": "The quick brown fox jumps over the lazy dog."},
        [{"id": "q", "question": "What jumps?", "markers": ["quick brown fox"]}],
    )

    report = evaluate(dataset=data, top_k=1)

    assert report.document_count == 1


@requires_postgres
def test_a_marker_that_ingestion_rewrites_aborts_rather_than_scoring_zero(tmp_path):
    # The subtle one. Validation reads the raw corpus, but ingestion redacts PII (#16) before
    # chunking — so a marker containing an email address passes validation and then matches nothing
    # that was actually stored. Scoring it would report a retrieval failure that is really a
    # dataset failure, and the two are indistinguishable in the output.
    data = _dataset(
        tmp_path,
        {"contacts.txt": "For escalation, contact alice@acme.test at any time of day."},
        [{"id": "who", "question": "Who do I contact?", "markers": ["contact alice@acme.test"]}],
    )

    with pytest.raises(EvaluationError, match="no ingested chunk contains any of its markers"):
        evaluate(dataset=data, top_k=1)


@requires_postgres
def test_it_refuses_to_report_on_a_corpus_that_did_not_ingest(tmp_path, settings):
    # A document that fails ingestion leaves the corpus incomplete, and every question that needed
    # it scores zero. Reporting that as retrieval quality would be a lie about the model.
    settings.TENANTIQ_CHUNK_TARGET_TOKENS = 800
    data = _dataset(
        tmp_path,
        {"empty.txt": "   \n  \n ", "real.txt": "The quick brown fox jumps over the lazy dog."},
        [{"id": "q", "question": "What jumps?", "markers": ["quick brown fox"]}],
    )

    with pytest.raises(EvaluationError, match="did not ingest cleanly"):
        evaluate(dataset=data, top_k=1)


@requires_postgres
def test_out_of_corpus_questions_are_measured_for_separation_not_scored(tmp_path):
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
        out_of_corpus=[{"id": "peru", "question": "What is the capital of Peru?"}],
    )

    report = evaluate(dataset=data, top_k=1)

    assert len(report.results) == 1  # the unanswerable question is not scored...
    assert len(report.out_of_corpus) == 1  # ...but it is measured
    assert report.separation is not None


@requires_postgres
def test_the_shipped_dataset_runs_end_to_end():
    # The real corpus and the real questions, through the real pipeline. No score is asserted: the
    # suite's embedder is lexical, so any threshold here would pin a number that means nothing.
    report = evaluate(top_k=5)

    assert report.document_count == len(load_dataset().documents)
    assert len(report.results) == len(load_dataset().questions)
    assert all(r.total_relevant >= 1 for r in report.results)
    assert report.chunk_count > report.document_count, "corpus should chunk into more than one each"


@requires_postgres
def test_a_hashing_embedder_run_says_loudly_that_it_is_not_a_baseline(tmp_path):
    # The trap this guards: numbers produced by the lexical stand-in embedder look exactly like real
    # ones in a table, and would be recorded in docs/evaluation.md as a baseline by anyone who did
    # not know which embedder was configured.
    data = _dataset(
        tmp_path,
        {"a.txt": "The quick brown fox jumps over the lazy dog."},
        [{"id": "q", "question": "What jumps?", "markers": ["quick brown fox"]}],
    )

    report = evaluate(dataset=data, top_k=1)

    assert report.warnings, "a hashing-embedder run must carry a warning"
    assert "NOT retrieval quality" in report.warnings[0]
    assert "NOT retrieval quality" in as_text(report)
    assert as_dict(report)["warnings"]


@requires_postgres
def test_the_report_records_what_produced_it(tmp_path):
    # A retrieval number without its embedder, dimension and k is a rumour, not a result — and this
    # report is the only place that provenance is ever written down.
    data = _dataset(
        tmp_path,
        {"a.txt": "The quick brown fox jumps over the lazy dog."},
        [{"id": "q", "question": "What jumps?", "markers": ["quick brown fox"]}],
    )

    payload = as_dict(evaluate(dataset=data, top_k=1))

    configuration = payload["configuration"]
    assert configuration["embedder_model"]
    assert configuration["embedding_dim"] > 0
    assert configuration["top_k"] == 1
    assert configuration["chunk_target_tokens"] > 0
    assert payload["corpus"]["chunks"] >= 1


# --- the faithfulness pass (#22) ----------------------------------------------------------------


@requires_postgres
def test_faithfulness_is_off_unless_asked_for(tmp_path):
    # Two model calls per question is minutes, not seconds, so `make eval` must stay a fast retrieval
    # check and the grounding pass must be opt-in.
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    report = evaluate(dataset=data, top_k=1)

    assert report.faithfulness is None
    assert as_dict(report)["faithfulness"] is None


@requires_postgres
def test_it_generates_and_judges_an_answer_per_question(tmp_path):
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    report = evaluate(dataset=data, top_k=1, faithfulness=True)

    grounding = report.faithfulness
    assert grounding is not None
    assert len(grounding.answers) == 1
    # The answer came from the product's own generation path, so it carries a resolvable citation.
    assert grounding.total_claims >= 1
    assert grounding.answers[0].answer


@requires_postgres
def test_a_run_judged_by_the_fake_says_it_is_not_a_measurement(tmp_path):
    # FakeJudge checks lexical containment rather than reading anything. Its numbers look exactly
    # like real ones in a table, which is why the report has to disown them.
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    report = evaluate(dataset=data, top_k=1, faithfulness=True)

    assert any("FakeJudge" in warning for warning in report.warnings)
    assert "not a grounding measurement" in as_text(report)


@requires_postgres
def test_the_report_renders_the_grounding_section(tmp_path):
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    text = as_text(evaluate(dataset=data, top_k=1, faithfulness=True))

    assert "Answer faithfulness" in text
    assert "supported / all claims" in text
    assert "supported / cited claims" in text


class _DivergentLLM:
    """Returns different text on the streaming and non-streaming paths, so a test can tell which
    one the harness actually read."""

    model = "divergent-llm"

    def generate(self, system_prompt, user_prompt):  # noqa: ARG002
        from app.generation import LLMResult

        return LLMResult(answer="STRUCTURED PATH, no marker", citations=(1,))

    def stream(self, system_prompt, user_prompt):  # noqa: ARG002
        yield "STREAMED PATH, with a marker [1]."


class _BrokenLLM:
    model = "broken-llm"

    def generate(self, system_prompt, user_prompt):  # noqa: ARG002
        raise RuntimeError("boom")

    def stream(self, system_prompt, user_prompt):  # noqa: ARG002
        raise RuntimeError("boom")
        yield ""  # pragma: no cover — makes this a generator


@requires_postgres
def test_faithfulness_measures_the_answer_the_product_streams(tmp_path, settings):
    """The bug this pins cost a whole 16-minute run and reported a meaningless 0.05.

    The two generation paths cite differently. The non-streaming path returns structured
    `{answer, citations}` — an *answer-level* citation list, with no `[n]` markers required in the
    prose, so per-claim grounding is not even expressible on it. The streaming path (#48), which is
    what the SSE endpoint and therefore every user receives, cites inline. Measuring prose markers
    against the structured path reported a citation coverage of 0.05 that said nothing about the
    model and everything about the harness reading the wrong output.
    """
    settings.TENANTIQ_LLM_FACTORY = lambda: _DivergentLLM()
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    report = evaluate(dataset=data, top_k=1, faithfulness=True)

    answer = report.faithfulness.answers[0]
    assert "STREAMED PATH" in answer.answer
    assert "STRUCTURED PATH" not in answer.answer
    # And because the streamed prose carries the marker, the claim is citable at all.
    assert answer.cited_claims


@requires_postgres
def test_a_generation_failure_is_a_recorded_gap_not_a_dead_run(tmp_path, settings):
    # `stream_grounded_answer` turns a backend failure into an ErrorEvent rather than raising, so
    # without handling it the harness would score an empty answer as a real, badly-grounded one.
    settings.TENANTIQ_LLM_FACTORY = lambda: _BrokenLLM()
    data = _dataset(
        tmp_path,
        {"a.txt": "Invoices are payable within forty-five days of the invoice date."},
        [{"id": "pay", "question": "When is an invoice due?", "markers": ["forty-five days"]}],
    )

    report = evaluate(dataset=data, top_k=1, faithfulness=True)

    grounding = report.faithfulness
    assert len(grounding.failed) == 1
    assert grounding.measured == ()
    assert grounding.total_claims == 0  # nothing was scored
