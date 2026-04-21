"""MSEB-SWE labeler — raw message tuples → CorpusMemory JSONL.

Phase 2 of the MSEB-SWE pipeline.  Reads
``raw/<instance_id>.jsonl`` (output of ``mine.py``) and emits
``raw_labeled/<instance_id>.jsonl`` with ``mid`` + metadata.kind
filled.

The SWE corpus is easy to label deterministically: the ``source``
field already carries the MemoryKind signal.

| Source | MemoryKind | Rationale |
| --- | --- | --- |
| ``issue_body`` | ``declaration`` | bug/state reported for the first time |
| ``pr_discussion`` | ``causal_link`` | discussion of why / how (hints_text) |
| ``resolving_patch`` | ``retirement`` | old behaviour replaced by new |
| ``test_patch`` | ``declaration`` | new invariant declared + enforced |

The ``ordinal_anchor`` label is emitted on the first message of
each subject chain (the issue body is the origin of every issue).

An optional LLM post-pass is stubbed (see ``_llm_refine``) —
disabled by default; the rule-based labels are already ≥95 %
precise for this domain per the pilot inspection.

Usage::

    uv run python -m benchmarks.mseb_swe.label \\
        --raw-dir benchmarks/mseb_swe/raw \\
        --out-dir benchmarks/mseb_swe/raw_labeled
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("mseb_swe.label")

DEFAULT_RAW = Path(__file__).parent / "raw"
DEFAULT_OUT = Path(__file__).parent / "raw_labeled"


# Deterministic source → MemoryKind map.
SOURCE_TO_KIND: dict[str, str] = {
    "issue_body":       "declaration",
    "pr_discussion":    "causal_link",
    "resolving_patch":  "retirement",
    "test_patch":       "declaration",
}


def _mid_for(instance_id: str, message_id: str) -> str:
    """Canonical mid: ``swe-<slug>-<suffix>``.

    ``message_id`` out of mine.py looks like
    ``<iid>::problem_statement``.  We slugify iid and drop the
    namespace prefix for readability.
    """
    slug = instance_id.replace("__", "-").replace("/", "-").lower()
    suffix = message_id.split("::", 1)[-1]
    return f"swe-{slug}-{suffix}"


def label_row(row: dict, *, is_first_in_subject: bool) -> dict:
    """Transform one raw row into a CorpusMemory-shaped dict."""
    source = row.get("source", "unknown")
    kind = SOURCE_TO_KIND.get(source, "none")

    # The issue body is always the ordinal anchor of its subject.
    if is_first_in_subject and source == "issue_body":
        kind = "ordinal_anchor"

    message_id = row["message_id"]
    instance_id = message_id.split("::", 1)[0]
    subject = f"swe-{instance_id.replace('__', '-').lower()}"

    return {
        "mid": _mid_for(instance_id, message_id),
        "subject": subject,
        "content": row["text"],
        "observed_at": row["timestamp"],
        "entities": [],
        "metadata": {
            "kind": kind,
            "source": source,
            "repo": row.get("repo", ""),
            "base_commit": row.get("base_commit", ""),
            "source_msg_id": message_id,
            "instance_id": instance_id,
            "domains": ["software_dev", row.get("repo", "").split("/")[0]],
        },
    }


def _llm_refine(labeled: list[dict]) -> list[dict]:  # pragma: no cover
    """Optional LLM cleanup pass (stubbed).

    For v1 we trust the source→kind map.  When the gold evaluation
    shows systematic errors, implement this to re-examine
    ``kind == "causal_link"`` rows and promote declarations /
    retirements that slipped in as pr_discussion.
    """
    raise NotImplementedError(
        "LLM refinement intentionally disabled for MSEB-SWE v1 — "
        "source→kind map is deterministic.  See docstring.",
    )


def label_file(raw_path: Path, out_path: Path) -> dict[str, int]:
    """Label one raw/<iid>.jsonl → raw_labeled/<iid>.jsonl.

    Returns per-kind counts.
    """
    stats = {"declaration": 0, "retirement": 0, "causal_link": 0,
             "ordinal_anchor": 0, "none": 0, "total": 0}

    raw_rows = [
        json.loads(line) for line in
        raw_path.read_text(encoding="utf-8").split(chr(10)) if line.strip()
    ]
    if not raw_rows:
        return stats

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(raw_rows):
            labeled = label_row(row, is_first_in_subject=(i == 0))
            fh.write(json.dumps(labeled, ensure_ascii=False))
            fh.write("\n")
            kind = labeled["metadata"]["kind"]
            stats[kind] = stats.get(kind, 0) + 1
            stats["total"] += 1
    return stats


def label_all(raw_dir: Path, out_dir: Path) -> dict:
    """Label every raw file in raw_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    file_count = 0
    for raw in sorted(raw_dir.glob("*.jsonl")):
        if raw.name.startswith("_"):
            continue
        stats = label_file(raw, out_dir / raw.name)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        file_count += 1
        logger.debug("%s: %s", raw.name, stats)
    summary = {"files": file_count, **totals}
    (out_dir / "_label_stats.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
    )
    logger.info("labeled %d files, %d memories", file_count, totals.get("total", 0))
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="MSEB-SWE labeler: raw messages → CorpusMemory JSONL",
    )
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    summary = label_all(args.raw_dir, args.out_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
