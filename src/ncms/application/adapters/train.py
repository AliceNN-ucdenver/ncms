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

    uv run python -m ncms.application.adapters.train \\
        --domain conversational \\
        --taxonomy experiments/intent_slot_distillation/taxonomies/conversational.yaml \\
        --adapter-dir experiments/intent_slot_distillation/adapters/conversational/v3 \\
        --target-size 500 \\
        --adversarial-size 200 \\
        --epochs 6 --lora-r 16

    # With baseline regression check:
    uv run python -m ncms.application.adapters.train \\
        --domain conversational \\
        --taxonomy ... \\
        --adapter-dir adapters/conversational/v4 \\
        --baseline-adapter-dir adapters/conversational/v3
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from ncms.application.adapters.corpus.loader import (
    dump_jsonl,
    load_all,
)
from ncms.application.adapters.methods.joint_bert_lora import (
    build_manifest,
    train,
)
from ncms.application.adapters.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
)
from ncms.application.adapters.sdg.expander import (
    _dedupe,
    expand_domain,
)


def _corpus_hash(examples: list[GoldExample]) -> str:
    """Deterministic hash of an ordered example list.

    Used for manifest provenance — re-running the same training on
    the same input produces the same hash, so downstream tooling
    can detect when two adapters were trained on different data.
    Previously lived in ``train_lora_adapter.py`` (research); moved
    inline here so the production path has no experiments/ imports.
    """
    h = hashlib.sha256()
    for ex in examples:
        row = json.dumps({
            "text": ex.text,
            "intent": ex.intent,
            "slots": ex.slots,
            "topic": ex.topic,
            "admission": ex.admission,
            "state_change": ex.state_change,
        }, sort_keys=True).encode("utf-8")
        h.update(row)
    return h.hexdigest()[:16]
from ncms.application.adapters.training.adversarial import (
    generate_adversarial,
)
from ncms.application.adapters.training.gate import (
    GateThresholds,
    _dump_outcome_json,
    run_gate,
    write_eval_report,
)

logger = logging.getLogger(__name__)


