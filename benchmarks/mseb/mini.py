"""Build a mini subset of a domain's corpus + queries for smoke testing.

Used before committing to a full ablation run — pick N subjects,
keep only the memories and gold queries tied to those subjects,
emit canonical JSONL into a `build_mini/` directory.

Usage::

    uv run python -m benchmarks.mseb.mini \
        --src benchmarks/mseb_swe/build \
        --out benchmarks/mseb_swe/build_mini \
        --subjects 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from benchmarks.mseb.schema import (
    dump_corpus,
    dump_queries,
    load_corpus,
    load_queries,
)

logger = logging.getLogger("mseb.mini")


def build_mini(*, src: Path, out: Path, n_subjects: int, seed: int = 42) -> dict:
    import random

    corpus = load_corpus(src / "corpus.jsonl")
    queries = load_queries(src / "queries.jsonl")

    # Pick N subjects that have at least one gold query against them.
    subject_queries: dict[str, list] = {}
    for q in queries:
        subject_queries.setdefault(q.subject, []).append(q)
    subjects_with_queries = sorted(s for s in subject_queries if s)

    rng = random.Random(seed)
    picked = rng.sample(
        subjects_with_queries,
        min(n_subjects, len(subjects_with_queries)),
    )
    picked_set = set(picked)

    mini_corpus = [m for m in corpus if m.subject in picked_set]
    mini_queries = [q for q in queries if q.subject in picked_set]
    # Keep noise queries too — they test rejection and don't tie to a subject.
    noise = [q for q in queries if q.shape == "noise"]
    mini_queries.extend(q for q in noise if q not in mini_queries)

    out.mkdir(parents=True, exist_ok=True)
    dump_corpus(mini_corpus, out / "corpus.jsonl")
    dump_queries(mini_queries, out / "queries.jsonl")

    stats = {
        "subjects_picked": len(picked),
        "memories": len(mini_corpus),
        "queries": len(mini_queries),
        "noise_queries": sum(1 for q in mini_queries if q.shape == "noise"),
    }
    (out / "_mini_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    logger.info("mini built: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="MSEB mini-subset builder")
    ap.add_argument(
        "--src",
        type=Path,
        required=True,
        help="Source build dir (contains corpus.jsonl + queries.jsonl)",
    )
    ap.add_argument("--out", type=Path, required=True, help="Output mini build dir")
    ap.add_argument(
        "--subjects", type=int, default=50, help="Number of subjects to sample (default 50)"
    )
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    stats = build_mini(
        src=args.src,
        out=args.out,
        n_subjects=args.subjects,
        seed=args.seed,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
