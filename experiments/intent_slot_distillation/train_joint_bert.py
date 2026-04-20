"""CLI driver for :func:`methods.joint_bert.train`.

Per-domain trainer — reads one or more gold JSONL splits, filters to
the requested domain, and writes a checkpoint directory that
:class:`methods.joint_bert.JointBert` can load.

Usage::

    # Train on gold only (smallest, noisiest signal — sanity check).
    uv run python -m experiments.intent_slot_distillation.train_joint_bert \\
        --domain conversational \\
        --splits gold \\
        --checkpoint-dir checkpoints/joint_bert/conversational \\
        --epochs 5

    # Train on gold + SDG-augmented data (recommended once SDG has run).
    uv run python -m experiments.intent_slot_distillation.train_joint_bert \\
        --domain conversational \\
        --splits gold,sdg \\
        --checkpoint-dir checkpoints/joint_bert/conversational_sdg \\
        --epochs 3
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

# Load .env (HF_TOKEN, optional device overrides) before any HF / torch
# code imports.  Noop when run outside a .env-bearing checkout.
try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover — experiment may run outside repo
    pass

from experiments.intent_slot_distillation.corpus.loader import (
    load_all,
    load_jsonl,
)
from experiments.intent_slot_distillation.methods.joint_bert import train
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Train Joint BERT per-domain",
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
        "--checkpoint-dir", type=Path, required=True,
    )
    parser.add_argument("--encoder", default="bert-base-uncased")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=3e-5)
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
    print(
        f"[train] domain={args.domain}  splits={splits}  "
        f"n_examples={len(examples)}  epochs={args.epochs}",
        flush=True,
    )

    cfg = train(
        examples,
        domain=args.domain,  # type: ignore[arg-type]
        encoder=args.encoder,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        checkpoint_dir=args.checkpoint_dir,
        device=args.device,
    )
    print(
        f"[train] done — checkpoint at {args.checkpoint_dir} "
        f"(encoder={cfg.encoder}, intents={len(cfg.intent_labels)}, "
        f"slots={len(cfg.slot_labels)})",
    )


if __name__ == "__main__":
    main()
