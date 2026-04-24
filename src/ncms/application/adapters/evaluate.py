"""Evaluation harness — method × domain × split matrix.

Runs each method against each labelled set, computes intent F1
macro, slot F1 macro, joint accuracy, latency p50/p95, and the
confidently-wrong rate.  Writes a markdown matrix to
``results/matrix_<timestamp>.md`` plus a JSON dump suitable for
further analysis.

Usage::

    # Single method, single domain — fast smoke test.
    uv run python -m experiments.intent_slot_distillation.evaluate \\
        --methods e5_zero_shot --domain conversational

    # Full matrix.
    uv run python -m experiments.intent_slot_distillation.evaluate \\
        --methods e5_zero_shot,gliner_plus_e5 --domain all

    # Joint BERT requires a trained checkpoint.
    uv run python -m experiments.intent_slot_distillation.evaluate \\
        --methods joint_bert \\
        --joint-checkpoint-dir checkpoints/conversational \\
        --domain conversational
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Load .env (HF_TOKEN, device overrides) before any method imports
# pull in transformers/gliner/torch.  Noop outside a .env checkout.
try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover — experiment may run outside repo
    pass

from ncms.application.adapters.corpus.loader import (
    load_all,
    load_jsonl,
)
from ncms.application.adapters.methods.base import (
    IntentSlotExtractor,
)
from ncms.application.adapters.schemas import (
    ADMISSION_DECISIONS,
    DOMAINS,
    INTENT_CATEGORIES,
    STATE_CHANGES,
    Domain,
    ExtractedLabel,
    GoldExample,
    Intent,
    MethodResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_CORPUS_DIR = Path(__file__).parent / "corpus"
_DEFAULT_RESULTS_DIR = Path(__file__).parent / "results"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _macro_f1(
    predictions: list[str], gold: list[str], labels: list[str],
) -> tuple[float, dict[str, float]]:
    """Macro F1 across labels that have GOLD support.

    Labels with zero gold samples AND zero predictions are skipped
    (not counted as F1=0) — they contribute no signal.  A label with
    zero gold but non-zero predictions IS counted (all false
    positives → F1=0).

    This fixes the common "macro F1 artificially low because rare
    classes have no gold in the eval split" issue.  Per-label F1
    is still reported for every label in ``labels`` so callers see
    the full per-class breakdown.
    """
    per_label: dict[str, float] = {}
    supported: list[float] = []
    for label in labels:
        tp = sum(
            1 for p, g in zip(predictions, gold, strict=False)
            if p == label and g == label
        )
        fp = sum(
            1 for p, g in zip(predictions, gold, strict=False)
            if p == label and g != label
        )
        fn = sum(
            1 for p, g in zip(predictions, gold, strict=False)
            if p != label and g == label
        )
        gold_support = tp + fn  # number of gold occurrences of `label`
        pred_support = tp + fp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        per_label[label] = f1
        # Only include in macro when there's at least one gold
        # instance (so the label is actually part of the task here)
        # OR when the model emitted it (then it's measurable as FP).
        if gold_support > 0 or pred_support > 0:
            supported.append(f1)
    macro = sum(supported) / len(supported) if supported else 0.0
    return macro, per_label


def _slot_f1(
    predictions: list[dict[str, str]],
    gold: list[dict[str, str]],
) -> float:
    """Entity-level slot F1 — treats ``(slot_name, surface.lower())`` as
    the matching unit.  Both slot name and surface must agree.

    Rows with empty gold slots are **skipped** — their predictions
    carry no signal for slot F1 (there's nothing to match against),
    and counting their predicted slots as FP would double-punish
    query-voice rows that legitimately mention catalog entities
    without asserting role.  For those rows the role head's
    behaviour is audited separately (the role head's macro F1 on
    the held-out split catches role-level regressions).
    """
    tp = fp = fn = 0
    for pred, g in zip(predictions, gold, strict=False):
        gold_set = {(k, v.lower()) for k, v in g.items() if v}
        if not gold_set:
            continue  # no signal — see docstring
        pred_set = {(k, v.lower()) for k, v in pred.items() if v}
        tp += len(pred_set & gold_set)
        fp += len(pred_set - gold_set)
        fn += len(gold_set - pred_set)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return (2 * precision * recall / (precision + recall)
            if (precision + recall) else 0.0)


def _confidently_wrong_rate(
    predictions: list[ExtractedLabel],
    gold_intents: list[Intent],
    threshold: float = 0.7,
) -> float:
    """% of predictions with confidence ≥ threshold AND wrong intent."""
    if not predictions:
        return 0.0
    wrong_confident = sum(
        1 for p, g in zip(predictions, gold_intents, strict=False)
        if p.intent_confidence >= threshold and p.intent != g
    )
    return wrong_confident / len(predictions)


# ---------------------------------------------------------------------------
# Evaluation loop
# ---------------------------------------------------------------------------


def _evaluate_method_on_split(
    extractor: IntentSlotExtractor,
    examples: list[GoldExample],
    domain: Domain,
    split_label: str,
) -> MethodResult:
    timings_ms: list[float] = []
    predictions: list[ExtractedLabel] = []
    for ex in examples:
        t0 = time.perf_counter()
        try:
            pred = extractor.extract(ex.text, domain=domain)
        except Exception as exc:
            logger.warning(
                "[evaluate] method=%s raised on %r: %s",
                extractor.name, ex.text[:80], exc,
            )
            pred = ExtractedLabel(
                intent="none", intent_confidence=0.0, method=extractor.name,
            )
        timings_ms.append((time.perf_counter() - t0) * 1000)
        predictions.append(pred)

    pred_intents = [p.intent for p in predictions]
    gold_intents = [ex.intent for ex in examples]
    intent_macro, per_intent = _macro_f1(
        pred_intents, gold_intents, list(INTENT_CATEGORIES),
    )

    pred_slots = [p.slots for p in predictions]
    gold_slots = [ex.slots for ex in examples]
    slot_f1 = _slot_f1(pred_slots, gold_slots)

    joint_correct = sum(
        1 for pred, ex in zip(predictions, examples, strict=False)
        if pred.intent == ex.intent
        and {k: v.lower() for k, v in pred.slots.items() if v}
        == {k: v.lower() for k, v in ex.slots.items() if v}
    )
    joint_acc = joint_correct / len(examples) if examples else 0.0

    timings_sorted = sorted(timings_ms)
    p50 = timings_sorted[len(timings_sorted) // 2] if timings_sorted else 0.0
    p95_idx = max(0, int(len(timings_sorted) * 0.95) - 1)
    p95 = timings_sorted[p95_idx] if timings_sorted else 0.0

    # Multi-head scoring — only rows where both the prediction and the
    # gold carry a label contribute.  Produces ``None`` for a head
    # when either the gold has no labels for it (legacy corpus) or
    # the method doesn't produce it (zero-shot baselines).
    topic_f1, n_topic = _head_macro_f1(
        [p.topic for p in predictions],
        [ex.topic for ex in examples],
    )
    admission_f1, n_admit = _head_macro_f1(
        [p.admission for p in predictions],
        [ex.admission for ex in examples],
        label_vocab=list(ADMISSION_DECISIONS),
    )
    state_f1, n_state = _head_macro_f1(
        [p.state_change for p in predictions],
        [ex.state_change for ex in examples],
        label_vocab=list(STATE_CHANGES),
    )

    return MethodResult(
        method=extractor.name,
        domain=domain,
        split=split_label,  # type: ignore[arg-type]
        n_examples=len(examples),
        intent_f1_macro=round(intent_macro, 4),
        slot_f1_macro=round(slot_f1, 4),
        joint_accuracy=round(joint_acc, 4),
        latency_p50_ms=round(p50, 2),
        latency_p95_ms=round(p95, 2),
        confidently_wrong_rate=round(
            _confidently_wrong_rate(predictions, gold_intents), 4,
        ),
        per_intent_f1={
            intent: round(per_intent.get(intent, 0.0), 4)
            for intent in INTENT_CATEGORIES
        },
        topic_f1_macro=(
            round(topic_f1, 4) if topic_f1 is not None else None
        ),
        admission_f1_macro=(
            round(admission_f1, 4) if admission_f1 is not None else None
        ),
        state_change_f1_macro=(
            round(state_f1, 4) if state_f1 is not None else None
        ),
        n_topic_labeled=n_topic,
        n_admission_labeled=n_admit,
        n_state_change_labeled=n_state,
    )


def _head_macro_f1(
    predictions: list[str | None],
    gold: list[str | None],
    *,
    label_vocab: list[str] | None = None,
) -> tuple[float | None, int]:
    """Macro F1 for a multi-head classifier against optional labels.

    Only rows where both ``predictions[i]`` and ``gold[i]`` are not
    ``None`` contribute.  Returns ``(None, 0)`` when no paired labels
    exist — the head is effectively unscored against this split.

    ``label_vocab`` defaults to the set of non-None gold labels
    present (open-vocab for topic heads with user taxonomies); pass
    an explicit vocab for closed enums like admission / state_change
    to keep the denominator stable across runs.
    """
    pairs = [
        (p, g) for p, g in zip(predictions, gold, strict=False)
        if g is not None and p is not None
    ]
    if not pairs:
        return None, 0
    preds, gs = zip(*pairs, strict=True)
    vocab = label_vocab or sorted({*preds, *gs})
    macro, _ = _macro_f1(list(preds), list(gs), list(vocab))
    return macro, len(pairs)


# ---------------------------------------------------------------------------
# Method construction
# ---------------------------------------------------------------------------


def _build_method(
    name: str,
    *,
    joint_checkpoint_dir: Path | None = None,
    adapter_dir: Path | None = None,
) -> IntentSlotExtractor:
    if name == "e5_zero_shot":
        from experiments.intent_slot_distillation.methods.e5_zero_shot import (
            E5ZeroShot,
        )
        return E5ZeroShot()
    if name == "gliner_plus_e5":
        from experiments.intent_slot_distillation.methods.gliner_plus_e5 import (
            GlinerPlusE5,
        )
        return GlinerPlusE5()
    if name == "joint_bert":
        if joint_checkpoint_dir is None:
            raise ValueError(
                "--joint-checkpoint-dir required for method=joint_bert"
            )
        from experiments.intent_slot_distillation.methods.joint_bert import (
            JointBert,
        )
        return JointBert(joint_checkpoint_dir)
    if name == "joint_bert_lora":
        if adapter_dir is None:
            raise ValueError(
                "--adapter-dir required for method=joint_bert_lora"
            )
        from ncms.application.adapters.methods.joint_bert_lora import (
            LoraJointBert,
        )
        return LoraJointBert(adapter_dir)
    raise ValueError(f"unknown method {name!r}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _write_report(
    results: list[MethodResult], output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    md_path = output_dir / f"matrix_{stamp}.md"
    json_path = output_dir / f"matrix_{stamp}.json"

    json_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2),
    )

    lines = [
        "# Intent+Slot Evaluation Matrix",
        "",
        f"Timestamp: {datetime.now(UTC).isoformat()}",
        "",
        "## Core heads (intent + slot + joint)",
        "",
        (
            "| Method | Domain | Split | N | Intent F1 | Slot F1 "
            "| Joint acc | p50 ms | p95 ms | Conf-wrong % |"
        ),
        (
            "|:-------|:-------|:------|--:|---------:|--------:"
            "|---------:|-------:|-------:|-------------:|"
        ),
    ]
    for r in results:
        lines.append(
            f"| {r.method} | {r.domain} | {r.split} | {r.n_examples} "
            f"| {r.intent_f1_macro:.3f} | {r.slot_f1_macro:.3f} "
            f"| {r.joint_accuracy:.3f} "
            f"| {r.latency_p50_ms:.1f} | {r.latency_p95_ms:.1f} "
            f"| {r.confidently_wrong_rate * 100:.2f}% |"
        )

    # Multi-head table — only emit when at least one row has a
    # non-null multi-head metric, to keep the single-head matrix
    # readable for zero-shot-only runs.
    has_multi = any(
        r.topic_f1_macro is not None
        or r.admission_f1_macro is not None
        or r.state_change_f1_macro is not None
        for r in results
    )
    if has_multi:
        lines.extend([
            "",
            "## Multi-head (topic / admission / state_change)",
            "",
            (
                "| Method | Domain | Split "
                "| Topic F1 (N) | Admission F1 (N) | State-change F1 (N) |"
            ),
            (
                "|:-------|:-------|:------"
                "|-------------:|-----------------:|--------------------:|"
            ),
        ])
        for r in results:
            def _fmt(f1: float | None, n: int) -> str:
                if f1 is None or n == 0:
                    return "—"
                return f"{f1:.3f} ({n})"
            lines.append(
                f"| {r.method} | {r.domain} | {r.split} "
                f"| {_fmt(r.topic_f1_macro, r.n_topic_labeled)} "
                f"| {_fmt(r.admission_f1_macro, r.n_admission_labeled)} "
                f"| {_fmt(r.state_change_f1_macro, r.n_state_change_labeled)} |"
            )

    md_path.write_text("\n".join(lines) + "\n")
    return md_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_examples_for_split(
    domain: Domain, split: str, corpus_dir: Path,
) -> list[GoldExample]:
    """Return examples for ``domain`` filtered to ``split``.

    Adversarial examples live at corpus_dir parent ``../adversarial.jsonl``.
    """
    if split == "adversarial":
        path = corpus_dir.parent / "adversarial.jsonl"
        if not path.exists():
            return []
        return [ex for ex in load_jsonl(path) if ex.domain == domain]

    out = load_all(corpus_dir, split=split)
    return [ex for ex in out if ex.domain == domain]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Evaluate intent+slot methods across domains and splits",
    )
    parser.add_argument(
        "--methods",
        default="e5_zero_shot",
        help=(
            "Comma-separated method names "
            "(e5_zero_shot, gliner_plus_e5, joint_bert)."
        ),
    )
    parser.add_argument(
        "--domain",
        default="conversational",
        help="Domain name or 'all'.",
    )
    parser.add_argument(
        "--splits",
        default="gold",
        help="Comma-separated splits (gold, llm, sdg, adversarial) or 'all'.",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=_DEFAULT_CORPUS_DIR,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_RESULTS_DIR,
    )
    parser.add_argument(
        "--joint-checkpoint-dir",
        type=Path,
        help="Directory holding model.pt + config.json (joint_bert only).",
    )
    parser.add_argument(
        "--adapter-dir",
        type=Path,
        help=(
            "Adapter artifact directory (joint_bert_lora only): "
            "holds lora_adapter/ + heads.safetensors + manifest.json."
        ),
    )
    args = parser.parse_args()

    method_names = [m.strip() for m in args.methods.split(",") if m.strip()]
    domains: list[Domain]
    if args.domain == "all":
        domains = list(DOMAINS)
    else:
        if args.domain not in DOMAINS:
            parser.error(f"unknown domain {args.domain!r}")
        domains = [args.domain]  # type: ignore[list-item]
    if args.splits == "all":
        splits = ["gold", "llm", "sdg", "adversarial"]
    else:
        splits = [s.strip() for s in args.splits.split(",") if s.strip()]

    results: list[MethodResult] = []
    # Instantiate each method once (they can be heavy).
    method_cache: dict[str, IntentSlotExtractor] = {}
    for name in method_names:
        print(f"[evaluate] loading method {name}...")
        method_cache[name] = _build_method(
            name,
            joint_checkpoint_dir=args.joint_checkpoint_dir,
            adapter_dir=args.adapter_dir,
        )

    for method_name, extractor in method_cache.items():
        for domain in domains:
            for split in splits:
                examples = _load_examples_for_split(
                    domain, split, args.corpus_dir,
                )
                if not examples:
                    logger.info(
                        "[evaluate] method=%s domain=%s split=%s: empty — skipping",
                        method_name, domain, split,
                    )
                    continue
                print(
                    f"[evaluate] {method_name} × {domain} × {split}: "
                    f"{len(examples)} examples...",
                    flush=True,
                )
                result = _evaluate_method_on_split(
                    extractor, examples, domain, split,
                )
                results.append(result)
                print(
                    f"    intent_f1={result.intent_f1_macro:.3f}  "
                    f"slot_f1={result.slot_f1_macro:.3f}  "
                    f"joint={result.joint_accuracy:.3f}  "
                    f"p95={result.latency_p95_ms:.1f}ms"
                )

    md_path, json_path = _write_report(results, args.output_dir)
    print(f"[evaluate] wrote {md_path}")
    print(f"[evaluate] wrote {json_path}")


if __name__ == "__main__":
    main()
