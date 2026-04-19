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
import statistics
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from experiments.intent_slot_distillation.corpus.loader import (
    load_all,
    load_jsonl,
)
from experiments.intent_slot_distillation.methods.base import (
    IntentSlotExtractor,
)
from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    INTENT_CATEGORIES,
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
    """Macro F1 across ``labels``; returns (macro, per-label)."""
    per_label: dict[str, float] = {}
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
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        per_label[label] = f1
    macro = sum(per_label.values()) / len(labels) if labels else 0.0
    return macro, per_label


def _slot_f1(
    predictions: list[dict[str, str]],
    gold: list[dict[str, str]],
) -> float:
    """Entity-level slot F1 — treats ``(slot_name, surface.lower())`` as
    the matching unit.  Both slot name and surface must agree.
    """
    tp = fp = fn = 0
    for pred, g in zip(predictions, gold, strict=False):
        pred_set = {(k, v.lower()) for k, v in pred.items() if v}
        gold_set = {(k, v.lower()) for k, v in g.items() if v}
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
    )


# ---------------------------------------------------------------------------
# Method construction
# ---------------------------------------------------------------------------


def _build_method(
    name: str,
    *,
    joint_checkpoint_dir: Path | None = None,
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
        "| Method | Domain | Split | N | Intent F1 | Slot F1 | Joint acc | p50 ms | p95 ms | Conf-wrong % |",
        "|:-------|:-------|:------|--:|---------:|--------:|---------:|-------:|-------:|-------------:|",
    ]
    for r in results:
        lines.append(
            f"| {r.method} | {r.domain} | {r.split} | {r.n_examples} "
            f"| {r.intent_f1_macro:.3f} | {r.slot_f1_macro:.3f} "
            f"| {r.joint_accuracy:.3f} "
            f"| {r.latency_p50_ms:.1f} | {r.latency_p95_ms:.1f} "
            f"| {r.confidently_wrong_rate * 100:.2f}% |"
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
            name, joint_checkpoint_dir=args.joint_checkpoint_dir,
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
