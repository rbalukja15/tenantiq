"""The retrieval metrics and the dataset's own integrity (#21).

No database here on purpose: these are the parts that decide what every number in the report *means*,
and they are worth pinning against rankings whose right answer is obvious by inspection. The harness
that talks to Postgres is exercised separately in ``test_eval_harness.py``.
"""

from __future__ import annotations

import pytest

from app.eval.dataset import DatasetError, Dataset, Document, Question, load_dataset
from app.eval.report import _verdict_on_configured_floor
from app.eval.metrics import (
    hit_at_k,
    is_relevant,
    mean,
    normalize,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


class TestRelevance:
    def test_a_chunk_containing_the_marker_is_relevant(self):
        assert is_relevant("... payable within forty-five (45) days ...", ["forty-five (45) days"])

    def test_a_chunk_without_it_is_not(self):
        assert not is_relevant("... payable within thirty (30) days ...", ["forty-five (45) days"])

    def test_a_marker_split_across_a_line_break_still_matches(self):
        # The corpus is hard-wrapped and chunk text is stored verbatim (#45), so without whitespace
        # normalisation a perfectly good marker fails purely because a newline landed inside it —
        # and the run reports a retrieval failure that is really a formatting artefact.
        chunk = (
            "Customer shall pay each undisputed invoice within forty-five\n(45) days of the date."
        )

        assert is_relevant(chunk, ["within forty-five (45) days"])

    def test_any_one_marker_is_enough(self):
        assert is_relevant("mentions the second thing", ["first thing", "second thing"])

    def test_no_markers_means_nothing_is_relevant(self):
        assert not is_relevant("anything at all", [])


class TestNormalize:
    def test_collapses_runs_of_whitespace(self):
        assert normalize("a  \n\t b ") == "a b"


class TestPrecision:
    def test_counts_the_relevant_fraction_of_the_top_k(self):
        assert precision_at_k([True, False, True, False], 4) == 0.5

    def test_only_looks_at_the_top_k(self):
        assert precision_at_k([True, False, False, True, True], 2) == 0.5

    def test_divides_by_what_was_returned_not_by_k(self):
        # A corpus smaller than k would otherwise punish a system for returning three results and
        # getting all three right, reporting 0.6 for a perfect ranking.
        assert precision_at_k([True, True, True], 5) == 1.0

    def test_an_empty_ranking_is_zero_rather_than_an_error(self):
        assert precision_at_k([], 5) == 0.0


class TestRecall:
    def test_is_the_share_of_all_relevant_chunks_that_were_found(self):
        assert recall_at_k([True, False, True], total_relevant=4, k=3) == 0.5

    def test_only_looks_at_the_top_k(self):
        assert recall_at_k([False, True], total_relevant=2, k=1) == 0.0

    def test_refuses_to_score_a_question_with_no_relevant_chunk(self):
        # Returning 0.0 (or 1.0) here would fold a broken dataset entry into the score. The caller
        # raises instead, so a marker that matches nothing surfaces as a dataset error.
        with pytest.raises(ValueError, match="undefined"):
            recall_at_k([False], total_relevant=0, k=1)


class TestHitAndRank:
    def test_hit_is_true_when_anything_relevant_is_in_the_window(self):
        assert hit_at_k([False, False, True], 3)
        assert not hit_at_k([False, False, True], 2)

    @pytest.mark.parametrize(
        ("relevance", "expected"),
        [([True, False], 1.0), ([False, True], 0.5), ([False, False, True], 1 / 3), ([False], 0.0)],
    )
    def test_reciprocal_rank_rewards_finding_it_early(self, relevance, expected):
        # Rank is not cosmetic: sources are numbered into the prompt in retrieval order, so a
        # relevant chunk at position 5 competes with four irrelevant ones for the model's attention.
        assert reciprocal_rank(relevance) == pytest.approx(expected)

    def test_mean_of_nothing_is_zero_rather_than_an_error(self):
        assert mean([]) == 0.0


class TestTheShippedDataset:
    """The dataset is data, and data rots. These run in CI, without a database."""

    def test_it_loads_and_validates(self):
        data = load_dataset()

        assert len(data.documents) >= 5
        assert len(data.questions) >= 10
        assert data.out_of_corpus, "separation cannot be measured without unanswerable questions"

    def test_every_marker_appears_in_exactly_one_document(self):
        # `load_dataset` enforces this; asserting it here names the property so the reason survives.
        # A marker matching nothing makes retrieval look broken; a marker matching two documents
        # silently inflates the relevant set with passages the question was never about.
        load_dataset()

    def test_each_question_names_the_document_it_expects(self):
        for question in load_dataset().questions:
            assert question.expected_document, f"{question.id} has no expected_document"

    def test_out_of_corpus_questions_carry_no_markers(self):
        for question in load_dataset().out_of_corpus:
            assert question.markers == ()


class TestValidation:
    """The failures that would otherwise produce a plausible-looking wrong number."""

    def _dataset(self, marker: str) -> Dataset:
        return Dataset(
            documents=(Document(name="a.txt", text="the quick brown fox"),),
            questions=(
                Question(id="q", question="?", markers=(marker,), expected_document="a.txt"),
            ),
            out_of_corpus=(),
        )

    def test_a_marker_matching_nothing_is_rejected(self, tmp_path):
        _write(
            tmp_path,
            {"a.txt": "the quick brown fox"},
            [{"id": "q", "question": "?", "markers": ["a phrase that is absent"]}],
        )

        with pytest.raises(DatasetError, match="not found in any corpus document"):
            load_dataset(tmp_path)

    def test_a_marker_matching_two_documents_is_rejected(self, tmp_path):
        _write(
            tmp_path,
            {"a.txt": "shared phrase here", "b.txt": "also shared phrase here"},
            [{"id": "q", "question": "?", "markers": ["shared phrase"]}],
        )

        with pytest.raises(DatasetError, match="cannot identify one passage"):
            load_dataset(tmp_path)

    def test_a_question_with_no_markers_is_rejected(self, tmp_path):
        _write(tmp_path, {"a.txt": "text"}, [{"id": "q", "question": "?", "markers": []}])

        with pytest.raises(DatasetError, match="at least one marker"):
            load_dataset(tmp_path)

    def test_duplicate_question_ids_are_rejected(self, tmp_path):
        _write(
            tmp_path,
            {"a.txt": "alpha beta"},
            [
                {"id": "same", "question": "?", "markers": ["alpha"]},
                {"id": "same", "question": "?", "markers": ["beta"]},
            ],
        )

        with pytest.raises(DatasetError, match="Duplicate question id"):
            load_dataset(tmp_path)

    def test_an_empty_corpus_is_rejected(self, tmp_path):
        _write(tmp_path, {}, [])

        with pytest.raises(DatasetError, match="No corpus documents"):
            load_dataset(tmp_path)


def _write(base, documents: dict[str, str], questions: list[dict]) -> None:
    """Lay out a miniature dataset on disk, in the same shape as the shipped one."""
    import json

    corpus = base / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        (corpus / name).write_text(text, encoding="utf-8")
    for question in questions:
        question.setdefault("expected_document", next(iter(documents), ""))
    (base / "questions.json").write_text(
        json.dumps({"version": 1, "corpus_dir": "corpus", "questions": questions}),
        encoding="utf-8",
    )


class TestFloorVerdict:
    """The report's advice about TENANTIQ_RETRIEVAL_MIN_SIMILARITY.

    An earlier version asserted a single outcome ("the default is X, which refuses nothing") and was
    simply wrong the first time a real run had a floor configured — it read the environment's value
    and called it the default, then made a claim about it that the numbers contradicted. Three
    explicit cases, each pinned.
    """

    def test_a_floor_below_the_noise_refuses_nothing(self):
        verdict = _verdict_on_configured_floor(0.0, low=0.558, high=0.403)

        assert "refuses nothing" in verdict

    def test_a_floor_above_the_worst_answerable_question_is_too_high(self):
        verdict = _verdict_on_configured_floor(0.9, low=0.558, high=0.403)

        assert "refuses questions this corpus can actually answer" in verdict

    def test_a_floor_inside_the_window_separates_exactly(self):
        verdict = _verdict_on_configured_floor(0.5, low=0.558, high=0.403)

        assert "sits inside that window" in verdict