def _evaluate_heldout(
    adapter_dir: Path,
    heldout: list[GoldExample],
    *,
    domain: Domain,
    device: str | None,
) -> None:
    """Run the freshly-trained adapter over the held-out split.

    Computes per-head micro-accuracy + macro F1 on the held-out
    gold for the four classification heads (intent / topic /
    admission / state_change).  Role F1 is covered by the main
    gate's :func:`_slot_f1` over reconstructed slots.  Writes
    results to ``adapter_dir/heldout_eval.json`` and appends
    ``heldout_*`` entries to ``manifest.gate_metrics``.

    Separate from the promotion gate (``run_gate``) which evaluates
    on the full gold + adversarial corpora.  The held-out eval
    catches train/eval leakage — a head that scores well on the
    training gold but drops on held-out has overfit.
    """
    from collections import Counter, defaultdict
    from ncms.application.adapters.methods.joint_bert_lora import (
        AdapterManifest,
        LoraJointBert,
    )

    extractor = LoraJointBert(adapter_dir, device=device)

    n = len(heldout)
    # Per-head hit counts (numerator) + labeled-row counts (denominator).
    hits: Counter[str] = Counter()
    labeled: Counter[str] = Counter()
    # Per-head per-class confusion accounting (for macro F1).  Catches
    # class-collapse that accuracy hides when the held-out split is
    # skewed toward one dominant class.
    class_tpfpfn: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter),
    )

    def _record_head(head: str, gold: str | None, pred: str | None) -> None:
        if gold is None:
            return
        labeled[head] += 1
        if pred == gold:
            hits[head] += 1
            class_tpfpfn[head][gold]["tp"] += 1
        else:
            class_tpfpfn[head][gold]["fn"] += 1
            if pred is not None:
                class_tpfpfn[head][pred]["fp"] += 1

    for ex in heldout:
        pred = extractor.extract(ex.text, domain=domain)
        _record_head("intent", ex.intent, pred.intent)
        _record_head("topic", ex.topic, pred.topic)
        _record_head("admission", ex.admission, pred.admission)
        _record_head("state_change", ex.state_change, pred.state_change)

    metrics: dict[str, float] = {
        f"heldout_{head}_acc": hits[head] / labeled[head]
        for head in labeled
        if labeled[head] > 0
    }
    for head, per_class in class_tpfpfn.items():
        f1s: list[float] = []
        for _cls, counts in per_class.items():
            t = counts["tp"]
            fp_ = counts["fp"]
            fn_ = counts["fn"]
            if t + fp_ + fn_ == 0:
                continue
            p = t / (t + fp_) if t + fp_ else 0.0
            r = t / (t + fn_) if t + fn_ else 0.0
            f1 = 2 * p * r / (p + r) if (p + r) else 0.0
            f1s.append(f1)
        if f1s:
            metrics[f"heldout_{head}_f1_macro"] = round(
                sum(f1s) / len(f1s), 4,
            )
    metrics["heldout_n"] = float(n)

    # Persist: JSON next to the adapter + update manifest.gate_metrics.
    (adapter_dir / "heldout_eval.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True),
    )
    manifest = AdapterManifest.load(adapter_dir / "manifest.json")
    manifest.gate_metrics = {**manifest.gate_metrics, **{
        k: round(v, 4) for k, v in metrics.items()
    }}
    manifest.save(adapter_dir / "manifest.json")

    logger.info(
        "[phase4] held-out eval (n=%d): "
        "intent acc=%.3f f1_macro=%.3f | "
        "topic acc=%.3f f1_macro=%.3f | "
        "admission acc=%.3f f1_macro=%.3f | "
        "state_change acc=%.3f f1_macro=%.3f",
        n,
        metrics.get("heldout_intent_acc", float("nan")),
        metrics.get("heldout_intent_f1_macro", float("nan")),
        metrics.get("heldout_topic_acc", float("nan")),
        metrics.get("heldout_topic_f1_macro", float("nan")),
        metrics.get("heldout_admission_acc", float("nan")),
        metrics.get("heldout_admission_f1_macro", float("nan")),
        metrics.get("heldout_state_change_acc", float("nan")),
        metrics.get("heldout_state_change_f1_macro", float("nan")),
    )


# ---------------------------------------------------------------------------
# Phase 1 — Bootstrap
# ---------------------------------------------------------------------------


