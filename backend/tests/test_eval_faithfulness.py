"""Claim splitting, the judge contract, and grounding scores (#22).

No database and no network: the parts that decide what a faithfulness number *means* are pure, and
the interesting cases are ones a real judge would only produce by luck — a model that skips a claim,
answers outside the enum, or cites a source that was never offered.

The one thing deliberately not asserted anywhere here is a *score against a real model*. An
LLM-as-judge figure is a measurement with error, and pinning one in CI would turn model drift into a
red build for a reason that has nothing to do with the code.
"""

from __future__ import annotations

import pytest

from app.eval.claims import split_claims
from app.eval.harness import AnswerResult, FaithfulnessReport, Report
from app.eval.report import as_dict, as_text
from app.eval.judge import FakeJudge, JudgedSource, Verdict, build_prompt, coerce_verdicts
from app.eval.dataset import Question


def _question(id: str = "q") -> Question:
    return Question(id=id, question="?", markers=("m",), expected_document="a.txt")


class TestSplitClaims:
    def test_splits_on_sentence_boundaries(self):
        claims = split_claims("Invoices are due in 45 days. [1] A discount applies. [2]", 2)

        assert len(claims) == 2

    def test_attaches_the_citations_each_claim_carries(self):
        claims = split_claims("Payment is due in 45 days [1][2].", 2)

        assert claims[0].cited == (1, 2)
        assert claims[0].is_cited

    def test_deduplicates_repeated_markers(self):
        claims = split_claims("As [1] says, and again [1], the term is 24 months.", 1)

        assert claims[0].cited == (1,)

    def test_flags_a_citation_that_was_never_a_source(self):
        # generation._resolve_citations drops [7] from the resolved citation list, so the API never
        # returns a dangling citation — but the prose keeps the marker, and the answer still reads
        # "as set out in [7]". That is the failure the project claims is impossible.
        claims = split_claims("The cap is three times fees [7].", available_sources=3)

        assert claims[0].invented == (7,)
        assert not claims[0].cites_only_real_sources

    def test_a_valid_citation_is_not_flagged(self):
        claims = split_claims("The cap is three times fees [3].", available_sources=3)

        assert claims[0].invented == ()

    def test_zero_is_an_invented_source_number(self):
        # Sources are numbered from 1. A `[0]` marker resolves to nothing and must not be treated as
        # a real citation just because it parses as an integer.
        claims = split_claims("Something something [0] here.", available_sources=3)

        assert claims[0].invented == (0,)

    def test_notices_a_claim_that_states_a_figure(self):
        claims = split_claims("Payment is due within 45 days [1].", 1)

        assert claims[0].has_number

    def test_a_citation_marker_is_not_a_numeric_claim(self):
        # The trap: `[1]` contains a digit. Counting it would make every cited sentence look like a
        # numeric claim and make the most important metric in the report meaningless.
        claims = split_claims("The parties agreed to the amendment [1].", 1)

        assert not claims[0].has_number

    def test_ignores_fragments_too_short_to_be_claims(self):
        claims = split_claims("Yes. Payment is due within forty-five days [1].", 1)

        assert len(claims) == 1

    def test_an_empty_answer_has_no_claims(self):
        assert split_claims("", 3) == ()


class TestCoerceVerdicts:
    def test_maps_the_enum_onto_a_tristate(self):
        raw = {
            "verdicts": [
                {"index": 0, "verdict": "supported", "reason": "a"},
                {"index": 1, "verdict": "unsupported", "reason": "b"},
                {"index": 2, "verdict": "unclear", "reason": "c"},
            ]
        }

        verdicts = coerce_verdicts(raw, [0, 1, 2])

        assert [v.supported for v in verdicts] == [True, False, None]

    def test_a_skipped_claim_becomes_unclear_rather_than_either_answer(self):
        # The load-bearing one. Defaulting a missing verdict to supported or unsupported would let a
        # flaky judge move the headline score in whichever direction its failures happened to fall.
        verdicts = coerce_verdicts({"verdicts": [{"index": 0, "verdict": "supported"}]}, [0, 1])

        assert verdicts[1].is_unclear
        assert "no verdict" in verdicts[1].reason

    def test_a_word_outside_the_enum_is_unclear(self):
        verdicts = coerce_verdicts(
            {"verdicts": [{"index": 0, "verdict": "probably", "reason": ""}]}, [0]
        )

        assert verdicts[0].is_unclear

    def test_an_index_the_judge_invented_is_discarded(self):
        verdicts = coerce_verdicts({"verdicts": [{"index": 99, "verdict": "supported"}]}, [0])

        assert len(verdicts) == 1
        assert verdicts[0].is_unclear

    @pytest.mark.parametrize("raw", [None, {}, {"verdicts": "nope"}, {"verdicts": [None]}, []])
    def test_malformed_payloads_never_raise(self, raw):
        # A judge is a remote model; it will eventually return something absurd. Losing the whole run
        # to a parse error would be worse than recording that it could not be read.
        verdicts = coerce_verdicts(raw, [0])

        assert verdicts[0].is_unclear

    def test_one_verdict_per_expected_claim_in_order(self):
        raw = {
            "verdicts": [{"index": 1, "verdict": "supported"}, {"index": 0, "verdict": "unclear"}]
        }

        verdicts = coerce_verdicts(raw, [0, 1])

        assert [v.index for v in verdicts] == [0, 1]


