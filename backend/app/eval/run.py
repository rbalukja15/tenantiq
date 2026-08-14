"""Evaluation entrypoint: ``python -m app.eval.run`` (#21; faithfulness follows in #22).

Deliberately thin. It configures Django and *then* imports everything else, because the harness
imports models at module scope and those cannot be touched before ``django.setup()`` — importing
them at the top of this file would make the entrypoint fail on import with a message about app
registries rather than about evaluation.

Run it through ``make eval``, which executes inside the compose stack. That is not a convenience:
the numbers are only meaningful against the real embedder, and on the host the configured embedder
is whatever happens to be reachable — usually nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


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
        report = evaluate(top_k=args.k)
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

    if args.fail_under is not None and report.hit_rate(1) < args.fail_under:
        print(
            f"\nFAIL: hit@1 {report.hit_rate(1):.2f} is below the required {args.fail_under:.2f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
