"""v8.1 CTLG forensics — per-cue-family F1 + synthesizer hit rate on HELD-OUT.

v8 forensics measured the adapter on its own training corpus.  v8.1
trains with an 80/20 held-out split (``heldout_gold.jsonl``); this
script evaluates both v8 and v8.1 on that same held-out slice so
the comparison is apples-to-apples.

Computes:

  1. Per-cue-family F1 on held-out (BIO tag level, span-exact).
  2. Synthesizer hit rate on real-model cue output.
  3. Content-head sanity (admission / state_change / topic / intent)
     on held-out — catches regression on the non-CTLG heads.

Output: ``docs/forensics/v8.1-ctlg-forensics.md``.

Usage::

    uv run python scripts/ctlg/forensics_v8_1.py \\
        --heldout adapters/checkpoints/software_dev/v8.1/heldout_gold.jsonl \\
        --v8-adapter ~/.ncms/adapters/software_dev/v8 \\
        --v8-1-adapter adapters/checkpoints/software_dev/v8.1
"""

from __future__ import annotations

import argparse
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
    cue_type = sp[0].cue_label.partition("-")[2]
    return (sp[0].char_start, sp[-1].char_end, cue_type)


def _family_of(cue_type: str) -> str | None:
    label = f"B-{cue_type}"
    for fam, labels in CUE_FAMILIES.items():
        if label in labels:
            return fam
    return None


def _eval_adapter(
    adapter_dir: Path, heldout: list, tag: str,
) -> tuple[dict[str, tuple[float, float, float, int]], float, dict]:
    """Evaluate one adapter on the held-out corpus.

    Returns (per_family_prf, synth_hit_rate, head_stats) where:
      per_family_prf = {fam: (P, R, F1, support)}
      synth_hit_rate = fraction of rows the synthesizer composed a
                       TLGQuery for
      head_stats     = {"intent_acc", "admission_acc", ...}
    """
    print(f"\n── loading {tag}: {adapter_dir} ──")
    ex = LoraJointBert(adapter_dir)

    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    synth_hits = 0

    head_hits = defaultdict(int)
    head_total = defaultdict(int)

    for row in heldout:
        pred = ex.extract(row.text, domain="software_dev")
        pred_tokens = list(pred.cue_tags)
        gold_tokens = _tagged_from_gold(row.cue_tags or [])

        pred_spans = group_bio_spans(pred_tokens)
        gold_spans = group_bio_spans(gold_tokens)

        gold_keys = {_span_key(t) for _, t in gold_spans}
        pred_keys = {_span_key(t) for _, t in pred_spans}

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

        # Synthesizer hit rate on predicted cue output.
        q = synthesize(pred_tokens)
        if q is not None:
            synth_hits += 1

        # Content-head stats (only when gold is labeled).
        if row.intent is not None:
            head_total["intent"] += 1
            if pred.intent == row.intent:
                head_hits["intent"] += 1
        if row.admission is not None:
            head_total["admission"] += 1
            if pred.admission == row.admission:
                head_hits["admission"] += 1
        if row.state_change is not None:
            head_total["state_change"] += 1
            if pred.state_change == row.state_change:
                head_hits["state_change"] += 1
        if row.topic is not None:
            head_total["topic"] += 1
            if pred.topic == row.topic:
                head_hits["topic"] += 1

    per_family: dict[str, tuple[float, float, float, int]] = {}
    for fam in sorted(set(list(tp) + list(fp) + list(fn))):
        t = tp[fam]
        f = fp[fam]
        n = fn[fam]
        p = t / (t + f) if t + f else 0.0
        r = t / (t + n) if t + n else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        support = t + n
        per_family[fam] = (p, r, f1, support)

    head_stats = {
        f"{head}_acc": head_hits[head] / head_total[head]
        if head_total[head] else 0.0
        for head in head_total
    }
    head_stats["synth_hit_rate"] = synth_hits / len(heldout)
    return per_family, synth_hits / len(heldout), head_stats


