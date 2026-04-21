"""MSEB temporal-weight sweep.

Answers the question "would a higher scoring_weight_temporal
actually lift rank-1 on temporal queries, or is the lexical layer
too dominant?"

Produces a focused build directory containing only the queries
where ``parse_temporal_reference`` fires — these are the queries
temporal scoring is supposed to help.  Then runs NCMS three times
with different temporal weights (0.2 / 0.5 / 0.8), keeping
everything else identical, and reports per-weight rank-1 /
top-5.  If weight=0.8 materially lifts rank-1 we've found a tuning
win; if it's flat the weight is in a saturation regime and the
problem is elsewhere.

Usage::

    # 1. Build the filtered subset
    uv run python -m benchmarks.mseb.temporal_sweep build \\
        --src benchmarks/mseb_clinical/build_mini \\
        --out benchmarks/mseb_clinical/build_mini_temporal

    # 2. Sweep
    uv run python -m benchmarks.mseb.temporal_sweep sweep \\
        --domain mseb_clinical_v2 \\
        --adapter clinical \\
        --build benchmarks/mseb_clinical/build_mini_temporal \\
        --weights 0.2 0.5 0.8
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


# Load HF_TOKEN etc. before any ncms/sentence-transformers import
# (SPLADE v3 is gated on HuggingFace and falls back to an
# anonymous fetch otherwise, which 401s).
try:
    from benchmarks.env import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:  # pragma: no cover
    pass

from benchmarks.mseb.schema import (
    dump_corpus,
    dump_queries,
    load_corpus,
    load_queries,
)

logger = logging.getLogger("mseb.temporal_sweep")


def build_subset(src: Path, out: Path) -> dict[str, int]:
    """Filter a build dir's queries to only temporal-parseable ones."""
    from ncms.domain.temporal.parser import parse_temporal_reference

    corpus = load_corpus(src / "corpus.jsonl")
    queries = load_queries(src / "queries.jsonl")
    now = datetime.now(tz=UTC)

    kept: list = []
    for q in queries:
        ref = parse_temporal_reference(q.text, now=now)
        if ref is None:
            continue
        kept.append(q)

    # Keep only subjects that have at least one surviving query.
    subjects = {q.subject for q in kept}
    corpus = [m for m in corpus if m.subject in subjects]

    out.mkdir(parents=True, exist_ok=True)
    dump_corpus(corpus, out / "corpus.jsonl")
    dump_queries(kept, out / "queries.jsonl")
    stats = {
        "total_queries_in_src": len(queries),
        "temporal_queries_kept": len(kept),
        "subjects_kept": len(subjects),
        "memories_kept": len(corpus),
    }
    (out / "_sweep_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    logger.info("built temporal subset: %s", stats)
    return stats


def run_sweep(
    *, domain: str, adapter: str, build_dir: Path,
    weights: list[float], log_dir: Path, out_dir: Path,
) -> list[dict]:
    """Run ncms/tlg-on with each temporal weight.  Returns result rows."""
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict] = []
    for w in weights:
        cell_log = log_dir / f"sweep-{domain}-tw{w}-{ts}.log"
        logger.info("running temporal_weight=%.2f", w)
        cmd = [
            "uv", "run", "python", "-m", "benchmarks.mseb.harness",
            "--domain", domain,
            "--build-dir", str(build_dir),
            "--out-dir", str(out_dir),
            "--backend", "ncms",
            "--adapter-domain", adapter,
            "--temporal-weight", str(w),
        ]
        with cell_log.open("w") as fh:
            subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=False)

        # Find the results file this run produced (matches domain + ts).
        candidates = sorted(out_dir.glob(f"{domain}_ncms_*.results.json"))
        if not candidates:
            rows.append({"weight": w, "error": "no results file produced"})
            continue
        latest = candidates[-1]
        d = json.loads(latest.read_text())
        rows.append({
            "weight": w,
            "overall": d["overall"],
            "results_file": latest.name,
        })
        logger.info(
            "  overall r@1=%.3f r@5=%.3f mrr=%.3f",
            d["overall"]["r@1"], d["overall"]["r@5"], d["overall"]["mrr"],
        )

    # Pretty-print comparison
    print()
    print(f"{'weight':<8} {'r@1':<8} {'r@5':<8} {'MRR':<8} file")
    print("-" * 70)
    for r in rows:
        if "error" in r:
            print(f"{r['weight']:<8.2f}  ERROR: {r['error']}")
            continue
        o = r["overall"]
        print(f"{r['weight']:<8.2f} {o['r@1']:<8.3f} {o['r@5']:<8.3f} {o['mrr']:<8.3f} {r['results_file']}")
    return rows


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="Temporal-weight sweep")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bap = sub.add_parser("build", help="Build temporal-filtered subset")
    bap.add_argument("--src", type=Path, required=True)
    bap.add_argument("--out", type=Path, required=True)

    sap = sub.add_parser("sweep", help="Run NCMS with 3 temporal weights")
    sap.add_argument("--domain", required=True)
    sap.add_argument("--adapter", required=True)
    sap.add_argument("--build", type=Path, required=True)
    sap.add_argument("--weights", nargs="+", type=float, default=[0.2, 0.5, 0.8])
    sap.add_argument("--log-dir", type=Path,
                     default=Path("benchmarks/mseb/run-logs"))
    sap.add_argument("--out-dir", type=Path,
                     default=Path("benchmarks/results/mseb/temporal_sweep"))

    args = ap.parse_args()
    if args.cmd == "build":
        stats = build_subset(args.src, args.out)
        print(json.dumps(stats, indent=2, sort_keys=True))
    elif args.cmd == "sweep":
        args.log_dir.mkdir(parents=True, exist_ok=True)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        rows = run_sweep(
            domain=args.domain, adapter=args.adapter,
            build_dir=args.build, weights=args.weights,
            log_dir=args.log_dir, out_dir=args.out_dir,
        )
        (args.out_dir / f"sweep-{args.domain}.json").write_text(
            json.dumps(rows, indent=2),
        )


if __name__ == "__main__":
    sys.exit(main())
