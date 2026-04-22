"""Per-head quality audit — factual correctness, not just confidence.

For each SLM head we want to answer:
  - How often does it fire vs abstain?
  - When it fires confidently, is the label RIGHT?
  - What are the most common mistakes?

Inputs: the ingest trace JSONL we produced after the fix, plus
predictions.jsonl (has query-side head_outputs from d353010).

Ground-truth proxies (no human labels available yet — these are
heuristic oracles, acknowledged imperfect):

  - admission  → expected "persist" on gold-curated MSEB corpus.
                 Any "ephemeral"/"discard" is a miss to flag.
  - state_change → expected "declaration" when content contains
                   "decided to" / "we chose" / "adopt" / "use",
                   "retirement" when "replaced by"/"deprecated"/
                   "removed", "none" otherwise.  Heuristic only —
                   SLM may reasonably disagree.
  - topic      → expected to relate to the subject slug's tech area.
                 For softwaredev, subject slugs contain hints
                 (react/vue/mysql/docker → framework/database/infra).
                 Report per-subject topic distribution.
  - intent     → on INGEST content, expected "none" (intent is a
                 user-side preference signal, rarely appears in
                 ADR prose).
  - slots      → if present, the slot surface form should appear in
                 the content (else the SLM hallucinated).
  - shape_intent (query-side) → compare to gold_locked.yaml shape.

For each head we print:
  - calibration: confidence distribution
  - oracle agreement: % of confident predictions that match the
    heuristic oracle
  - top-5 examples where SLM + oracle disagree (for manual review)

Domain-parameterised: run with --domain softwaredev (default) or
any other if we've dumped a trace for it.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path("/Users/shawnmccarthy/ncms")


# ── Heuristic oracles ──────────────────────────────────────────────────

DECLARATION_PATTERNS = [
    r"\bwe (?:have )?decided to\b",
    r"\bwe (?:have )?chose\b",
    r"\bwe (?:will )?use\b",
    r"\bwe (?:have )?adopted?\b",
    r"\bwe (?:have )?picked\b",
    r"\bdecision to (?:adopt|use|choose|pick)\b",
    r"\bdecided on\b",
    r"\bthe decision(?: was|: |\.)",
]
RETIREMENT_PATTERNS = [
    r"\bdeprecated\b",
    r"\breplaced by\b",
    r"\bretired\b",
    r"\bno longer use\b",
    r"\bmoved away from\b",
    r"\bmigrate[ds]?\s+(?:from|away)\b",
]


def classify_state_oracle(content: str) -> str:
    c = content.lower()
    for p in RETIREMENT_PATTERNS:
        if re.search(p, c):
            return "retirement"
    for p in DECLARATION_PATTERNS:
        if re.search(p, c):
            return "declaration"
    return "none"


def slot_in_content(slot_value: str, content: str) -> bool:
    """True if the surface form appears (case-insensitive) in content."""
    if not slot_value:
        return False
    return slot_value.lower() in content.lower()


# ── Head audits ────────────────────────────────────────────────────────

def audit_admission(records: list[dict]) -> None:
    print("── admission head ──")
    vals = Counter()
    confs = []
    for r in records:
        slm = r["slm"]
        vals[slm.get("admission")] += 1
        if slm.get("admission_conf") is not None:
            confs.append(float(slm["admission_conf"]))
    print(f"  decisions: {dict(vals)}")
    if confs:
        confs.sort()
        print(f"  confidence  min={confs[0]:.3f}  "
              f"median={confs[len(confs)//2]:.3f}  "
              f"max={confs[-1]:.3f}")

    # Gold curated → "persist" for every memory.  Anything else is a
    # false negative.
    misses = [r for r in records if r["slm"].get("admission") != "persist"]
    print(f"  gold-curated misses (non-persist): {len(misses)}")
    for m in misses[:5]:
        print(f"    mid={m['mid']}  decision="
              f"{m['slm']['admission']!r} conf={m['slm']['admission_conf']}")
        print(f"      content[:120]: {m['content_head'][:120]!r}")


def audit_state_change(records: list[dict]) -> None:
    print("── state_change head ──")
    vals = Counter()
    confs = []
    agreements = Counter()  # (slm, oracle) → count
    for r in records:
        slm = r["slm"]
        vals[slm.get("state_change")] += 1
        if slm.get("state_change_conf") is not None:
            confs.append(float(slm["state_change_conf"]))
        slm_v = slm.get("state_change") or "none"
        oracle_v = classify_state_oracle(r["content_head"])
        agreements[(slm_v, oracle_v)] += 1
    print(f"  SLM decisions: {dict(vals)}")
    if confs:
        confs.sort()
        print(f"  confidence  min={confs[0]:.3f}  "
              f"median={confs[len(confs)//2]:.3f}  "
              f"max={confs[-1]:.3f}")
    print(f"  SLM vs heuristic-oracle agreement matrix:")
    for (s, o), n in sorted(agreements.items()):
        agree = "✓" if s == o else "✗"
        print(f"    slm={s!r:15}  oracle={o!r:15}  n={n:4d}  {agree}")

    # Spotlight: content clearly declaration but SLM says none
    print(f"\n  cases where heuristic=declaration but SLM=none:")
    seen = 0
    for r in records:
        slm_v = r["slm"].get("state_change") or "none"
        oracle_v = classify_state_oracle(r["content_head"])
        if oracle_v == "declaration" and slm_v == "none" and seen < 5:
            print(f"    mid={r['mid']}  SLM=none (conf={r['slm']['state_change_conf']})")
            print(f"      content[:120]: {r['content_head'][:120]!r}")
            seen += 1


def audit_topic(records: list[dict]) -> None:
    print("── topic head ──")
    # Distribution
    vals = Counter()
    confs = []
    for r in records:
        slm = r["slm"]
        vals[slm.get("topic")] += 1
        if slm.get("topic_conf") is not None:
            confs.append(float(slm["topic_conf"]))
    print(f"  topic distribution: {dict(vals)}")
    if confs:
        confs.sort()
        print(f"  confidence (non-None only)  "
              f"min={confs[0]:.3f}  "
              f"median={confs[len(confs)//2]:.3f}  "
              f"max={confs[-1]:.3f}")

    # Subject-slug hints vs topic head output
    #   infra ADRs      (docker/swarm/postgres/mysql/secrets/cloud)    → infra
    #   framework       (django/flask/sveltekit/rails)                 → framework
    #   language        (python/rust/go-programming)                   → language_runtime
    #   tooling         (timestamp-format/env-var/api-json)            → tooling
    infra_hints = ("docker", "swarm", "postgres", "mysql", "secrets",
                   "cloud", "gcp", "kafka", "redis", "rabbit", "database")
    framework_hints = ("django", "flask", "svelte", "rails", "angular",
                       "react", "vue", "next", "tailwind", "bulma", "css-framework")
    lang_hints = ("python-programming", "rust-programming", "go-programming",
                  "typescript", "javascript-language", "language")
    testing_hints = ("playwright", "cypress", "selenium", "testing",
                     "e2e", "vitest", "jest")
    def hint_domain(mid: str) -> str:
        m = mid.lower()
        if any(h in m for h in testing_hints):
            return "testing"
        if any(h in m for h in lang_hints):
            return "language_runtime"
        if any(h in m for h in framework_hints):
            return "framework"
        if any(h in m for h in infra_hints):
            return "infra"
        return "(other)"
    agreements = Counter()
    for r in records:
        slm_topic = r["slm"].get("topic") or "(none)"
        slug_hint = hint_domain(r["mid"])
        agreements[(slm_topic, slug_hint)] += 1
    print(f"  SLM topic vs slug-hint agreement matrix:")
    for (s, h), n in sorted(agreements.items()):
        print(f"    slm={s!r:20}  slug_hint={h!r:20}  n={n:4d}")


def audit_intent(records: list[dict]) -> None:
    print("── intent head (ingest-voice) ──")
    vals = Counter()
    confs = []
    for r in records:
        slm = r["slm"]
        vals[slm.get("intent")] += 1
        if slm.get("intent_conf") is not None:
            confs.append(float(slm["intent_conf"]))
    print(f"  intent distribution: {dict(vals)}")
    if confs:
        confs.sort()
        print(f"  confidence  min={confs[0]:.3f}  "
              f"median={confs[len(confs)//2]:.3f}  "
              f"max={confs[-1]:.3f}")
    # Ingest content should be mostly intent='none'
    non_none = [r for r in records if r["slm"].get("intent") != "none"]
    print(f"  ingest-time intent != 'none' (rare expected): {len(non_none)}")
    for m in non_none[:5]:
        print(f"    mid={m['mid']}  intent="
              f"{m['slm']['intent']!r} conf={m['slm']['intent_conf']}")
        print(f"      content[:120]: {m['content_head'][:120]!r}")


def audit_slots(records: list[dict]) -> None:
    print("── slot head (typed surface forms) ──")
    total_slots = 0
    hallucinated = 0
    slot_types = Counter()
    examples = []
    for r in records:
        slots = r.get("memory_slots") or {}
        for label, surface in slots.items():
            total_slots += 1
            slot_types[label] += 1
            if not slot_in_content(surface, r["content_head"]):
                hallucinated += 1
                if len(examples) < 8:
                    examples.append({
                        "mid": r["mid"],
                        "label": label,
                        "surface": surface,
                        "content_preview": r["content_head"][:120],
                    })
    print(f"  total slots extracted: {total_slots}")
    print(f"  slot-type distribution: {dict(slot_types)}")
    print(f"  surface NOT found in content (hallucination count): "
          f"{hallucinated} / {total_slots}  "
          f"({(hallucinated/total_slots*100 if total_slots else 0):.1f}%)")
    for ex in examples[:5]:
        print(f"    mid={ex['mid']}  slot={ex['label']}:{ex['surface']!r}")
        print(f"      content[:120]: {ex['content_preview']!r}")


def audit_shape_intent(predictions_path: Path, gold_path: Path) -> None:
    print("── shape_intent head (query-voice) ──")
    preds = {}
    with predictions_path.open() as f:
        for line in f:
            d = json.loads(line)
            preds[d["qid"]] = d.get("head_outputs") or {}
    gold = {g["qid"]: g for g in yaml.safe_load(gold_path.read_text())}

    # Confusion matrix: gold shape × SLM shape_intent
    cm: dict[tuple[str, str], int] = Counter()
    per_shape = Counter()
    hi_conf_correct = 0
    hi_conf_wrong = 0
    low_conf = 0
    for qid, heads in preds.items():
        g = gold.get(qid) or {}
        gold_shape = g.get("shape", "?")
        slm_shape = heads.get("shape_intent") or "(none)"
        slm_conf = heads.get("shape_intent_conf") or 0.0
        if gold_shape == "noise":
            # MSEB gold uses "noise"; SLM labels may use "none".
            slm_norm = "none" if slm_shape == "(none)" else slm_shape
            target = "none"
        else:
            slm_norm = slm_shape
            target = gold_shape
        cm[(target, slm_norm)] += 1
        per_shape[target] += 1
        if slm_conf >= 0.7:
            if slm_norm == target:
                hi_conf_correct += 1
            else:
                hi_conf_wrong += 1
        else:
            low_conf += 1
    total = sum(per_shape.values())
    print(f"  queries total: {total}")
    print(f"  high-conf (≥0.7) correct: {hi_conf_correct}  "
          f"({(hi_conf_correct/total*100):.1f}%)")
    print(f"  high-conf (≥0.7) wrong:   {hi_conf_wrong}  "
          f"({(hi_conf_wrong/total*100):.1f}%)")
    print(f"  low-conf / abstain:        {low_conf}  "
          f"({(low_conf/total*100):.1f}%)")
    print(f"  per-shape recall (SLM gets gold shape at high conf):")
    for shape in sorted(per_shape):
        n = per_shape[shape]
        got = cm.get((shape, shape), 0)
        print(f"    {shape:20}  {got:4d}/{n:4d}  ({(got/n*100):.1f}%)")

    # Confusion: most common wrong predictions
    print(f"\n  Top confusion pairs (gold → SLM, wrong predictions):")
    wrong = [(k, v) for k, v in cm.items() if k[0] != k[1]]
    wrong.sort(key=lambda x: -x[1])
    for (gold_s, slm_s), n in wrong[:8]:
        print(f"    {gold_s:20} → {slm_s:20}  n={n}")


# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    domain = sys.argv[1] if len(sys.argv) > 1 else "softwaredev"
    ingest_jsonl = (
        ROOT / f"benchmarks/results/audit/softwaredev_ingest_trace.jsonl"
    )
    preds_jsonl = sorted(
        (ROOT / "benchmarks/results/mseb/main12").glob(
            f"main_{domain}_ncms_temporal-on_*.predictions.jsonl"
        )
    )[-1]
    gold_yaml = ROOT / f"benchmarks/mseb_{domain}/gold_locked.yaml"

    records = [json.loads(l) for l in ingest_jsonl.open()]
    print(f"domain: {domain}  ingest records: {len(records)}  "
          f"predictions: {preds_jsonl.name}")
    print()

    audit_admission(records)
    print()
    audit_state_change(records)
    print()
    audit_topic(records)
    print()
    audit_intent(records)
    print()
    audit_slots(records)
    print()
    audit_shape_intent(preds_jsonl, gold_yaml)


if __name__ == "__main__":
    main()
