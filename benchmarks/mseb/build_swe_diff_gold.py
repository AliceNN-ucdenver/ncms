"""Build ``gold_swe_diff.jsonl`` from MSEB-SWE's rule-labeled corpus.

The MSEB-SWE labeler (``benchmarks/mseb_swe/label.py``) assigns
``MemoryKind`` from the immutable ``source`` field of each
message, which for this corpus is source-deterministic:

    issue_body       → declaration  (first report of a bug state)
    pr_discussion    → causal_link  (discussion, not a state change)
    resolving_patch  → retirement   (old impl retired)
    test_patch       → declaration  (new invariant declared)

…and we override the FIRST memory of each subject chain to
``ordinal_anchor`` (issue body is always the origin).

For the ``swe_diff`` adapter we only train the
``state_change_head`` + ``topic_head`` + ``admission_head`` +
``slot_head``.  Intent_head is irrelevant on diff content
(preferences live in conversations, not patches) — we set all
intent labels to ``none`` and let the LoRA training skip that
head's loss when intent stays constant.

Topic labels are derived from file paths in the diff.
Symbol slot values come from our entity extractor.

Run once::

    uv run python -m benchmarks.mseb.build_swe_diff_gold \\
        --labeled-dir benchmarks/mseb_swe/raw_labeled \\
        --out experiments/intent_slot_distillation/corpus/gold_swe_diff.jsonl

The output plugs directly into ``train_adapter.py --domain swe_diff``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from benchmarks.mseb.gold_author import (
    extract_backtick_symbols,
    extract_patch_files,
    first_non_test_path,
    first_test_path,
)

logger = logging.getLogger("mseb.build_swe_diff_gold")


# MSEB MemoryKind → swe_diff state_change label.
# ``causal_link`` (PR discussions) don't declare a state change, they
# reason about one — so map to ``none``.  ordinal_anchor acts like
# a declaration (the first state in a chain).
KIND_TO_STATE_CHANGE: dict[str, str] = {
    "declaration": "declaration",
    "retirement": "retirement",
    "ordinal_anchor": "declaration",
    "causal_link": "none",
    "none": "none",
}


def topic_from_source_and_path(source: str, content: str) -> str:
    """Map a SWE message to the swe_diff topic taxonomy.

    - patches touching tests/   → test_module
    - patches touching docs/    → docs
    - patches touching setup.* / pyproject → config
    - other patches              → core_module
    - issue bodies / PR discussion → core_module (conservative default)
    """
    if source not in ("resolving_patch", "test_patch"):
        return "core_module"
    if first_test_path(content):
        return "test_module"
    path = first_non_test_path(content) or ""
    plow = path.lower()
    if "docs/" in plow or plow.endswith((".rst", ".md")):
        return "docs"
    if plow.endswith((".cfg", ".toml", ".ini")) or "setup.py" in plow or "pyproject" in plow:
        return "config"
    if (
        plow.endswith((".yaml", ".yml", ".dockerfile"))
        or "makefile" in plow
        or "dockerfile" in plow
    ):
        return "build"
    return "core_module"


def slots_for_memory(source: str, content: str) -> dict[str, str]:
    """Extract typed slots for the swe_diff slot_head BIO head.

    Fields map to ``SLOT_TAXONOMY["swe_diff"]`` (see schemas.py):
    ``file_path`` / ``test_path`` / ``function`` / ``symbol``.
    """
    slots: dict[str, str] = {}
    syms = extract_backtick_symbols(content)
    if syms:
        slots["symbol"] = syms[0]
    if source in ("resolving_patch", "test_patch"):
        files = extract_patch_files(content)
        if files:
            non_test = first_non_test_path(content)
            tst = first_test_path(content)
            if non_test:
                slots["file_path"] = non_test
            if tst:
                slots["test_path"] = tst
    return slots


def row_to_gold_example(row: dict) -> dict:
    """Transform one rule-labeled MSEB-SWE row into GoldExample JSONL."""
    content = row["content"]
    meta = row.get("metadata", {})
    source = meta.get("source", "")
    kind = meta.get("kind", "none")

    state_change = KIND_TO_STATE_CHANGE.get(kind, "none")
    topic = topic_from_source_and_path(source, content)
    slots = slots_for_memory(source, content)

    return {
        # GoldExample fields — see experiments/intent_slot_distillation/schemas.py
        "text": content[:4000],  # cap at the same 4k the miner uses
        "domain": "swe_diff",
        "intent": "none",  # diff content carries no preference
        "slots": slots,
        "topic": topic,
        "admission": "persist",  # everything the MSEB corpus keeps is persist-grade
        "state_change": state_change,
        "split": "gold",
        "source": (f"mseb_swe_rule_label/{source}/{kind}/{meta.get('instance_id', '?')}"),
        "note": "",
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--labeled-dir", type=Path, default=Path("benchmarks/mseb_swe/raw_labeled"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    n_total = 0
    by_state_change: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    by_source: dict[str, int] = {}

    with args.out.open("w", encoding="utf-8") as fh:
        for jsonl in sorted(args.labeled_dir.glob("*.jsonl")):
            if jsonl.name.startswith("_"):
                continue
            for line in jsonl.read_text(encoding="utf-8").split(chr(10)):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ex = row_to_gold_example(row)
                fh.write(json.dumps(ex, ensure_ascii=False))
                fh.write("\n")
                n_total += 1
                by_state_change[ex["state_change"]] = (
                    by_state_change.get(
                        ex["state_change"],
                        0,
                    )
                    + 1
                )
                by_topic[ex["topic"]] = by_topic.get(ex["topic"], 0) + 1
                src = row.get("metadata", {}).get("source", "?")
                by_source[src] = by_source.get(src, 0) + 1

    stats = {
        "total_examples": n_total,
        "by_state_change": by_state_change,
        "by_topic": by_topic,
        "by_source": by_source,
    }
    (args.out.with_suffix(".stats.json")).write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    logger.info("wrote %d examples to %s", n_total, args.out)
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
