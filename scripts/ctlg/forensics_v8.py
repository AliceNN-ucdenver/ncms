"""v8 CTLG forensics — per-cue-family F1 + synthesizer hit rate.

Loads the trained v8 adapter, runs it over the 640-row cue-gold
corpus, computes:

  1. Per-cue-family F1 (BIO-tag level) — does the model learn each
     cue family?  Breakdown by family: CAUSAL, TEMPORAL, ORDINAL,
     MODAL, ASK, REFERENT, SUBJECT, SCOPE.
  2. Synthesizer hit rate on real-model cue output — does the
     rule engine fire on model predictions the way it fired on
     LLM labels?
  3. Content-head sanity — intent/admission/state_change/role should
     match v7.2.

Eval is held-out: for each row we strip the gold cue_tags, run the
model's extract() to get predicted cue_tags, and compare.

Output: ``docs/forensics/v8-ctlg-forensics.md``
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.application.adapters.corpus.loader import load_jsonl  # noqa: E402
from ncms.application.adapters.methods.joint_bert_lora import (  # noqa: E402
    LoraJointBert,
)
from ncms.domain.tlg.cue_taxonomy import (  # noqa: E402
    CUE_FAMILIES,
    TaggedToken,
    group_bio_spans,
)
from ncms.domain.tlg.semantic_parser import synthesize  # noqa: E402


def _load_gold() -> list:
    rows = load_jsonl(str(_REPO / "adapters/corpora/gold_cues_software_dev.jsonl"))
    return [r for r in rows if r.domain == "software_dev" and r.cue_tags]


def _tagged_from_gold(cue_tags: list[dict]) -> list[TaggedToken]:
    return [
        TaggedToken(
            char_start=t["char_start"], char_end=t["char_end"],
            surface=t["surface"], cue_label=t["cue_label"],
            confidence=t.get("confidence", 1.0),
        )
        for t in cue_tags
    ]


def _span_key(sp: list[TaggedToken]) -> tuple[int, int, str]:
    """Canonical (start, end, cue_type) key for set-level F1."""
    cue_type = sp[0].cue_label.partition("-")[2]
    return (sp[0].char_start, sp[-1].char_end, cue_type)


def _family_of(cue_type: str) -> str | None:
    """Map a bare cue type (e.g. TEMPORAL_BEFORE) to its family."""
    # BIO-less form
    label = f"B-{cue_type}"
    for fam, labels in CUE_FAMILIES.items():
        if label in labels:
            return fam
    return None


def _family_of_label(label: str) -> str | None:
    """Map a BIO label to its family."""
    for fam, labels in CUE_FAMILIES.items():
        if label in labels:
            return fam
    return None


def run() -> None:
    print("=" * 72)
    print("v8 FORENSICS — loading adapter + gold")
    print("=" * 72)

    adapter_dir = _REPO / "adapters/checkpoints/software_dev/v8"
    ex = LoraJointBert(adapter_dir)
    gold = _load_gold()
    print(f"  adapter:      {adapter_dir}")
    print(f"  gold rows:    {len(gold)}")

    # ── 1. Per-cue-family F1 ──────────────────────────────────────
    print()
    print("=" * 72)
    print("FORENSICS 1 — per-cue-family F1 (span-level exact match)")
    print("=" * 72)

    # Per-family tallies
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    total_gold_spans = 0
    total_pred_spans = 0

    synth_hits = 0
    synth_miss = 0
    for row in gold:
        pred = ex.extract(row.text, domain="software_dev")
        pred_tokens = list(pred.cue_tags)
        gold_tokens = _tagged_from_gold(row.cue_tags)

        pred_spans = group_bio_spans(pred_tokens)
        gold_spans = group_bio_spans(gold_tokens)
        total_gold_spans += len(gold_spans)
        total_pred_spans += len(pred_spans)

        gold_keys = {_span_key(t) for _, t in gold_spans}
        pred_keys = {_span_key(t) for _, t in pred_spans}

        # Per-family TP/FP/FN
        for key in gold_keys & pred_keys:
            fam = _family_of(key[2])
            if fam:
                tp[fam] += 1
        for key in pred_keys - gold_keys:
            fam = _family_of(key[2])
            if fam:
                fp[fam] += 1
        for key in gold_keys - pred_keys:
            fam = _family_of(key[2])
            if fam:
                fn[fam] += 1

        # Synthesizer hit rate on PREDICTED cues
        q = synthesize(pred_tokens)
        if q is None:
            synth_miss += 1
        else:
            synth_hits += 1

    print(f"\n  total gold spans: {total_gold_spans}")
    print(f"  total pred spans: {total_pred_spans}")
    print()
    print(f"{'family':<18} {'P':>6} {'R':>6} {'F1':>6} "
          f"{'TP':>5} {'FP':>5} {'FN':>5}")
    print("-" * 60)
    macro_f1_list = []
    for fam in sorted(set(list(tp)+list(fp)+list(fn))):
        t = tp[fam]
        f = fp[fam]
        n = fn[fam]
        precision = t / (t + f) if t + f else 0.0
        recall = t / (t + n) if t + n else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        support = t + n
        if support > 0:
            macro_f1_list.append(f1)
        print(f"{fam:<18} {precision:>6.3f} {recall:>6.3f} {f1:>6.3f} "
              f"{t:>5} {f:>5} {n:>5}")
    print("-" * 60)
    macro_f1 = sum(macro_f1_list) / len(macro_f1_list) if macro_f1_list else 0.0
    print(f"macro F1 (over supported families): {macro_f1:.4f}")

    # ── 2. Synthesizer hit rate ──────────────────────────────────
    print()
    print("=" * 72)
    print("FORENSICS 2 — synthesizer hit rate on real-model cue output")
    print("=" * 72)
    total = synth_hits + synth_miss
    print(f"  synth hits: {synth_hits}/{total} = "
          f"{synth_hits/total:.1%} (vs LLM-labeled baseline 56.1%)")

    # ── 3. Content-head sanity (compare to v7.2) ─────────────────
    print()
    print("=" * 72)
    print("FORENSICS 3 — content-head predictions (sanity sample)")
    print("=" * 72)
    intent_counts: dict[str, int] = defaultdict(int)
    admission_counts: dict[str, int] = defaultdict(int)
    state_counts: dict[str, int] = defaultdict(int)
    role_counts: dict[str, int] = defaultdict(int)
    for row in gold[:100]:  # sample 100 rows
        pred = ex.extract(row.text, domain="software_dev")
        intent_counts[str(pred.intent)] += 1
        admission_counts[str(pred.admission)] += 1
        state_counts[str(pred.state_change)] += 1
        for rs in pred.role_spans:
            role_counts[str(rs.role)] += 1
    print(f"  intent distribution (on 100-row sample): {dict(intent_counts)}")
    print(f"  admission: {dict(admission_counts)}")
    print(f"  state_change: {dict(state_counts)}")
    print(f"  role_spans: {dict(role_counts)}")


if __name__ == "__main__":
    run()
