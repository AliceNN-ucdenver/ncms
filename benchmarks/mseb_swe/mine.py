"""MSEB-SWE miner — SWE-bench Verified → message tuples.

Phase 1 of the MSEB-SWE pipeline.  Loads the public HuggingFace
dataset ``princeton-nlp/SWE-bench_Verified`` (500 verified issues
with resolving PRs across 12 mature Python projects) and emits
one **raw message JSONL** per issue.

Output format (one JSONL per issue under ``raw/<instance_id>.jsonl``)::

    {"message_id": "problem_statement",   "text": "...",
     "timestamp": "2023-07-15T14:32:00Z", "source": "issue_body"}
    {"message_id": "hints_text",           "text": "...",
     "timestamp": "2023-07-15T14:32:00Z", "source": "pr_discussion"}
    …

The raw-messages layer is intentionally **un-labeled** — Phase 2
(``label.py``) applies the MemoryKind classifier.  This split
keeps mining cheap and cacheable (hit HuggingFace once, label
many times while tuning the prompt).

See ``benchmarks/mseb_swe/README.md`` §3-4 for the fetch
pipeline, search-pattern catalogue, and reproducibility policy.

Usage::

    # pilot — first 50 verified issues
    uv run python -m benchmarks.mseb_swe.mine --limit 50

    # full scale
    uv run python -m benchmarks.mseb_swe.mine --limit 500
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Load HF_TOKEN before datasets import — HF_DATASETS_OFFLINE=1
# deployments also honour this.
try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover — allow running outside repo
    pass

logger = logging.getLogger("mseb_swe.mine")

# Alternative datasets with identical schema — used for training the
# swe_diff adapter on a DISJOINT corpus from the MSEB-SWE benchmark
# (SWE-bench Verified).  See experiments/intent_slot_distillation/
# adapters/swe_diff/DATASHEET.md for the train/test leakage rationale.
DATASETS: dict[str, str] = {
    "swe_bench_verified": "princeton-nlp/SWE-bench_Verified",
    "swe_gym":            "SWE-Gym/SWE-Gym",
    "swe_gym_lite":       "SWE-Gym/SWE-Gym-Lite",
}
DEFAULT_DATASET_KEY = "swe_bench_verified"
DATASET = DATASETS[DEFAULT_DATASET_KEY]  # legacy alias
DEFAULT_OUT = Path(__file__).parent / "raw"


def _iso(ts: str | None) -> str:
    """Normalise SWE-bench timestamps to ISO-8601 UTC.

    SWE-bench stores created_at as ISO already; we re-parse to
    guarantee timezone-awareness downstream.
    """
    if not ts:
        # Fallback: pin to Unix epoch.  The labeler will flag rows
        # with epoch timestamps and the harness filters them from
        # ordinal / temporal queries.
        return "1970-01-01T00:00:00Z"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except ValueError:
        logger.debug("unparseable timestamp %r → epoch fallback", ts)
        return "1970-01-01T00:00:00Z"


def _row_to_messages(row: dict) -> list[dict]:
    """Explode one SWE-bench Verified row into message tuples.

    The public dataset carries the issue body (``problem_statement``),
    optional PR hints / discussion (``hints_text``), the resolving
    patch (``patch``) and test patch (``test_patch``).  We surface
    each as its own message so the downstream labeler can classify
    each independently.

    Timestamp policy: ``created_at`` is the issue creation time
    (when present); the patch & test_patch inherit that timestamp
    unless a more specific one is available.
    """
    iid = row["instance_id"]
    repo = row.get("repo", "")
    created = _iso(row.get("created_at"))
    base_sha = row.get("base_commit", "")[:12]

    messages: list[dict] = []

    problem = (row.get("problem_statement") or "").strip()
    if problem:
        messages.append({
            "message_id": f"{iid}::problem_statement",
            "text": problem,
            "timestamp": created,
            "source": "issue_body",
            "repo": repo,
            "base_commit": base_sha,
        })

    hints = (row.get("hints_text") or "").strip()
    if hints:
        messages.append({
            "message_id": f"{iid}::hints",
            "text": hints,
            "timestamp": created,
            "source": "pr_discussion",
            "repo": repo,
            "base_commit": base_sha,
        })

    # Patches are structural change records — treat each as its own
    # message.  The labeler often classifies these as `retirement`
    # (old behaviour replaced) or `causal_link` (patch cause → fix).
    patch = (row.get("patch") or "").strip()
    if patch:
        messages.append({
            "message_id": f"{iid}::patch",
            "text": patch[:4000],   # cap ingest-sized; full patch in cache
            "timestamp": created,
            "source": "resolving_patch",
            "repo": repo,
            "base_commit": base_sha,
            "patch_full_chars": len(patch),
        })

    test_patch = (row.get("test_patch") or "").strip()
    if test_patch:
        messages.append({
            "message_id": f"{iid}::test_patch",
            "text": test_patch[:4000],
            "timestamp": created,
            "source": "test_patch",
            "repo": repo,
            "base_commit": base_sha,
        })

    return messages


def mine(
    *,
    limit: int,
    out_dir: Path,
    split: str = "test",
    hf_cache_dir: Path | None = None,
    dataset: str = DATASET,
    shuffle_seed: int | None = None,
) -> dict:
    """Fetch a SWE-bench-compatible dataset and emit raw messages.

    ``dataset`` defaults to SWE-bench Verified.  Pass
    ``SWE-Gym/SWE-Gym`` to mine the disjoint training-only corpus.
    ``shuffle_seed`` deterministically shuffles rows before slicing
    to ``limit`` — avoids alphabetical project-clumping in partial mines.
    """
    from datasets import load_dataset

    out_dir.mkdir(parents=True, exist_ok=True)
    cache = str(hf_cache_dir) if hf_cache_dir else None

    logger.info("Loading %s [split=%s] …", dataset, split)
    ds = load_dataset(dataset, split=split, cache_dir=cache)
    logger.info("Loaded %d rows", len(ds))

    if shuffle_seed is not None:
        ds = ds.shuffle(seed=shuffle_seed)
        logger.info("shuffled with seed=%d for representative sampling", shuffle_seed)

    stats = {
        "issues": 0,
        "messages": 0,
        "per_source": {},
        "per_repo": {},
    }

    for i, row in enumerate(ds):
        if i >= limit:
            break

        messages = _row_to_messages(row)
        if not messages:
            logger.warning(
                "row %d (instance=%s) produced 0 messages — skipping",
                i, row.get("instance_id"),
            )
            continue

        iid = row["instance_id"]
        out_path = out_dir / f"{iid}.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(json.dumps(msg, ensure_ascii=False))
                fh.write("\n")

        stats["issues"] += 1
        stats["messages"] += len(messages)
        for msg in messages:
            stats["per_source"].setdefault(msg["source"], 0)
            stats["per_source"][msg["source"]] += 1
        repo = row.get("repo", "unknown")
        stats["per_repo"].setdefault(repo, 0)
        stats["per_repo"][repo] += 1

        if (i + 1) % 10 == 0 or (i + 1) == limit:
            logger.info(
                "[%d/%d] issue=%s messages=%d",
                i + 1, limit, iid, len(messages),
            )

    (out_dir / "_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="MSEB-SWE miner: SWE-bench Verified → raw messages",
    )
    parser.add_argument("--limit", type=int, default=50,
                        help="Max issues to mine (default 50 for pilot)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--split", default=None,
                        help="HF dataset split.  Default 'test' for "
                             "SWE-bench Verified, 'train' for SWE-Gym.")
    parser.add_argument("--hf-cache-dir", type=Path, default=None)
    parser.add_argument(
        "--dataset", default=DEFAULT_DATASET_KEY,
        choices=list(DATASETS) + list(DATASETS.values()),
        help="Which upstream dataset to pull from (default: SWE-bench Verified).",
    )
    parser.add_argument(
        "--shuffle-seed", type=int, default=None,
        help="Shuffle dataset rows before slicing to --limit.  Recommended "
             "for SWE-Gym training mines (alphabetical ordering clumps "
             "rows by repo).  Default None (preserve upstream order).",
    )
    args = parser.parse_args()

    # Accept either friendly keys or full HF names.
    dataset_name = DATASETS.get(args.dataset, args.dataset)
    # Default split depends on the dataset — SWE-bench uses 'test' for the
    # verified split; SWE-Gym ships as 'train' only.
    split = args.split or (
        "test" if dataset_name.endswith("SWE-bench_Verified") else "train"
    )

    stats = mine(
        limit=args.limit,
        out_dir=args.out_dir,
        split=split,
        hf_cache_dir=args.hf_cache_dir,
        dataset=dataset_name,
        shuffle_seed=args.shuffle_seed,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
