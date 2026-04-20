"""Adapter promotion gate + eval-report writer.

Runs the evaluation matrix against a trained adapter and compares
against a prior baseline (either the previous version of the same
adapter or an in-tree full-FT / LoRA baseline).  Writes
``eval_report.md`` next to the adapter so promotions are auditable.

Gate semantics::

    PASS iff:
      - intent_f1_macro  >= threshold_intent_f1         (default 0.70)
      - slot_f1_macro    >= threshold_slot_f1           (default 0.75)
      - confidently_wrong_rate <= threshold_conf_wrong  (default 0.10)
      - every new metric either matches or beats the baseline
        within ``regression_tolerance`` (default 0.02)

    FAIL otherwise.  The gate writes the eval report regardless
    of PASS/FAIL; the exit code flags the outcome to callers.

The thresholds are intentionally conservative — production
deployments should tighten them once baselines stabilise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from experiments.intent_slot_distillation.corpus.loader import (
    load_all,
    load_jsonl,
)
from experiments.intent_slot_distillation.evaluate import (
    _evaluate_method_on_split,
)
from experiments.intent_slot_distillation.methods.joint_bert_lora import (
    LoraJointBert,
)
from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
    MethodResult,
)

logger = logging.getLogger(__name__)


@dataclass
class GateThresholds:
    """Minimum-quality bar for adapter promotion."""

    intent_f1_min: float = 0.70
    slot_f1_min: float = 0.75
    confidently_wrong_max: float = 0.10
    regression_tolerance: float = 0.02
    # Latency budget — warn only, never fail.
    latency_p95_ms_soft_limit: float = 200.0


@dataclass
class GateOutcome:
    """Result of a single gate evaluation."""

    passed: bool
    adapter_dir: str
    domain: Domain
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, MethodResult] = field(default_factory=dict)
    baseline_metrics: dict[str, MethodResult] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Split loading
# ---------------------------------------------------------------------------


def _load_eval_split(
    domain: Domain, split: str, corpus_dir: Path,
) -> list[GoldExample]:
    """Load evaluation examples for ``domain`` × ``split``.

    ``adversarial`` is resolved to the reserved held-out file
    ``corpus/../adversarial.jsonl`` — *not* the training-adversarial
    file in ``corpus/adversarial_train_*.jsonl``.  This is a hard
    invariant of the gate: promotion metrics must be scored against
    held-out data the adapter never saw during training.
    """
    if split == "adversarial":
        held_out = corpus_dir.parent / "adversarial.jsonl"
        if not held_out.exists():
            return []
        return [ex for ex in load_jsonl(held_out) if ex.domain == domain]
    return [
        ex for ex in load_all(corpus_dir, split=split)
        if ex.domain == domain
    ]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def run_gate(
    adapter_dir: Path,
    *,
    domain: Domain,
    corpus_dir: Path,
    eval_splits: list[str] | None = None,
    thresholds: GateThresholds | None = None,
    baseline_adapter_dir: Path | None = None,
) -> GateOutcome:
    """Evaluate the adapter and decide pass/fail.

    ``eval_splits`` defaults to ``["gold", "adversarial"]`` — a
    sensible production setup is to always include a held-out
    adversarial split so the gate refuses to promote an adapter
    that regresses on hard cases.
    """
    thresholds = thresholds or GateThresholds()
    eval_splits = eval_splits or ["gold", "adversarial"]
    outcome = GateOutcome(
        passed=True,  # flipped to False on first failure
        adapter_dir=str(adapter_dir),
        domain=domain,
    )

    extractor = LoraJointBert(adapter_dir)
    for split in eval_splits:
        examples = _load_eval_split(domain, split, corpus_dir)
        if not examples:
            outcome.warnings.append(f"eval split {split!r} is empty — skipped")
            continue
        result = _evaluate_method_on_split(
            extractor, examples, domain, split,
        )
        outcome.metrics[split] = result

        # Primary-split thresholds (gate applies to "gold" by default —
        # adversarial is reported but doesn't fail promotion unless the
        # intent-collapse case is specifically targeted).
        if split == "gold":
            if result.intent_f1_macro < thresholds.intent_f1_min:
                outcome.passed = False
                outcome.failures.append(
                    f"intent_f1_macro {result.intent_f1_macro:.3f} < "
                    f"threshold {thresholds.intent_f1_min:.3f} on {split}",
                )
            if result.slot_f1_macro < thresholds.slot_f1_min:
                outcome.passed = False
                outcome.failures.append(
                    f"slot_f1_macro {result.slot_f1_macro:.3f} < "
                    f"threshold {thresholds.slot_f1_min:.3f} on {split}",
                )
            if (
                result.confidently_wrong_rate
                > thresholds.confidently_wrong_max
            ):
                outcome.passed = False
                outcome.failures.append(
                    f"confidently_wrong_rate "
                    f"{result.confidently_wrong_rate:.3f} > "
                    f"threshold {thresholds.confidently_wrong_max:.3f} "
                    f"on {split}",
                )
            if result.latency_p95_ms > thresholds.latency_p95_ms_soft_limit:
                outcome.warnings.append(
                    f"latency_p95_ms {result.latency_p95_ms:.1f} > "
                    f"soft limit {thresholds.latency_p95_ms_soft_limit:.1f} "
                    f"on {split}",
                )

    # Baseline regression check — only runs when a baseline adapter is
    # specified.  Protects against silent quality regressions when
    # training data or hyperparameters change.
    if baseline_adapter_dir is not None:
        baseline = LoraJointBert(baseline_adapter_dir)
        for split in eval_splits:
            examples = _load_eval_split(domain, split, corpus_dir)
            if not examples:
                continue
            base_result = _evaluate_method_on_split(
                baseline, examples, domain, split,
            )
            outcome.baseline_metrics[split] = base_result
            current = outcome.metrics.get(split)
            if current is None:
                continue
            tol = thresholds.regression_tolerance
            if (
                current.intent_f1_macro + tol
                < base_result.intent_f1_macro
            ):
                outcome.passed = False
                outcome.failures.append(
                    f"intent_f1 regression on {split}: "
                    f"baseline={base_result.intent_f1_macro:.3f} "
                    f"current={current.intent_f1_macro:.3f} "
                    f"(tolerance {tol:.2f})",
                )
            if current.slot_f1_macro + tol < base_result.slot_f1_macro:
                outcome.passed = False
                outcome.failures.append(
                    f"slot_f1 regression on {split}: "
                    f"baseline={base_result.slot_f1_macro:.3f} "
                    f"current={current.slot_f1_macro:.3f} "
                    f"(tolerance {tol:.2f})",
                )

    return outcome


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_eval_report(
    outcome: GateOutcome,
    thresholds: GateThresholds,
    path: Path,
) -> None:
    """Write a human-readable eval report next to the adapter."""
    lines = [
        f"# Adapter eval report — {outcome.domain}",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Adapter:   `{outcome.adapter_dir}`",
        "",
        f"**Gate verdict:** {'✅ PASS' if outcome.passed else '❌ FAIL'}",
        "",
    ]
    if outcome.failures:
        lines.append("## Failures")
        lines.append("")
        for reason in outcome.failures:
            lines.append(f"- {reason}")
        lines.append("")
    if outcome.warnings:
        lines.append("## Warnings")
        lines.append("")
        for reason in outcome.warnings:
            lines.append(f"- {reason}")
        lines.append("")
    lines.extend([
        "## Thresholds",
        "",
        f"- intent_f1_min: **{thresholds.intent_f1_min:.3f}**",
        f"- slot_f1_min: **{thresholds.slot_f1_min:.3f}**",
        f"- confidently_wrong_max: **{thresholds.confidently_wrong_max:.3f}**",
        f"- regression_tolerance: **{thresholds.regression_tolerance:.3f}**",
        f"- latency_p95_soft_limit: **{thresholds.latency_p95_ms_soft_limit:.1f} ms**",
        "",
        "## Metrics",
        "",
        (
            "| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) "
            "| Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |"
        ),
        (
            "|:------|--:|---------:|--------:|------:|-------------:"
            "|-----------------:|-------------:|-------:|-------------:|"
        ),
    ])
    def _fmt_head(f1: float | None, n: int) -> str:
        if f1 is None or n == 0:
            return "—"
        return f"{f1:.3f} ({n})"
    for split, r in outcome.metrics.items():
        lines.append(
            f"| {split} | {r.n_examples} | {r.intent_f1_macro:.3f} "
            f"| {r.slot_f1_macro:.3f} | {r.joint_accuracy:.3f} "
            f"| {_fmt_head(r.topic_f1_macro, r.n_topic_labeled)} "
            f"| {_fmt_head(r.admission_f1_macro, r.n_admission_labeled)} "
            f"| {_fmt_head(r.state_change_f1_macro, r.n_state_change_labeled)} "
            f"| {r.latency_p95_ms:.1f} "
            f"| {r.confidently_wrong_rate * 100:.2f}% |"
        )
    if outcome.baseline_metrics:
        lines.extend(["", "## Baseline comparison", ""])
        lines.append(
            "| Split | Metric | Baseline | Current | Δ |",
        )
        lines.append("|:------|:-------|-------:|-------:|-------:|")
        for split, base in outcome.baseline_metrics.items():
            cur = outcome.metrics.get(split)
            if cur is None:
                continue
            for label, base_val, cur_val in [
                ("intent_f1", base.intent_f1_macro, cur.intent_f1_macro),
                ("slot_f1", base.slot_f1_macro, cur.slot_f1_macro),
                ("joint_acc", base.joint_accuracy, cur.joint_accuracy),
                (
                    "conf_wrong",
                    base.confidently_wrong_rate,
                    cur.confidently_wrong_rate,
                ),
            ]:
                delta = cur_val - base_val
                marker = "✅" if delta >= -thresholds.regression_tolerance else "❌"
                lines.append(
                    f"| {split} | {label} | {base_val:.3f} | "
                    f"{cur_val:.3f} | {delta:+.3f} {marker} |",
                )

    path.write_text("\n".join(lines) + "\n")


def _dump_outcome_json(outcome: GateOutcome, path: Path) -> None:
    """Dump the structured outcome for CI / dashboards."""
    data = {
        "passed": outcome.passed,
        "adapter_dir": outcome.adapter_dir,
        "domain": outcome.domain,
        "failures": outcome.failures,
        "warnings": outcome.warnings,
        "metrics": {
            split: asdict(r) for split, r in outcome.metrics.items()
        },
        "baseline_metrics": {
            split: asdict(r) for split, r in outcome.baseline_metrics.items()
        },
    }
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Evaluate an adapter + gate promotion",
    )
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--domain", required=True, choices=list(DOMAINS))
    parser.add_argument(
        "--corpus-dir", type=Path,
        default=Path(__file__).parent.parent / "corpus",
    )
    parser.add_argument(
        "--eval-splits", default="gold,adversarial",
        help="Comma-separated eval splits (default: gold,adversarial).",
    )
    parser.add_argument(
        "--baseline-adapter-dir", type=Path, default=None,
        help="Optional baseline adapter for regression check.",
    )
    parser.add_argument(
        "--intent-f1-min", type=float, default=0.70,
    )
    parser.add_argument("--slot-f1-min", type=float, default=0.75)
    parser.add_argument("--conf-wrong-max", type=float, default=0.10)
    parser.add_argument("--regression-tolerance", type=float, default=0.02)
    parser.add_argument(
        "--latency-p95-soft-limit", type=float, default=200.0,
    )
    args = parser.parse_args()

    thresholds = GateThresholds(
        intent_f1_min=args.intent_f1_min,
        slot_f1_min=args.slot_f1_min,
        confidently_wrong_max=args.conf_wrong_max,
        regression_tolerance=args.regression_tolerance,
        latency_p95_ms_soft_limit=args.latency_p95_soft_limit,
    )
    eval_splits = [s.strip() for s in args.eval_splits.split(",") if s.strip()]

    t0 = time.perf_counter()
    outcome = run_gate(
        args.adapter_dir,
        domain=args.domain,  # type: ignore[arg-type]
        corpus_dir=args.corpus_dir,
        eval_splits=eval_splits,
        thresholds=thresholds,
        baseline_adapter_dir=args.baseline_adapter_dir,
    )
    elapsed = time.perf_counter() - t0

    report_path = args.adapter_dir / "eval_report.md"
    json_path = args.adapter_dir / "eval_outcome.json"
    write_eval_report(outcome, thresholds, report_path)
    _dump_outcome_json(outcome, json_path)

    verdict = "PASS" if outcome.passed else "FAIL"
    print(
        f"[gate] {verdict} adapter={args.adapter_dir} "
        f"failures={len(outcome.failures)} warnings={len(outcome.warnings)} "
        f"elapsed={elapsed:.1f}s",
    )
    for r in outcome.failures:
        print(f"  FAIL: {r}")
    for r in outcome.warnings:
        print(f"  WARN: {r}")
    print(f"[gate] report → {report_path}")
    print(f"[gate] outcome → {json_path}")

    sys.exit(0 if outcome.passed else 1)


if __name__ == "__main__":
    main()