def phase1_bootstrap(
    domain: Domain, corpus_dir: Path,
) -> list[GoldExample]:
    """Load all pre-existing gold rows for ``domain``.

    Returns both tagged (multi-head labels present) and untagged
    rows — the training loop's per-head masking handles mixed
    labelling gracefully.  The loader silently drops the v6/v7.x
    ``shape_intent`` and v8 ``cue_tags`` fields if present on
    legacy rows.
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
    taxonomy_path: Path | None = None,  # retained for CLI compat; unused in v7
    output_path: Path,
) -> list[GoldExample]:
    """Run template-SDG expansion and write the result to disk.

    Post-v7 rewrite: topic labels come directly from each
    :class:`SlotPool`.``topic`` in the template registry, so no
    external ``object_to_topic`` map is required.  The
    ``taxonomy_path`` parameter is kept for CLI backwards-compat
    but is ignored by this call.

    ``target_size`` is the pre-dedup target; deduped output may be
    ~20% smaller depending on vocabulary diversity.
    """
    del taxonomy_path  # unused post-v7
    raw = expand_domain(domain, target=target_size, seed=seed)
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
    from ncms.application.adapters.training.adversarial import (
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
    heldout_fraction: float = 0.2,
    holdout_seed: int = 42,
) -> bool:
    """Train the LoRA adapter, run the gate, persist report.

    * ``heldout_fraction`` > 0 splits gold 80/20 (seeded, stable);
      training sees only the 80%, the 20% gets written to
      ``adapter_dir/heldout_gold.jsonl`` and evaluated after training
      for honest per-head metrics.  Set 0.0 to train on all gold.
    """
    import random as _random
    import yaml

    taxonomy: dict[str, list[str]] = {}
    if taxonomy_path is not None:
        data = yaml.safe_load(taxonomy_path.read_text()) or {}
        taxonomy = {
            "topic_labels": list(data.get("topic_labels") or []),
            "admission_labels": list(data.get("admission_labels") or []),
            "state_change_labels": list(data.get("state_change_labels") or []),
        }

    # v8.1: the v6/v7.x shape_intent classifier head was removed.
    # Query-shape classification is now produced compositionally at
    # inference by the CTLG synthesizer over the cue head's output —
    # see ncms.domain.tlg.semantic_parser.synthesize.

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
    # ── v8.1 held-out split: honest per-head evaluation ───────────
    # Splits gold into train_gold + heldout_gold via a seeded
    # shuffle so reruns are stable.  Training sees only train_gold
    # (plus SDG + adversarial); heldout_gold is saved to disk and
    # evaluated after training.  SDG + adversarial are NOT held out
    # — they're always in the train mix because they're synthetic /
    # target the role head's corner cases.
    heldout_gold: list[GoldExample] = []
    train_gold = gold
    if heldout_fraction > 0.0 and gold:
        rng = _random.Random(holdout_seed)
        shuffled = list(gold)
        rng.shuffle(shuffled)
        cut = max(1, int(len(shuffled) * heldout_fraction))
        heldout_gold = shuffled[:cut]
        train_gold = shuffled[cut:]
        logger.info(
            "[phase4] held-out split: %d gold rows (%.0f%%) held out "
            "for post-train evaluation (seed=%d); %d rows used for training",
            len(heldout_gold), heldout_fraction * 100,
            holdout_seed, len(train_gold),
        )

    # Upsample — repeat gold / adversarial rows in the training mix
    # to counter SDG-dilution on the slot head.  Corpus hash uses the
    # un-upsampled TRAIN rows so hash stability doesn't depend on mix
    # ratios; held-out rows are excluded from the hash so re-training
    # with a different seed produces the same hash.
    hash_input = train_gold + sdg + adversarial_train
    combined = (
        train_gold * gold_upsample
        + sdg
        + adversarial_train * adversarial_upsample
    )
    manifest.corpus_hash = _corpus_hash(hash_input)
    manifest.trained_at = _dt.datetime.now(_dt.UTC).isoformat()

    logger.info(
        "[phase4] training — domain=%s n_raw=%d n_training=%d "
        "(train_gold×%d + sdg + adversarial×%d) n_heldout=%d "
        "topic_labels=%d role_labels=%d epochs=%d lora_r=%d "
        "lora_alpha=%d",
        domain, len(hash_input), len(combined),
        gold_upsample, adversarial_upsample, len(heldout_gold),
        len(manifest.topic_labels), len(manifest.role_labels),
        epochs, lora_r, lora_alpha,
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

    # Dump held-out rows for audit + future re-evaluation without
    # re-training.  JSONL path next to the adapter; the train corpus
    # hash in manifest.corpus_hash pins the split contract.
    if heldout_gold:
        from ncms.application.adapters.corpus.loader import dump_jsonl
        dump_jsonl(heldout_gold, adapter_dir / "heldout_gold.jsonl")
        logger.info(
            "[phase4] wrote held-out corpus to %s",
            adapter_dir / "heldout_gold.jsonl",
        )
        _evaluate_heldout(
            adapter_dir, heldout_gold, domain=domain, device=device,
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
    parser.add_argument("--lora-r", type=int, default=16,
                        help="LoRA rank (v9 default: 16).")
    parser.add_argument("--lora-alpha", type=int, default=32,
                        help="LoRA alpha (v9 default: 32 = 2×rank).")
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--lora-targets", default="query,value")
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--heldout-fraction", type=float, default=0.2,
        help=(
            "Fraction of gold rows to hold out of training for honest "
            "per-head evaluation.  Default 0.2 (80/20 split).  Set to "
            "0.0 to train on all gold (disables held-out eval)."
        ),
    )
    parser.add_argument(
        "--holdout-seed", type=int, default=42,
        help="RNG seed for the held-out shuffle (stable splits).",
    )

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
        "--skip-sdg", action="store_true",
        help=(
            "Skip phase 2 (template-SDG expansion).  Appropriate when "
            "the gold corpus is already large enough to train without "
            "synthetic augmentation."
        ),
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

    # Log the resolved domain ↔ adapter ↔ corpus mapping upfront so
    # the run-log is self-describing (no silent mismatches).
    try:
        from ncms.application.adapters.schemas import (
            get_domain_manifest,
        )
        m = get_domain_manifest(args.domain)  # type: ignore[arg-type]
        logger.info("=" * 72)
        logger.info("train_adapter: DOMAIN MANIFEST")
        logger.info("  domain              = %s", m.name)
        logger.info("  description         = %s", m.description)
        logger.info("  gold corpus         = %s", m.gold_jsonl)
        logger.info("  sdg corpus          = %s", m.sdg_jsonl)
        logger.info("  adversarial corpus  = %s", m.adversarial_train_jsonl)
        logger.info("  taxonomy            = %s", m.taxonomy_yaml)
        logger.info("  adapter output root = %s", m.adapter_output_root)
        logger.info("  deployed path       = %s", m.deployed_path(args.version))
        logger.info("  CLI --adapter-dir   = %s", args.adapter_dir)
        logger.info("  CLI --taxonomy      = %s", args.taxonomy)
        logger.info("=" * 72)
    except (KeyError, ImportError):
        # New domain without a manifest — warn but proceed.
        logger.warning(
            "no DomainManifest for domain=%s; proceeding with explicit CLI paths",
            args.domain,
        )

    t0 = time.perf_counter()

    # Phase 1
    gold = phase1_bootstrap(args.domain, args.corpus_dir)
    if not gold:
        parser.error(f"no gold rows for domain={args.domain!r}")

    # Phase 2
    if args.skip_sdg:
        logger.info(
            "[phase2] skipping SDG expansion (--skip-sdg); training on gold alone",
        )
        sdg = []
    else:
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
        heldout_fraction=args.heldout_fraction,
        holdout_seed=args.holdout_seed,
    )

    elapsed = time.perf_counter() - t0
    print(
        f"[train-adapter] total elapsed {elapsed:.1f}s  "
        f"verdict={'PASS' if passed else 'FAIL'}",
    )
    sys.exit(0 if passed else 1)


def run_training(
    *,
    domain: str,
    version: str,
    target_size: int = 3000,
    adversarial_size: int = 300,
    epochs: int | None = None,
    batch_size: int | None = None,
    learning_rate: float | None = None,
    device: str | None = None,
    skip_sdg: bool = False,
    skip_adversarial: bool = False,
) -> None:
    """Programmatic training entry point used by ``ncms adapters train``.

    Resolves paths via ``DomainManifest`` so callers only pass
    domain + version.  Builds an argv list for the existing argparse
    ``main()`` so CLI + programmatic paths share one code path.
    """
    from ncms.application.adapters.schemas import get_domain_manifest

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    adapter_dir = manifest.adapter_output_root / version

    argv = [
        "ncms-adapters-train",
        "--domain", domain,
        "--adapter-dir", str(adapter_dir),
        "--corpus-dir", str(manifest.gold_jsonl.parent),
        "--taxonomy", str(manifest.taxonomy_yaml),
        "--version", version,
        "--target-size", str(target_size),
        "--adversarial-size", str(adversarial_size),
    ]
    if epochs is not None:
        argv += ["--epochs", str(epochs)]
    if batch_size is not None:
        argv += ["--batch-size", str(batch_size)]
    if learning_rate is not None:
        argv += ["--lr", str(learning_rate)]
    if device is not None:
        argv += ["--device", device]
    if skip_sdg:
        argv.append("--skip-sdg")
    if skip_adversarial:
        argv.append("--skip-adversarial")

    prev_argv = sys.argv
    try:
        sys.argv = argv
        main()
    except SystemExit as exit_error:
        if exit_error.code not in (0, None):
            raise
    finally:
        sys.argv = prev_argv


if __name__ == "__main__":
    main()
