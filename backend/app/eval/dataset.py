"""Loading and validating the evaluation dataset (#21).

The dataset is data, not code: a directory of plain-text documents plus one JSON file of questions
and their marker phrases. Growing it is a data change, which is the point — the harness should never
need editing to measure more.

Validation is strict and runs before anything is embedded, because every failure mode here produces
a *plausible-looking wrong number* rather than an error. A marker with a typo simply never matches,
and the run reports a retrieval failure that is really a dataset failure; the two are
indistinguishable in the output, so they are separated here instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.eval.metrics import normalize

DATASET_DIR = Path(__file__).resolve().parent / "dataset"


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    markers: tuple[str, ...]
    expected_document: str
    note: str = ""


@dataclass(frozen=True)
class Document:
    name: str
    text: str


@dataclass(frozen=True)
class Dataset:
    documents: tuple[Document, ...]
    questions: tuple[Question, ...]
    out_of_corpus: tuple[Question, ...]

    @property
    def total_chars(self) -> int:
        return sum(len(document.text) for document in self.documents)


class DatasetError(RuntimeError):
    """The dataset is internally inconsistent — a broken measurement, not a bad score."""


def load_dataset(directory: Path | None = None) -> Dataset:
    """Load, validate, and return the dataset. Raises :class:`DatasetError` on any inconsistency."""
    base = directory or DATASET_DIR
    spec = json.loads((base / "questions.json").read_text(encoding="utf-8"))
    corpus_dir = base / spec.get("corpus_dir", "corpus")

    documents = tuple(
        Document(name=path.name, text=path.read_text(encoding="utf-8"))
        for path in sorted(corpus_dir.glob("*.txt"))
    )
    if not documents:
        raise DatasetError(f"No corpus documents found in {corpus_dir}")

    questions = tuple(
        Question(
            id=entry["id"],
            question=entry["question"],
            markers=tuple(entry["markers"]),
            expected_document=entry.get("expected_document", ""),
            note=entry.get("note", ""),
        )
        for entry in spec["questions"]
    )
    out_of_corpus = tuple(
        Question(id=entry["id"], question=entry["question"], markers=(), expected_document="")
        for entry in spec.get("out_of_corpus", ())
    )

    _validate(documents, questions, out_of_corpus)
    return Dataset(documents=documents, questions=questions, out_of_corpus=out_of_corpus)


def _validate(
    documents: tuple[Document, ...],
    questions: tuple[Question, ...],
    out_of_corpus: tuple[Question, ...],
) -> None:
    ids = [question.id for question in (*questions, *out_of_corpus)]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    if duplicates:
        raise DatasetError(f"Duplicate question id(s): {', '.join(duplicates)}")

    haystacks = {document.name: normalize(document.text) for document in documents}
    for question in questions:
        if not question.markers:
            raise DatasetError(f"{question.id}: an in-corpus question needs at least one marker")
        for marker in question.markers:
            hits = [name for name, text in haystacks.items() if normalize(marker) in text]
            if not hits:
                # The failure that would otherwise be invisible: a marker nothing can ever match
                # makes every retrieval look wrong, and the score would report it as a model
                # problem.
                raise DatasetError(
                    f"{question.id}: marker not found in any corpus document: {marker!r}"
                )
            if len(hits) > 1:
                # An ambiguous marker silently inflates the relevant set with chunks the question
                # was never about, which raises recall and lowers precision for reasons that have
                # nothing to do with retrieval.
                raise DatasetError(
                    f"{question.id}: marker appears in {len(hits)} documents "
                    f"({', '.join(sorted(hits))}), so it cannot identify one passage: {marker!r}"
                )

    for question in out_of_corpus:
        if question.markers:
            raise DatasetError(
                f"{question.id}: an out-of-corpus question must carry no markers — "
                "it is scored on similarity separation, not on relevance"
            )
