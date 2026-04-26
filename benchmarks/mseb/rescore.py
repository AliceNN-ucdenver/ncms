"""Post-hoc re-score a completed run.

Reads a ``<run_id>.predictions.jsonl`` + a ``queries.jsonl`` (any
variant — newly classified, filtered to a subset, etc.) and recomputes
the full aggregate.  Saves a fresh ``<run_id>.rescore.results.json``
alongside the original.

Intended use: when we add a new taxonomy field (``query_class`` in
this case) after a run, we can re-score without re-running NCMS/mem0.

Usage::

    uv run python -m benchmarks.mseb.rescore \\
        --preds benchmarks/results/mseb/mini/run_abc.predictions.jsonl \\
        --queries benchmarks/mseb_swe/build_mini/queries.jsonl \\
        --out benchmarks/results/mseb/mini/run_abc.rescore.results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Load HF_TOKEN etc. before any ncms/sentence-transformers import
# (SPLADE v3 is gated on HuggingFace and falls back to an
# anonymous fetch otherwise, which 401s).
try:
    from benchmarks.env import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:  # pragma: no cover
    pass

from benchmarks.mseb.metrics import Prediction, aggregate, markdown_summary
from benchmarks.mseb.schema import load_queries

logger = logging.getLogger("mseb.rescore")


def load_predictions(path: Path) -> list[Prediction]:
    out: list[Prediction] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        out.append(
            Prediction(
                qid=row["qid"],
                ranked_mids=row.get("ranked_mids") or [],
                latency_ms=float(row.get("latency_ms") or 0),
                head_outputs=row.get("head_outputs") or {},
                intent_confidence=row.get("intent_confidence"),
            )
        )
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", type=Path, required=True)
    ap.add_argument("--queries", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    preds = load_predictions(args.preds)
    queries = load_queries(args.queries)

    result = aggregate(preds, queries)
    args.out.write_text(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    md = args.out.with_suffix(".md")
    md.write_text(markdown_summary(result, run_id=args.preds.stem))
    logger.info("wrote %s + %s", args.out, md)
    # Short summary to stdout
    print(
        json.dumps(
            {
                "overall": result["overall"],
                "per_class": {
                    k: {"n": v["n"], "r@1": v["r@1"], "r@5": v["r@5"]}
                    for k, v in result.get("per_class", {}).items()
                    if v["n"]
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