def _render_report(
    heldout_n: int,
    v8_family: dict, v8_synth: float, v8_heads: dict,
    v81_family: dict, v81_synth: float, v81_heads: dict,
    out_path: Path,
) -> None:
    all_fams = sorted(set(v8_family) | set(v81_family))
    lines: list[str] = []
    w = lines.append
    w("# v8.1 CTLG Forensics")
    w("")
    w(
        f"Evaluated on {heldout_n} held-out rows from "
        "`adapters/checkpoints/software_dev/v8.1/heldout_gold.jsonl` "
        "(the 20% of gold_cues_software_dev that was excluded from "
        "v8.1 training; the holdout_seed=42 shuffle is stable, so "
        "v8 — trained on the full corpus — is being evaluated here "
        "on rows it *did* see during its own training.  The v8.1 "
        "numbers are honest held-out; v8 numbers are upper-bound)."
    )
    w("")
    w("## 1. Per-cue-family F1 (span-exact, held-out)")
    w("")
    w(
        "| Family | v8 P | v8 R | v8 F1 | v8.1 P | v8.1 R | v8.1 F1 | Δ F1 | support |"
    )
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    f1_delta_sum = 0.0
    f1_delta_count = 0
    for fam in all_fams:
        v8p, v8r, v8f1, _ = v8_family.get(fam, (0.0, 0.0, 0.0, 0))
        v81p, v81r, v81f1, support = v81_family.get(fam, (0.0, 0.0, 0.0, 0))
        delta = v81f1 - v8f1
        if support > 0:
            f1_delta_sum += delta
            f1_delta_count += 1
        w(
            f"| {fam} | {v8p:.3f} | {v8r:.3f} | {v8f1:.3f} | "
            f"{v81p:.3f} | {v81r:.3f} | {v81f1:.3f} | "
            f"{delta:+.3f} | {support} |"
        )
    macro_delta = f1_delta_sum / f1_delta_count if f1_delta_count else 0.0
    w("")
    w(f"**Macro Δ F1 (v8.1 vs v8, over supported families): {macro_delta:+.3f}**")

    w("")
    w("## 2. Synthesizer hit rate on held-out")
    w("")
    w(f"| Version | Hit rate |")
    w(f"|---|---:|")
    w(f"| v8   | {v8_synth:.1%} |")
    w(f"| v8.1 | {v81_synth:.1%} |")
    delta = v81_synth - v8_synth
    w("")
    w(
        f"**Δ synthesizer hits: {delta:+.1%}**  "
        f"— how often the rule-first synthesizer composed a TLGQuery "
        f"from the cue-head output.  Higher = more queries routed "
        f"through grammar dispatch instead of pure hybrid retrieval."
    )

    w("")
    w("## 3. Content-head accuracy on held-out")
    w("")
    w(f"| Head | v8 | v8.1 | Δ |")
    w(f"|---|---:|---:|---:|")
    for head in sorted(set(v8_heads) | set(v81_heads)):
        if head == "synth_hit_rate":
            continue
        v8v = v8_heads.get(head, 0.0)
        v81v = v81_heads.get(head, 0.0)
        d = v81v - v8v
        w(f"| {head} | {v8v:.3f} | {v81v:.3f} | {d:+.3f} |")
    w("")
    w(
        "Regression guard: state_change / admission / intent / topic "
        "accuracy shouldn't drop materially vs v8.  The cue head got "
        "3× loss weight but the pooled heads share gradient with it; "
        "a drop here would mean the rank bump or loss weighting "
        "interfered with the other heads."
    )
    w("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"\nForensics report written to {out_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--heldout", type=Path,
        default=_REPO / "adapters/checkpoints/software_dev/v8.1/heldout_gold.jsonl",
    )
    p.add_argument(
        "--v8-adapter", type=Path,
        default=Path.home() / ".ncms/adapters/software_dev/v8",
    )
    p.add_argument(
        "--v8-1-adapter", type=Path,
        default=_REPO / "adapters/checkpoints/software_dev/v8.1",
    )
    p.add_argument(
        "--output", type=Path,
        default=_REPO / "docs/forensics/v8.1-ctlg-forensics.md",
    )
    args = p.parse_args()

    print("=" * 72)
    print("v8.1 CTLG FORENSICS — held-out per-cue-family F1 + synth hit rate")
    print("=" * 72)
    heldout = load_jsonl(args.heldout)
    heldout = [r for r in heldout if r.cue_tags]
    print(f"  held-out rows (with cue_tags): {len(heldout)}")

    v8_family, v8_synth, v8_heads = _eval_adapter(
        args.v8_adapter, heldout, "v8",
    )
    v81_family, v81_synth, v81_heads = _eval_adapter(
        args.v8_1_adapter, heldout, "v8.1",
    )

    _render_report(
        len(heldout),
        v8_family, v8_synth, v8_heads,
        v81_family, v81_synth, v81_heads,
        args.output,
    )


if __name__ == "__main__":
    main()
