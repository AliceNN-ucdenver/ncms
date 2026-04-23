"""Sync gold's ``slots`` dict with its ``role_spans``.

The human labelers who hand-labeled gold frequently omitted the
``alternative`` slot and occasionally disagreed with the role-labeled
primaries.  Post-v7 we have authoritative role_spans (from the LLM
role-classification pass), so the slots dict can be reconstructed
deterministically:

    for each primary role_span:
        if its catalog slot isn't already in slots, add it
    for each alternative role_span (highest-confidence-style: first wins):
        add to slots['alternative'] if absent

We are deliberately CONSERVATIVE — we only ADD missing entries, we
don't overwrite existing human-labeled values.  Human gold wins on
conflict (the labeler may have intentionally called a casual-mention
surface the primary for this memory's purpose).

Usage::

    uv run python scripts/v7_rollout/repair_gold_slots.py --domain software_dev
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ncms.application.adapters.corpus.loader import dump_jsonl, load_jsonl
from ncms.application.adapters.schemas import get_domain_manifest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("repair_gold")


def repair(domain: str) -> None:
    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    path = manifest.gold_jsonl
    rows = load_jsonl(path)
    log.info("repair: %s rows=%d", path, len(rows))

    primary_added = 0
    alt_added = 0
    rows_changed = 0
    for ex in rows:
        if not ex.role_spans:
            continue
        existing = dict(ex.slots)
        changed = False
        for rs in ex.role_spans:
            if rs.role == "primary" and rs.slot not in existing:
                existing[rs.slot] = rs.canonical
                primary_added += 1
                changed = True
            elif rs.role == "alternative" and "alternative" not in existing:
                existing["alternative"] = rs.canonical
                alt_added += 1
                changed = True
        if changed:
            ex.slots = existing
            rows_changed += 1

    dump_jsonl(rows, path)
    log.info(
        "repaired %d rows: %d primary slot additions, %d alternative additions",
        rows_changed, primary_added, alt_added,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True)
    args = p.parse_args()
    repair(args.domain)


if __name__ == "__main__":
    main()
