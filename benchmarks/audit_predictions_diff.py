"""Per-qid comparison: OLD (pre-78b9fc9) vs NEW (post-78b9fc9) predictions.

Fact-finding only — no assumptions.  For every qid:

  - gold_mid (from gold_locked.yaml)
  - OLD rank of gold, NEW rank of gold
  - OLD top-1 mid, NEW top-1 mid
  - NEW head_outputs: shape_intent, intent, topic, admission, state_change
    with confidences (from the new predictions.jsonl's head_outputs field)

Emits three reports:
  - summary: r@1 transition matrix (old hit / miss × new hit / miss)
  - regressions: qids where OLD hit top-1 and NEW missed
  - gains: qids where NEW hit top-1 and OLD missed
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path("/Users/shawnmccarthy/ncms")
OLD = ROOT / (
    "benchmarks/results/mseb/main12/main_softwaredev_ncms_tlg-on_20260421T123020Z.predictions.jsonl"
)
NEW = ROOT / (
    "benchmarks/results/mseb/main12/"
    "main_softwaredev_ncms_temporal-on_20260422T134805Z.predictions.jsonl"
)
GOLD = ROOT / "benchmarks/mseb_softwaredev/gold_locked.yaml"


def load_preds(path: Path) -> dict[str, dict]:
    out = {}
    with path.open() as f:
        for line in f:
            d = json.loads(line)
            out[d["qid"]] = d
    return out


def rank_of(mids: list[str], target: str) -> int | None:
    for i, m in enumerate(mids, start=1):
        if m == target:
            return i
    return None


def main() -> None:
    gold = {g["qid"]: g for g in yaml.safe_load(GOLD.read_text())}
    old = load_preds(OLD)
    new = load_preds(NEW)

    common = sorted(set(old) & set(new))
    print(f"qids: old={len(old)} new={len(new)} common={len(common)}")
    print()

    transitions: Counter = Counter()
    regressions: list[dict] = []
    gains: list[dict] = []
    by_shape: dict[str, Counter] = {}

    for qid in common:
        g = gold.get(qid, {})
        gmid = g.get("gold_mid", "")
        shape = g.get("shape", "?")
        o_rank = rank_of(old[qid]["ranked_mids"], gmid)
        n_rank = rank_of(new[qid]["ranked_mids"], gmid)
        o_hit = o_rank == 1
        n_hit = n_rank == 1
        key = f"old={'hit' if o_hit else 'miss'}_new={'hit' if n_hit else 'miss'}"
        transitions[key] += 1
        by_shape.setdefault(shape, Counter())[key] += 1

        if o_hit and not n_hit:
            regressions.append(
                {
                    "qid": qid,
                    "shape": shape,
                    "gold_mid": gmid,
                    "old_top1": old[qid]["ranked_mids"][0] if old[qid]["ranked_mids"] else None,
                    "new_top1": new[qid]["ranked_mids"][0] if new[qid]["ranked_mids"] else None,
                    "new_gold_rank": n_rank,
                    "new_heads": new[qid].get("head_outputs", {}),
                    "query": g.get("text", ""),
                }
            )
        elif n_hit and not o_hit:
            gains.append(
                {
                    "qid": qid,
                    "shape": shape,
                    "gold_mid": gmid,
                    "old_top1": old[qid]["ranked_mids"][0] if old[qid]["ranked_mids"] else None,
                    "new_top1": new[qid]["ranked_mids"][0] if new[qid]["ranked_mids"] else None,
                    "old_gold_rank": o_rank,
                    "new_heads": new[qid].get("head_outputs", {}),
                    "query": g.get("text", ""),
                }
            )

    print("R@1 transition matrix:")
    for key, n in sorted(transitions.items()):
        print(f"  {key:30s} {n:4d}")

    print()
    print("Per-shape breakdown:")
    for shape, cnts in sorted(by_shape.items()):
        total = sum(cnts.values())
        hit_new = cnts.get("old=hit_new=hit", 0) + cnts.get("old=miss_new=hit", 0)
        print(f"  {shape:30s} total={total:4d} new_hit={hit_new:4d} ({hit_new / total:.2%})")
        for k, v in sorted(cnts.items()):
            print(f"    {k:30s} {v:4d}")

    print()
    print(f"REGRESSIONS (old hit top-1, new missed): {len(regressions)}")
    print("=" * 80)
    for r in regressions[:12]:
        h = r["new_heads"] or {}
        print(f"\nqid={r['qid']}  shape={r['shape']}  new_gold_rank={r['new_gold_rank']}")
        print(f"  query: {r['query'][:100]}")
        print(f"  gold_mid: {r['gold_mid']}")
        print(f"  old_top1: {r['old_top1']}")
        print(f"  new_top1: {r['new_top1']}")
        print("  new heads:")
        print(
            f"    shape_intent     = {h.get('shape_intent')!r:30}  "
            f"conf={h.get('shape_intent_conf')!r}"
        )
        print(f"    intent           = {h.get('intent')!r:30}  conf={h.get('intent_conf')!r}")
        print(f"    topic            = {h.get('topic')!r:30}  conf={h.get('topic_conf')!r}")
        print(f"    admission        = {h.get('admission')!r:30}  conf={h.get('admission_conf')!r}")
        print(
            f"    state_change     = {h.get('state_change')!r:30}  "
            f"conf={h.get('state_change_conf')!r}"
        )
        print(f"    slots            = {h.get('slots')!r}")
        print(f"    adapter          = {h.get('adapter')!r}")
    if len(regressions) > 12:
        print(f"\n... and {len(regressions) - 12} more regressions")

    print()
    print(f"GAINS (new hit top-1, old missed): {len(gains)}")
    print("=" * 80)
    for r in gains[:6]:
        h = r["new_heads"] or {}
        print(f"\nqid={r['qid']}  shape={r['shape']}  old_gold_rank={r['old_gold_rank']}")
        print(f"  query: {r['query'][:100]}")
        print(f"  gold_mid: {r['gold_mid']}")
        print(f"  old_top1: {r['old_top1']}")
        print(f"  new_top1: {r['new_top1']}")
    if len(gains) > 6:
        print(f"\n... and {len(gains) - 6} more gains")


if __name__ == "__main__":
    main()
