"""Auto-label existing gold JSONL with topic / admission / state_change.

Idempotent — reads gold JSONL, augments each row with multi-head
labels derived from the domain taxonomy's ``object_to_topic`` map,
and writes back in place (with a backup).

Heuristics match the SDG expander's conventions:

* ``topic``: looked up from the row's slots against
  ``object_to_topic``.  ``"other"`` if unmapped.
* ``admission``: always ``"persist"`` for preference content.
* ``state_change``: always ``"none"`` for preferences.

Rows that already carry multi-head labels are left unchanged — the
utility is safe to run repeatedly.

Usage::

    uv run python -m experiments.intent_slot_distillation.corpus.autolabel_multihead \\
        --gold-path experiments/intent_slot_distillation/corpus/gold_conversational.jsonl \\
        --taxonomy experiments/intent_slot_distillation/taxonomies/conversational.yaml
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from experiments.intent_slot_distillation.corpus.loader import (
    dump_jsonl,
    load_jsonl,
)


def _load_object_to_topic(taxonomy_path: Path) -> dict[str, str]:
    import yaml
    data = yaml.safe_load(taxonomy_path.read_text()) or {}
    return {
        str(k).lower(): str(v)
        for k, v in (data.get("object_to_topic") or {}).items()
    }


def _pick_topic_for_row(
    slots: dict[str, str], mapping: dict[str, str],
) -> str:
    """Map primary slot surface to a topic label.

    Walks the row's slots looking for one whose lowercased value is
    in the object_to_topic map.  Falls back to ``"other"`` when no
    slot matches.  Deterministic — first hit wins.
    """
    for value in slots.values():
        lookup = value.lower().strip()
        if lookup in mapping:
            return mapping[lookup]
    return "other"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-label gold JSONL with multi-head labels",
    )
    parser.add_argument("--gold-path", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing.",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip the .bak sidecar (default: write one).",
    )
    args = parser.parse_args()

    examples = load_jsonl(args.gold_path)
    mapping = _load_object_to_topic(args.taxonomy)
    if not mapping:
        raise SystemExit(
            f"taxonomy {args.taxonomy} has no object_to_topic map",
        )

    changed = 0
    already = 0
    for ex in examples:
        if ex.topic is not None and ex.admission is not None \
                and ex.state_change is not None:
            already += 1
            continue
        if ex.topic is None:
            ex.topic = _pick_topic_for_row(ex.slots, mapping)
        if ex.admission is None:
            ex.admission = "persist"  # preferences always persist-worthy
        if ex.state_change is None:
            ex.state_change = "none"  # preferences don't retire state
        changed += 1

    print(
        f"[autolabel] domain={examples[0].domain if examples else '?'} "
        f"rows={len(examples)} changed={changed} already_labeled={already}",
    )

    if args.dry_run:
        for ex in examples[:5]:
            print(
                f"  {ex.text!r:<60}  topic={ex.topic}  "
                f"admission={ex.admission}  state_change={ex.state_change}",
            )
        return

    if not args.no_backup and changed > 0:
        backup = args.gold_path.with_suffix(args.gold_path.suffix + ".bak")
        shutil.copy2(args.gold_path, backup)
        print(f"[autolabel] backup written to {backup}")

    dump_jsonl(examples, args.gold_path)
    print(f"[autolabel] wrote {args.gold_path}")


if __name__ == "__main__":
    main()
