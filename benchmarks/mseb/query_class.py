"""Deterministic classifier that tags a gold query with its ``query_class``.

Cross-cutting categories the results matrix breaks out:

- ``noise``       — adversarial off-topic (``shape == "noise"``).
- ``preference``  — pref != "none", or the LMEval question_type is
  ``single-session-preference``.
- ``temporal``    — the NCMS temporal parser fires on the query text,
  OR the shape is one of the explicit temporal family
  (``ordinal_first`` / ``ordinal_last`` / ``sequence`` / ``predecessor``
  / ``before_named``).
- ``general``     — everything else (standard lexical retrieval,
  still interesting for BM25+SPLADE vs dense comparison).

One row at a time; the batch entry point
:func:`tag_gold_file` annotates every gold query in place.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.mseb.schema import GoldQuery, QueryClass

logger = logging.getLogger("mseb.query_class")


# Shapes that are inherently temporal — classifier falls back to
# ``temporal`` on these regardless of parser hits, because the
# evaluation mechanism for the shape itself is time-ordered retrieval.
_TEMPORAL_SHAPES: frozenset[str] = frozenset(
    {
        "ordinal_first",
        "ordinal_last",
        "sequence",
        "predecessor",
        "before_named",
    }
)


def classify(query: GoldQuery) -> QueryClass:
    """Assign a :class:`QueryClass` label to one gold query."""
    if query.shape == "noise":
        return "noise"
    if query.preference != "none":
        return "preference"
    if query.shape in _TEMPORAL_SHAPES:
        return "temporal"

    # Try the NCMS temporal parser — catches "most recent", "current",
    # etc. on shapes like current_state that aren't intrinsically
    # temporal but happen to have time-referring wording.
    try:
        from ncms.domain.temporal.parser import parse_temporal_reference

        ref = parse_temporal_reference(query.text, now=datetime.now(UTC))
        if ref is not None and (ref.ordinal or ref.recency_bias or ref.range_start):
            return "temporal"
    except Exception:  # pragma: no cover — keep classifier robust
        pass

    return "general"


def tag_gold_file(path: Path, overwrite: bool = True) -> dict[str, int]:
    """Load a gold.yaml, classify each row, write back with tags.

    Returns a per-class count summary.
    """
    try:
        import yaml

        rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except ImportError:
        rows = json.loads(path.read_text(encoding="utf-8"))

    classes: Counter[str] = Counter()
    for row in rows:
        # Build a minimal GoldQuery-like view (only the fields the
        # classifier needs).  Avoids re-loading as full dataclass.
        shape = row.get("shape", "current_state")
        pref = row.get("preference", "none")
        text = row.get("text", "")

        # Reuse the same logic as ``classify`` without needing the
        # full dataclass instantiation.
        if shape == "noise":
            cls = "noise"
        elif pref != "none":
            cls = "preference"
        elif shape in _TEMPORAL_SHAPES:
            cls = "temporal"
        else:
            try:
                from ncms.domain.temporal.parser import parse_temporal_reference

                ref = parse_temporal_reference(text, now=datetime.now(UTC))
                cls = (
                    "temporal"
                    if (ref is not None and (ref.ordinal or ref.recency_bias or ref.range_start))
                    else "general"
                )
            except Exception:
                cls = "general"

        row["query_class"] = cls
        classes[cls] += 1

    if overwrite:
        try:
            import yaml

            body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
            # Preserve any comment header that was on the file.
            original = path.read_text(encoding="utf-8")
            header = ""
            for line in original.split("\n"):
                if line.startswith("#") or not line.strip():
                    header += line + "\n"
                else:
                    break
            path.write_text(header + body, encoding="utf-8")
        except ImportError:  # pragma: no cover
            path.write_text(
                json.dumps(rows, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    return dict(classes)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="Tag gold queries with query_class")
    ap.add_argument(
        "paths", nargs="+", type=Path, help="One or more gold.yaml files to tag in place."
    )
    ap.add_argument(
        "--no-overwrite", action="store_true", help="Just report classes; don't modify files."
    )
    args = ap.parse_args()

    overall: Counter[str] = Counter()
    for p in args.paths:
        if not p.exists():
            logger.warning("missing: %s", p)
            continue
        stats = tag_gold_file(p, overwrite=not args.no_overwrite)
        overall.update(stats)
        print(f"{p}: {dict(stats)}")
    print(f"\ntotal across {len(args.paths)} file(s): {dict(overall)}")


if __name__ == "__main__":
    sys.exit(main())