class TestJudgePrompt:
    SOURCES = (
        JudgedSource(number=1, text="Payment is due within forty-five days."),
        JudgedSource(number=2, text="Hotel nights in London are capped at GBP 200."),
        JudgedSource(number=3, text="The initial term is twenty-four months."),
    )

    def test_shows_only_the_sources_the_claims_cite(self):
        # The judge must not be able to credit a claim to evidence the answer never pointed at. If
        # every source were included, a claim citing [1] could be marked supported because the text
        # happens to appear in [3] — which is precisely the mistake being measured.
        prompt = build_prompt([(0, "Payment is due in 45 days.", (1,))], self.SOURCES)

        assert "forty-five days" in prompt
        assert "London" not in prompt
        assert "twenty-four months" not in prompt

    def test_quotes_the_source_text_verbatim(self):
        prompt = build_prompt([(0, "x", (2,))], self.SOURCES)

        assert "Hotel nights in London are capped at GBP 200." in prompt

    def test_states_what_each_claim_cites(self):
        prompt = build_prompt([(0, "Payment is due.", (1, 3))], self.SOURCES)

        assert "CLAIM 0 (cites [1], [3])" in prompt

    def test_survives_a_claim_that_cites_nothing(self):
        prompt = build_prompt([(0, "Payment is due.", ())], self.SOURCES)

        assert "no sources cited" in prompt


class TestFakeJudge:
    def test_supports_a_claim_whose_words_are_in_the_cited_source(self):
        verdicts = FakeJudge().judge(
            [(0, "payment forty-five days", (1,))],
            [JudgedSource(number=1, text="Payment is due within forty-five days.")],
        )

        assert verdicts[0].is_supported

    def test_rejects_a_claim_whose_words_are_absent(self):
        verdicts = FakeJudge().judge(
            [(0, "payment ninety days", (1,))],
            [JudgedSource(number=1, text="Payment is due within forty-five days.")],
        )

        assert verdicts[0].is_unsupported


class TestScores:
    """The aggregation, on hand-built verdicts where the arithmetic is checkable by eye."""

    def _answer(self, text: str, sources: int, verdicts: list[bool | None]) -> AnswerResult:
        claims = split_claims(text, sources)
        cited = [claim for claim in claims if claim.is_cited]
        return AnswerResult(
            question=_question(),
            answer=text,
            claims=claims,
            verdicts=tuple(
                Verdict(index=claim.index, supported=supported, reason="")
                for claim, supported in zip(cited, verdicts, strict=True)
            ),
            refused=False,
        )

    def test_faithfulness_is_over_cited_claims_and_grounded_is_over_all(self):
        # One cited-and-supported claim, one uncited. Faithfulness sees only the cited one and reports
        # a perfect score; grounded counts the uncited claim against the answer. That difference is
        # the whole reason both are reported.
        report = FaithfulnessReport(
            judge_model="j",
            generator_model="g",
            answers=(
                self._answer(
                    "Payment is due within forty-five days [1]. The weather is pleasant today.",
                    1,
                    [True],
                ),
            ),
        )

        assert report.total_claims == 2
        assert report.total_cited_claims == 1
        assert report.faithfulness == 1.0
        assert report.grounded == 0.5
        assert report.citation_coverage == 0.5

    def test_an_unclear_verdict_counts_as_neither_supported_nor_unsupported(self):
        report = FaithfulnessReport(
            judge_model="j",
            generator_model="g",
            answers=(self._answer("Payment is due in forty-five days [1].", 1, [None]),),
        )

        assert report.unclear_claims == 1
        assert report.unsupported_claims == 0
        assert report.grounded == 0.0  # not credited

    def test_counts_uncited_numeric_claims(self):
        report = FaithfulnessReport(
            judge_model="j",
            generator_model="g",
            answers=(
                self._answer(
                    "The term is 24 months and renews. Payment is due in forty-five days [1].",
                    1,
                    [True],
                ),
            ),
        )

        assert report.uncited_numeric_claims == 1

    def test_counts_invented_citations(self):
        report = FaithfulnessReport(
            judge_model="j",
            generator_model="g",
            answers=(self._answer("The liability cap is three times fees [9].", 3, [True]),),
        )

        assert report.invented_citations == 1

    def test_an_empty_answer_set_scores_zero_rather_than_dividing_by_zero(self):
        report = FaithfulnessReport(judge_model="j", generator_model="g", answers=())

        assert report.grounded == 0.0
        assert report.faithfulness == 0.0
        assert report.citation_coverage == 0.0

    def test_notices_when_the_judge_is_the_model_that_wrote_the_answer(self):
        # Self-assessment is known-optimistic, and the report has to say so out loud.
        same = FaithfulnessReport(judge_model="llama3.1", generator_model="llama3.1", answers=())
        different = FaithfulnessReport(judge_model="claude", generator_model="llama3.1", answers=())

        assert same.judge_is_the_generator
        assert not different.judge_is_the_generator


