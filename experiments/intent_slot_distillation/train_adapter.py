"""Sprint 3 — Four-phase train-adapter orchestrator.

Runs the full adapter lifecycle in one command::

    Phase 1 — Bootstrap
      Load existing gold + SDG + auto-label multi-head tags.
      (The research plan's GLiNER+E5 auto-labeller is a future
       milestone; Sprint 3 uses the already-shipped
       ``autolabel_multihead`` as the bootstrap primitive.)

    Phase 2 — Expand
      Template-SDG expansion to reach ``--target-size`` total.
      Topic / admission / state_change labels derived from the
      taxonomy YAML.

    Phase 3 — Adversarial
      Generate ``--adversarial-size`` pattern-based adversarial
      training examples covering seven failure modes.  Goes in
      corpus as a separate file with split="adversarial"; the
      held-out corpus/../adversarial.jsonl stays reserved for
      eval.

    Phase 4 — Train + Gate
      Train LoRA adapter on gold + sdg + adversarial_train.
      Evaluate on gold + held-out adversarial.  Write
      eval_report.md.  Exit code 1 when gate fails.

Usage::

    uv run python -m experiments.intent_slot_distillation.train_adapter \\
        --domain conversational \\
        --taxonomy experiments/intent_slot_distillation/taxonomies/conversational.yaml \\
        --adapter-dir experiments/intent_slot_distillation/adapters/conversational/v3 \\
        --target-size 500 \\
        --adversarial-size 200 \\
        --epochs 6 --lora-r 16

    # With baseline regression check:
    uv run python -m experiments.intent_slot_distillation.train_adapter \\
        --domain conversational \\
        --taxonomy ... \\
        --adapter-dir adapters/conversational/v4 \\
        --baseline-adapter-dir adapters/conversational/v3
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
import time
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from experiments.intent_slot_distillation.corpus.loader import (
    dump_jsonl,
    load_all,
)
from experiments.intent_slot_distillation.methods.joint_bert_lora import (
    build_manifest,
    train,
)
from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
)
from experiments.intent_slot_distillation.sdg.template_expander import (
    _dedupe,
    _load_object_to_topic,
    expand_domain,
)
from experiments.intent_slot_distillation.train_lora_adapter import (
    _corpus_hash,
)
from experiments.intent_slot_distillation.training.adversarial import (
    generate_adversarial,
)
from experiments.intent_slot_distillation.training.gate import (
    GateThresholds,
    _dump_outcome_json,
    run_gate,
    write_eval_report,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 — Bootstrap
# ---------------------------------------------------------------------------


def phase1_bootstrap(
    domain: Domain, corpus_dir: Path,
) -> list[GoldExample]:
    """Load all pre-existing gold rows for ``domain``.

    Returns both tagged (multi-head labels present) and untagged
    rows — the training loop's per-head masking handles mixed
    labelling gracefully.
    """
    gold = [ex for ex in load_all(corpus_dir, split="gold")
            if ex.domain == domain]
    logger.info(
        "[phase1] domain=%s gold_rows=%d", domain, len(gold),
    )
    return gold


# ---------------------------------------------------------------------------
# Phase 2 — SDG expansion
# ---------------------------------------------------------------------------


def phase2_expand(
    domain: Domain,
    *,
    target_size: int,
    seed: int,
    taxonomy_path: Path | None,
    output_path: Path,
) -> list[GoldExample]:
    """Run template-SDG expansion and write the result to disk.

    ``target_size`` is the pre-dedup target; deduped output may be
    ~20% smaller depending on vocabulary diversity.
    """
    object_to_topic = _load_object_to_topic(taxonomy_path)
    raw = expand_domain(
        domain,
        target=target_size,
        seed=seed,
        object_to_topic=object_to_topic,
    )
    deduped = _dedupe(raw)
    dump_jsonl(deduped, output_path)
    logger.info(
        "[phase2] domain=%s raw=%d deduped=%d → %s",
        domain, len(raw), len(deduped), output_path,
    )
    return deduped


# ---------------------------------------------------------------------------
# Phase 3 — Adversarial augmentation
# ---------------------------------------------------------------------------


def phase3_adversarial(
    domain: Domain,
    seeds: list[GoldExample],
    *,
    target: int,
    seed: int,
    output_path: Path,
) -> list[GoldExample]:
    """Generate pattern-based adversarial training rows.

    Filters seeds against the domain's primary-slot map so seeds
    without a recognised primary slot are skipped; seeds from all
    three domains (conversational ``object``, software_dev
    ``library``/``language``/…, clinical ``medication``/
    ``procedure``/…) now feed the generator.
    """
    from experiments.intent_slot_distillation.training.adversarial import (
        _primary,
    )
    seeds_with_primary = [s for s in seeds if _primary(s) is not None]
    if not seeds_with_primary:
        logger.warning(
            "[phase3] no seeds with a recognised primary slot — "
            "adversarial generation skipped for domain %s",
            domain,
        )
        return []
    out = generate_adversarial(
        seeds_with_primary, target=target, seed=seed,
    )
    dump_jsonl(out, output_path)
    modes: dict[str, int] = {}
    for ex in out:
        modes[ex.note or "?"] = modes.get(ex.note or "?", 0) + 1
    logger.info(
        "[phase3] domain=%s generated=%d modes=%s → %s",
        domain, len(out), modes, output_path,
    )
    return out


# ---------------------------------------------------------------------------
# Phase 4 — Fine-tune + gate
# ---------------------------------------------------------------------------


def phase4_finetune_and_gate(
    *,
    domain: Domain,
    gold: list[GoldExample],
    sdg: list[GoldExample],
    adversarial_train: list[GoldExample],
    adapter_dir: Path,
    taxonomy_path: Path | None,
    encoder: str,
    version: str,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    lora_r: int,
    lora_alpha: int,
    lora_dropout: float,
    lora_targets: list[str],
    max_length: int,
    corpus_dir: Path,
    thresholds: GateThresholds,
    baseline_adapter_dir: Path | None,
    device: str | None,
    eval_splits: list[str],
    gold_upsample: int = 1,
    adversarial_upsample: int = 1,
) -> bool:
    """Train the LoRA adapter, run the gate, persist report."""
    import yaml

    taxonomy: dict[str, list[str]] = {}
    if taxonomy_path is not None:
        data = yaml.safe_load(taxonomy_path.read_text()) or {}
        taxonomy = {
            "topic_labels": list(data.get("topic_labels") or []),
            "admission_labels": list(data.get("admission_labels") or []),
            "state_change_labels": list(data.get("state_change_labels") or []),
        }

    manifest = build_manifest(
        domain=domain,
        encoder=encoder,
        topic_labels=taxonomy.get("topic_labels"),
        admission_labels=(
            taxonomy["admission_labels"]
            if taxonomy.get("admission_labels") else None
        ),
        state_change_labels=(
            taxonomy["state_change_labels"]
            if taxonomy.get("state_change_labels") else None
        ),
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        lora_target_modules=lora_targets,
        max_length=max_length,
        version=version,
    )
    # Upsample — repeat gold / adversarial rows in the training mix
    # to counter SDG-dilution on the slot head.  Corpus hash uses the
    # un-upsampled rows so hash stability doesn't depend on mix
    # ratios, but the training loop sees the expanded set.
    hash_input = gold + sdg + adversarial_train
    combined = (
        gold * gold_upsample
        + sdg
        + adversarial_train * adversarial_upsample
    )
    manifest.corpus_hash = _corpus_hash(hash_input)
    manifest.trained_at = _dt.datetime.now(_dt.UTC).isoformat()

    logger.info(
        "[phase4] training — domain=%s n_raw=%d n_training=%d "
        "(gold×%d + sdg + adversarial×%d) topic_labels=%d slot_labels=%d "
        "epochs=%d lora_r=%d",
        domain, len(hash_input), len(combined),
        gold_upsample, adversarial_upsample,
        len(manifest.topic_labels), len(manifest.slot_labels),
        epochs, lora_r,
    )
    train(
        combined,
        domain=domain,
        adapter_dir=adapter_dir,
        manifest=manifest,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        device=device,
    )

    # Gate
    logger.info("[phase4] gate — running eval + threshold check...")
    outcome = run_gate(
        adapter_dir,
        domain=domain,
        corpus_dir=corpus_dir,
        eval_splits=eval_splits,
        thresholds=thresholds,
        baseline_adapter_dir=baseline_adapter_dir,
    )
    write_eval_report(
        outcome, thresholds, adapter_dir / "eval_report.md",
    )
    _dump_outcome_json(outcome, adapter_dir / "eval_outcome.json")

    verdict = "PASS" if outcome.passed else "FAIL"
    print(
        f"[train-adapter] gate {verdict} — adapter={adapter_dir}",
    )
    for r in outcome.failures:
        print(f"  FAIL: {r}")
    for r in outcome.warnings:
        print(f"  WARN: {r}")
    return outcome.passed


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Four-phase train-adapter pipeline: "
            "bootstrap → expand → adversarial → train + gate"
        ),
    )
    parser.add_argument("--domain", required=True, choices=list(DOMAINS))
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, default=None)
    parser.add_argument(
        "--corpus-dir", type=Path,
        default=Path(__file__).parent / "corpus",
    )
    parser.add_argument("--target-size", type=int, default=500,
                        help="SDG pre-dedup target (phase 2).")
    parser.add_argument("--adversarial-size", type=int, default=200,
                        help="Adversarial training examples (phase 3).")
    parser.add_argument(
        "--sdg-output", type=Path, default=None,
        help="SDG JSONL path (default: corpus/sdg_<domain>.jsonl).",
    )
    parser.add_argument(
        "--adversarial-output", type=Path, default=None,
        help=(
            "Adversarial training JSONL path "
            "(default: corpus/adversarial_train_<domain>.jsonl)."
        ),
    )
    parser.add_argument("--seed", type=int, default=42)

    # Training hyperparameters
    parser.add_argument("--encoder", default="bert-base-uncased")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--epochs", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--lora-targets", default="query,value")
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--device", default=None)

    # Gate thresholds
    parser.add_argument("--intent-f1-min", type=float, default=0.70)
    parser.add_argument("--slot-f1-min", type=float, default=0.75)
    parser.add_argument("--conf-wrong-max", type=float, default=0.10)
    parser.add_argument("--regression-tolerance", type=float, default=0.02)
    parser.add_argument("--latency-p95-soft-limit", type=float, default=200.0)
    parser.add_argument(
        "--eval-splits", default="gold,adversarial",
        help="Comma-separated eval splits for the gate.",
    )
    parser.add_argument(
        "--baseline-adapter-dir", type=Path, default=None,
        help="Optional baseline for regression comparison.",
    )
    parser.add_argument(
        "--skip-adversarial", action="store_true",
        help="Skip phase 3 (useful for smoke tests).",
    )
    parser.add_argument(
        "--gold-upsample", type=int, default=1,
        help=(
            "How many times to repeat gold rows in the training mix.  "
            "Counteracts SDG-dilution on slot F1 when the SDG corpus is "
            "much larger than gold.  Default 1 (no upsampling); typical "
            "values 5-10 for small gold + large SDG."
        ),
    )
    parser.add_argument(
        "--adversarial-upsample", type=int, default=1,
        help="Upsample factor for adversarial training rows.",
    )
    args = parser.parse_args()

    sdg_output = args.sdg_output or (
        args.corpus_dir / f"sdg_{args.domain}.jsonl"
    )
    adversarial_output = args.adversarial_output or (
        args.corpus_dir / f"adversarial_train_{args.domain}.jsonl"
    )

    t0 = time.perf_counter()

    # Phase 1
    gold = phase1_bootstrap(args.domain, args.corpus_dir)
    if not gold:
        parser.error(f"no gold rows for domain={args.domain!r}")

    # Phase 2
    sdg = phase2_expand(
        args.domain,
        target_size=args.target_size,
        seed=args.seed,
        taxonomy_path=args.taxonomy,
        output_path=sdg_output,
    )

    # Phase 3
    adversarial: list[GoldExample] = []
    if not args.skip_adversarial:
        adversarial = phase3_adversarial(
            args.domain,
            seeds=gold + sdg,
            target=args.adversarial_size,
            seed=args.seed,
            output_path=adversarial_output,
        )

    thresholds = GateThresholds(
        intent_f1_min=args.intent_f1_min,
        slot_f1_min=args.slot_f1_min,
        confidently_wrong_max=args.conf_wrong_max,
        regression_tolerance=args.regression_tolerance,
        latency_p95_ms_soft_limit=args.latency_p95_soft_limit,
    )
    eval_splits = [
        s.strip() for s in args.eval_splits.split(",") if s.strip()
    ]

    # Phase 4
    passed = phase4_finetune_and_gate(
        domain=args.domain,  # type: ignore[arg-type]
        gold=gold,
        sdg=sdg,
        adversarial_train=adversarial,
        adapter_dir=args.adapter_dir,
        taxonomy_path=args.taxonomy,
        encoder=args.encoder,
        version=args.version,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        lora_targets=[
            t.strip() for t in args.lora_targets.split(",") if t.strip()
        ],
        max_length=args.max_length,
        corpus_dir=args.corpus_dir,
        thresholds=thresholds,
        gold_upsample=args.gold_upsample,
        adversarial_upsample=args.adversarial_upsample,
        baseline_adapter_dir=args.baseline_adapter_dir,
        device=args.device,
        eval_splits=eval_splits,
    )

    elapsed = time.perf_counter() - t0
    print(
        f"[train-adapter] total elapsed {elapsed:.1f}s  "
        f"verdict={'PASS' if passed else 'FAIL'}",
    )
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
