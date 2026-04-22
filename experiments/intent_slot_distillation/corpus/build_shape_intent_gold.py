"""Extract MSEB gold queries into per-domain GoldExample rows.

The MSEB gold YAMLs already label every query with a ``shape``
field drawn from the TLG grammar's 12 productions + ``noise``.
This script converts those 747 queries into JSONL rows suitable
for training the 6th SLM head (``shape_intent_head``).

For each gold query row we emit:

* ``text``          — the query string verbatim
* ``domain``        — SLM adapter domain (swe_diff / clinical / …)
* ``intent``        — ``none`` (queries aren't preference utterances)
* ``slots``         — empty for now; Pass 4 adds ``subject`` slot
  extraction
* ``topic``         — ``None`` (queries don't carry corpus-topic tags;
  training loss masks this head on shape-intent rows)
* ``admission``     — ``persist`` (queries themselves aren't admitted,
  but this is a sensible default that keeps the admission head's
  training distribution from being warped)
* ``state_change``  — ``none`` (queries don't declare/retire state)
* ``shape_intent``  — from the gold's ``shape`` field (``noise`` → ``none``)
* ``split``         — ``gold``
* ``source``        — ``mseb-gold-v1 <qid>``

The result ships as ``gold_shape_intent_<domain>.jsonl`` and gets
mixed into the training corpus for the corresponding adapter by
``train_adapter.py``.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml

# MSEB domain name → SLM adapter name.
MSEB_TO_ADAPTER: dict[str, str] = {
    "swe":         "swe_diff",
    "clinical":    "clinical",
    "softwaredev": "software_dev",
    "convo":       "conversational",
}

# MSEB gold ``shape`` field → SLM ``shape_intent`` label.
# MSEB ``noise`` maps to ``none`` (grammar abstain is the correct
# behaviour — noise queries should fall through to BM25).
_SHAPE_MAP: dict[str, str] = {
    "current_state":     "current_state",
    "before_named":      "before_named",
    "concurrent":        "concurrent",
    "origin":            "origin",
    "retirement":        "retirement",
    "sequence":          "sequence",
    "predecessor":       "predecessor",
    "transitive_cause":  "transitive_cause",
    "causal_chain":      "causal_chain",
    "interval":          "interval",
    "ordinal_first":     "ordinal_first",
    "ordinal_last":      "ordinal_last",
    "noise":             "none",
}

# Project root two levels up from this file.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BENCHMARKS = _ROOT / "benchmarks"
_CORPUS_OUT = Path(__file__).resolve().parent


def _load_mseb_gold(mseb_domain: str) -> list[dict]:
    path = _BENCHMARKS / f"mseb_{mseb_domain}" / "gold_locked.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no MSEB gold at {path}")
    rows = yaml.safe_load(path.read_text()) or []
    return rows


def _to_gold_example(row: dict, adapter_domain: str) -> dict:
    shape = row.get("shape", "none")
    shape_intent = _SHAPE_MAP.get(shape, "none")
    return {
        "text":         row["text"],
        "domain":       adapter_domain,
        "intent":       "none",
        "slots":        {},
        "topic":        None,
        "admission":    "persist",
        "state_change": "none",
        "shape_intent": shape_intent,
        "split":        "gold",
        "source":       f"mseb-gold-v1 {row.get('qid', '')}",
        "note":         f"shape_intent={shape_intent}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--out-dir", type=Path, default=_CORPUS_OUT,
        help="Output directory for the JSONL files.",
    )
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    by_domain_label: dict[str, Counter] = {}

    for mseb_domain, adapter_domain in MSEB_TO_ADAPTER.items():
        rows = _load_mseb_gold(mseb_domain)
        converted = [_to_gold_example(r, adapter_domain) for r in rows]
        out_path = args.out_dir / f"gold_shape_intent_{adapter_domain}.jsonl"
        out_path.write_text("\n".join(json.dumps(r) for r in converted) + "\n")
        labels = Counter(r["shape_intent"] for r in converted)
        by_domain_label[adapter_domain] = labels
        total += len(converted)
        print(
            f"[shape-intent-gold] {mseb_domain:>11} -> {adapter_domain:12} "
            f"n={len(converted):4d}  {dict(labels)}  -> {out_path.name}"
        )

    print(f"\nTotal: {total} query-voice training rows across "
          f"{len(MSEB_TO_ADAPTER)} adapters")
    print("\nPer-domain × shape_intent label distribution:")
    all_labels = sorted({lab for c in by_domain_label.values() for lab in c})
    print(f"  {'domain':14} " + " ".join(f"{lab:16}" for lab in all_labels))
    for domain, counts in sorted(by_domain_label.items()):
        print(f"  {domain:14} " + " ".join(
            f"{counts.get(lab, 0):16d}" for lab in all_labels
        ))


if __name__ == "__main__":
    main()
