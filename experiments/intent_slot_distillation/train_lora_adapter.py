"""Sprint 1 + 2 — train a LoRA adapter artifact.

Produces ``adapters/<domain>/<version>/`` with::

    lora_adapter/        — peft save_pretrained directory
    heads.safetensors    — intent/topic/admission/state/slot heads
    manifest.json        — full head + lora config + gate metrics
    taxonomy.yaml        — human-readable label vocab
    eval_report.md       — optional, written by the gate step

Usage::

    # Sprint 1 — parity check against full-FT baseline.  Same gold
    # corpus, same schemas, no taxonomy file needed.
    uv run python -m experiments.intent_slot_distillation.train_lora_adapter \\
        --domain conversational \\
        --splits gold \\
        --adapter-dir adapters/conversational/v1 \\
        --epochs 10

    # Sprint 2 — supply a taxonomy with topic / admission / state
    # labels; multi-head training kicks in per-example where labels
    # are present.
    uv run python -m experiments.intent_slot_distillation.train_lora_adapter \\
        --domain conversational \\
        --splits gold,sdg \\
        --taxonomy experiments/intent_slot_distillation/taxonomies/conversational.yaml \\
        --adapter-dir adapters/conversational/v2 \\
        --epochs 5
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover — experiment may run outside repo
    pass

from experiments.intent_slot_distillation.corpus.loader import (
    load_all,
    load_jsonl,
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

logger = logging.getLogger(__name__)

_DEFAULT_CORPUS_DIR = Path(__file__).parent / "corpus"


def _collect_examples(
    domain: Domain, splits: list[str], corpus_dir: Path,
) -> list[GoldExample]:
    """Load all requested splits for ``domain`` and concatenate."""
    out: list[GoldExample] = []
    for split in splits:
        if split == "adversarial":
            path = corpus_dir.parent / "adversarial.jsonl"
            if not path.exists():
                continue
            out.extend(ex for ex in load_jsonl(path) if ex.domain == domain)
        else:
            out.extend(
                ex for ex in load_all(corpus_dir, split=split)
                if ex.domain == domain
            )
    return out


def _corpus_hash(examples: list[GoldExample]) -> str:
    """Deterministic hash of an ordered example list.

    Used for manifest provenance — re-running the same training on
    the same input produces the same ``corpus_hash``, so downstream
    tooling can detect when two adapters were trained on different
    data.
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


def _load_taxonomy(path: Path | None) -> dict[str, list[str]]:
    """Load YAML taxonomy or return empty defaults.

    Shape::

        topic_labels: [food_pref, hobby, transit, ...]
        admission_labels: [persist, ephemeral, discard]
        state_change_labels: [declaration, retirement, none]

    All three keys are optional; omission uses the schema defaults
    (empty topic vocab + full admission/state_change enums).
    """
    if path is None:
        return {}
    import yaml
    data = yaml.safe_load(path.read_text()) or {}
    return {
        "topic_labels": list(data.get("topic_labels") or []),
        "admission_labels": list(data.get("admission_labels") or []),
        "state_change_labels": list(data.get("state_change_labels") or []),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Train a LoRA adapter + multi-head classifier",
    )
    parser.add_argument("--domain", required=True, choices=list(DOMAINS))
    parser.add_argument(
        "--splits", default="gold",
        help="Comma-separated: gold,llm,sdg,adversarial",
    )
    parser.add_argument(
        "--corpus-dir", type=Path, default=_DEFAULT_CORPUS_DIR,
    )
    parser.add_argument(
        "--taxonomy", type=Path, default=None,
        help="Optional taxonomy YAML (topic/admission/state_change vocabs).",
    )
    parser.add_argument(
        "--adapter-dir", type=Path, required=True,
        help="Output directory for the adapter artifact.",
    )
    parser.add_argument("--encoder", default="bert-base-uncased")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--lora-r", type=int, default=8)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument(
        "--lora-targets", default="query,value",
        help="Comma-separated BERT attention modules to adapt.",
    )
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument(
        "--device", default=None,
        help="Override device (cuda/mps/cpu).  Default: auto-resolve.",
    )
    args = parser.parse_args()

    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    examples = _collect_examples(args.domain, splits, args.corpus_dir)
    if not examples:
        parser.error(
            f"no examples for domain={args.domain!r} splits={splits!r}",
        )

    taxonomy = _load_taxonomy(args.taxonomy)
    manifest = build_manifest(
        domain=args.domain,  # type: ignore[arg-type]
        encoder=args.encoder,
        topic_labels=taxonomy.get("topic_labels"),
        admission_labels=(
            taxonomy["admission_labels"]
            if taxonomy.get("admission_labels") else None
        ),
        state_change_labels=(
            taxonomy["state_change_labels"]
            if taxonomy.get("state_change_labels") else None
        ),
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        lora_target_modules=[
            t.strip() for t in args.lora_targets.split(",") if t.strip()
        ],
        max_length=args.max_length,
        version=args.version,
    )
    manifest.corpus_hash = _corpus_hash(examples)
    manifest.trained_at = _dt.datetime.now(_dt.UTC).isoformat()

    print(
        f"[train-lora] domain={args.domain}  splits={splits}  "
        f"n_examples={len(examples)}  topic_labels={len(manifest.topic_labels)}  "
        f"slot_labels={len(manifest.slot_labels)}  epochs={args.epochs}  "
        f"lora_r={args.lora_r}",
        flush=True,
    )

    train(
        examples,
        domain=args.domain,  # type: ignore[arg-type]
        adapter_dir=args.adapter_dir,
        manifest=manifest,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        device=args.device,
    )
    print(f"[train-lora] done — adapter at {args.adapter_dir}")


if __name__ == "__main__":
    main()
