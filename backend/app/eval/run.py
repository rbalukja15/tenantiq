"""Evaluation entrypoint: ``python -m app.eval.run`` (#21; faithfulness follows in #22).

Deliberately thin. It configures Django and *then* imports everything else, because the harness
imports models at module scope and those cannot be touched before ``django.setup()`` — importing
them at the top of this file would make the entrypoint fail on import with a message about app
registries rather than about evaluation.

Run it through ``make eval``, which executes inside the compose stack. That is not a convenience:
the numbers are only meaningful against the real embedder, and on the host the configured embedder
is whatever happens to be reachable — usually nothing.

Exit status: 0 for a measurement that passed, 1 for one that failed (hit@1 under ``--fail-under``,
or a similarity gap that no floor can sit in, #88), and 2 when there was nothing to measure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.eval.harness import Report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval.run",
        description="Measure retrieval quality against the curated evaluation dataset.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=None,
        help="how many chunks to retrieve (default: TENANTIQ_RETRIEVAL_TOP_K)",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        help="also write the full report as JSON to this path, for diffing two runs",
    )
    parser.add_argument(
        "--faithfulness",
        action="store_true",
        help=(
            "also generate an answer per question and have an LLM judge rule on every claim in it "
            "(#22). Two model calls per question, so minutes rather than seconds"
        ),
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        metavar="HIT@1",
        help="exit non-zero if mean hit@1 falls below this, so a regression can gate a pipeline",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()

    # Imported here, after setup, for the reason in the module docstring.
    from app.eval.dataset import DatasetError
    from app.eval.harness import EvaluationError, evaluate
    from app.eval.report import as_dict, as_text

    try:
        report = evaluate(top_k=args.k, faithfulness=args.faithfulness)
    except (DatasetError, EvaluationError) as exc:
        # A broken dataset or a corpus that would not ingest is not a bad score — it is the absence
        # of a measurement, and printing a table of zeros would be the worst possible response.
        print(f"evaluation aborted: {exc}", file=sys.stderr)
        return 2

    print(as_text(report))

    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(as_dict(report), indent=2) + "\n", encoding="utf-8")
        print(f"\n  wrote {args.json_path}")

    failures = _failures(report, fail_under=args.fail_under)
    for failure in failures:
        print(f"\nFAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _failures(report: Report, *, fail_under: float | None) -> list[str]:
    """Why this run should exit non-zero, if it should.

    A separation gap that is not positive always fails the run (#88): it means no value of
    ``TENANTIQ_RETRIEVAL_MIN_SIMILARITY`` refuses every question the corpus cannot answer while
    answering every one it can, so the product's only way of declining cannot be configured to work.
    As one more number in a table, that is easy to scroll past.
    """
    failures = []
    if fail_under is not None and report.hit_rate(1) < fail_under:
        failures.append(f"hit@1 {report.hit_rate(1):.2f} is below the required {fail_under:.2f}")
    gap = report.separation
    if gap is not None and gap <= 0:
        failures.append(
            f"the similarity gap is {gap:+.3f}, so no TENANTIQ_RETRIEVAL_MIN_SIMILARITY separates "
            "the questions this corpus can answer from the ones it cannot (see the report's "
            "'Similarity separation')"
        )
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