class TestResilience:
    """One slow model call must not throw away the whole run.

    The first version of the harness let a single `TimeoutError` propagate out of the judging loop.
    Eighteen questions at two model calls each is a long enough window that something eventually
    times out — and it did, on the very first real run, discarding every answer already collected.
    """

    def _failed(self, id: str) -> AnswerResult:
        return AnswerResult(
            question=_question(id),
            answer="",
            claims=(),
            verdicts=(),
            refused=False,
            error="TimeoutError: timed out",
        )

    def _good(self) -> AnswerResult:
        claims = split_claims("Payment is due within forty-five days [1].", 1)
        return AnswerResult(
            question=_question("good"),
            answer="Payment is due within forty-five days [1].",
            claims=claims,
            verdicts=(Verdict(index=0, supported=True, reason=""),),
            refused=False,
        )

    def test_a_failed_question_is_separated_from_the_measured_ones(self):
        report = FaithfulnessReport(
            judge_model="j", generator_model="g", answers=(self._good(), self._failed("dead"))
        )

        assert len(report.measured) == 1
        assert len(report.failed) == 1
        assert report.failed[0].question.id == "dead"

    def test_a_failure_does_not_drag_the_score_down(self):
        # The whole point. A timeout is a gap in coverage, not an ungrounded answer — scoring it as
        # one would report a grounding problem the model never had.
        with_failure = FaithfulnessReport(
            judge_model="j", generator_model="g", answers=(self._good(), self._failed("dead"))
        )
        without = FaithfulnessReport(judge_model="j", generator_model="g", answers=(self._good(),))

        assert with_failure.grounded == without.grounded == 1.0
        assert with_failure.total_claims == without.total_claims

    def test_a_run_where_everything_failed_scores_zero_rather_than_dividing_by_zero(self):
        report = FaithfulnessReport(
            judge_model="j", generator_model="g", answers=(self._failed("a"), self._failed("b"))
        )

        assert report.measured == ()
        assert report.grounded == 0.0
        assert report.total_claims == 0


class TestReportRendering:
    """The report has to be readable *and* honest when things went wrong."""

    def _report(self, answers) -> Report:
        return Report(
            embedder_model="m",
            embedder_class="OllamaEmbedder",
            embedding_dim=768,
            top_k=5,
            min_similarity=0.0,
            chunk_target_tokens=800,
            chunk_overlap_tokens=100,
            document_count=1,
            chunk_count=1,
            corpus_chars=100,
            results=(),
            out_of_corpus=(),
            faithfulness=FaithfulnessReport(
                judge_model="claude", generator_model="llama3.1", answers=answers
            ),
        )

    def _good(self) -> AnswerResult:
        claims = split_claims("Payment is due within forty-five days [1].", 1)
        return AnswerResult(
            question=_question("good"),
            answer="Payment is due within forty-five days [1].",
            claims=claims,
            verdicts=(Verdict(index=0, supported=True, reason=""),),
            refused=False,
        )

    def test_says_how_many_answers_were_actually_measured(self):
        failed = AnswerResult(
            question=_question("dead"),
            answer="",
            claims=(),
            verdicts=(),
            refused=False,
            error="TimeoutError: timed out",
        )

        text = as_text(self._report((self._good(), failed)))

        assert "answers measured        1 of 2" in text
        assert "produced no measurement" in text
        assert "dead: TimeoutError: timed out" in text

    def test_names_the_contract_violations_it_found(self):
        # A count alone is unactionable — the point is to be able to go and look at the sentence.
        claims = split_claims("The cap is 3x fees [9]. The term runs 24 months.", 1)
        answer = AnswerResult(
            question=_question("viol"),
            answer="The cap is 3x fees [9]. The term runs 24 months.",
            claims=claims,
            verdicts=(Verdict(index=0, supported=False, reason="not in source"),),
            refused=False,
        )

        text = as_text(self._report((answer,)))

        assert "Contract violations" in text
        assert "cites [9], which was never a source" in text
        assert "uncited figure" in text

    def test_a_clean_run_has_no_violations_section(self):
        text = as_text(self._report((self._good(),)))

        assert "Contract violations" not in text

    def test_the_json_carries_the_judge_s_reasons_for_a_rejection(self):
        # A faithfulness score nobody can audit is just a number.
        claims = split_claims("Payment is due within ninety days [1].", 1)
        answer = AnswerResult(
            question=_question("wrong"),
            answer="Payment is due within ninety days [1].",
            claims=claims,
            verdicts=(Verdict(index=0, supported=False, reason="source says forty-five"),),
            refused=False,
        )

        payload = as_dict(self._report((answer,)))["faithfulness"]

        assert payload["answers"][0]["unsupported_reasons"][0]["reason"] == "source says forty-five"
