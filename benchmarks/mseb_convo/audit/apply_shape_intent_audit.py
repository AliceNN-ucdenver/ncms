"""Apply the hand-audit verdicts to produce a clean
``gold_shape_intent_conversational.jsonl`` for v6 retraining.

Reads ``/tmp/convo_gold_audit.jsonl`` (per-query verdicts) joined
with the MSEB convo gold, produces the standard GoldExample JSONL
format with the AUDITED shape_intent (post-remap).

* ``keep`` → emit with the current shape as shape_intent
* ``remap:<X>`` → emit with X as shape_intent
* ``drop`` → skip (rare)
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

AUDIT_JSONL = Path("/tmp/convo_gold_audit.jsonl")
GOLD_YAML = Path("/Users/shawnmccarthy/ncms/benchmarks/mseb_convo/gold_locked.yaml")
OUT_PATH = Path(
    "/Users/shawnmccarthy/ncms/experiments/intent_slot_distillation/"
    "corpus/gold_shape_intent_conversational.jsonl"
)

# MSEB "noise" → shape_intent "none"
_NOISE_REMAP = {"noise": "none"}


def main() -> None:
    audit = {}
    for line in AUDIT_JSONL.read_text().splitlines():
        rec = json.loads(line)
        audit[rec["qid"]] = rec

    gold_by_qid = {r["qid"]: r for r in yaml.safe_load(GOLD_YAML.read_text())}

    out_rows: list[dict] = []
    stats: dict[str, int] = {}
    for qid, verdict_rec in audit.items():
        if qid not in gold_by_qid:
            continue
        verdict = verdict_rec["verdict"]

        if verdict == "drop":
            stats["drop"] = stats.get("drop", 0) + 1
            continue

        if verdict == "keep":
            shape = verdict_rec["current_shape"]
        elif verdict.startswith("remap:"):
            shape = verdict[6:]
        else:
            continue

        # Convert MSEB "noise" to TLG "none".
        shape = _NOISE_REMAP.get(shape, shape)

        gold_row = gold_by_qid[qid]
        out_rows.append(
            {
                "text": gold_row["text"],
                "domain": "conversational",
                "intent": "none",
                "slots": {},
                "topic": None,
                "admission": "persist",
                "state_change": "none",
                "shape_intent": shape,
                "split": "gold",
                "source": f"mseb-gold-v2-audited {qid}",
                "note": (f"audit_verdict={verdict}; audit_reason={verdict_rec['reason']}"),
            }
        )
        stats[shape] = stats.get(shape, 0) + 1

    OUT_PATH.write_text(
        "\n".join(json.dumps(r) for r in out_rows) + "\n",
    )

    print(f"Wrote {len(out_rows)} rows to {OUT_PATH}")
    print("\nPost-audit shape_intent distribution:")
    for shape in sorted(stats):
        print(f"  {shape:18} {stats[shape]:4d}")


if __name__ == "__main__":
    main()
