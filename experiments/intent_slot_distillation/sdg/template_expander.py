"""Template-based SDG expander.

Walks the template × vocabulary cross product and emits
:class:`GoldExample` rows.  Deterministic for a given
``--seed``.  Primary slot value is always mapped to the
appropriate per-domain slot name (``object`` for conversational,
the specific slot name otherwise) so downstream metrics score
slot F1 correctly.

Usage::

    uv run python -m experiments.intent_slot_distillation.sdg.template_expander \\
        --domain conversational --target 10000 \\
        --output experiments/intent_slot_distillation/corpus/sdg_conversational.jsonl
"""

from __future__ import annotations

import argparse
import itertools
import random
from pathlib import Path

from experiments.intent_slot_distillation.corpus.loader import dump_jsonl
from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
    Intent,
)
from experiments.intent_slot_distillation.sdg.templates import (
    TEMPLATE_REGISTRY,
    DomainTemplates,
    IntentTemplate,
)


def _primary_slot_name(domain: Domain, template: IntentTemplate) -> str:
    """Map the template's ``object`` placeholder to the domain's
    principal slot name.

    Conversational uses a generic ``object`` slot.  Software-dev
    maps to ``library``.  Clinical maps to ``medication``.  These
    are the most common primary slots in the hand-labelled gold and
    let the expander emit realistic slot distributions.
    """
    if "object" in template.required_slots:
        if domain == "conversational":
            return "object"
        if domain == "software_dev":
            return "library"
        if domain == "clinical":
            return "medication"
    return template.required_slots[0]


def _render_example(
    rng: random.Random,
    domain: Domain,
    intent: Intent,
    template: IntentTemplate,
    vocab: DomainTemplates,
) -> GoldExample | None:
    """Realise one template using vocabulary drawn with ``rng``."""
    obj = rng.choice(vocab.objects)
    subs: dict[str, str] = {"object": obj}
    slots: dict[str, str] = {_primary_slot_name(domain, template): obj}

    if intent == "positive":
        subs["verb"] = rng.choice(vocab.positive_verbs)
    elif intent == "negative":
        subs["verb"] = rng.choice(vocab.negative_verbs)
    elif intent == "habitual":
        freq = rng.choice(vocab.habitual_freqs)
        subs["freq"] = freq
        slots["frequency"] = freq
    elif intent == "difficulty":
        subs["phrase"] = rng.choice(vocab.difficulty_phrasings)
    elif intent == "choice":
        alt = rng.choice(vocab.alternatives)
        subs["alt"] = alt
        slots["alternative"] = alt

    # Render — tolerate templates that don't use every key.
    try:
        text = template.pattern.format(**subs)
    except KeyError:
        return None
    return GoldExample(
        text=text,
        domain=domain,
        intent=intent,
        slots=slots,
        split="sdg",
        source=f"template-v1 seed={rng.getstate()[1][0]}",
    )


def expand_domain(
    domain: Domain,
    *,
    target: int = 2000,
    seed: int = 17,
) -> list[GoldExample]:
    """Produce ``target`` synthetic examples for ``domain``.

    Distributes roughly evenly across the five intents.  Repeats
    the template × vocab cycle as needed; since the vocabulary is
    finite, large ``target`` values will produce duplicates — the
    caller dedupes (see ``main``) so the final count may be a bit
    smaller than ``target``.
    """
    rng = random.Random(seed)
    vocab = TEMPLATE_REGISTRY[domain]
    intents: list[Intent] = [
        "positive", "negative", "habitual", "difficulty", "choice",
    ]
    per_intent = target // len(intents)
    out: list[GoldExample] = []
    for intent in intents:
        templates = vocab.intent_templates.get(intent, ())
        if not templates:
            continue
        # Cycle through templates × objects × auxiliary vocab.
        for _ in range(per_intent):
            template = rng.choice(templates)
            example = _render_example(rng, domain, intent, template, vocab)
            if example is not None:
                out.append(example)
    return out


def _dedupe(examples: list[GoldExample]) -> list[GoldExample]:
    seen: set[str] = set()
    out: list[GoldExample] = []
    for ex in examples:
        key = ex.text
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expand intent+slot templates into SDG data",
    )
    parser.add_argument(
        "--domain", required=True, choices=DOMAINS,
    )
    parser.add_argument(
        "--target", type=int, default=2000,
        help="Target pre-dedup example count (default: 2000).",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--output", type=Path, required=True,
        help="JSONL output path.",
    )
    args = parser.parse_args()

    raw = expand_domain(args.domain, target=args.target, seed=args.seed)
    deduped = _dedupe(raw)
    dump_jsonl(deduped, args.output)
    print(
        f"[template-expander] domain={args.domain} "
        f"raw={len(raw)} deduped={len(deduped)} → {args.output}",
    )


if __name__ == "__main__":
    main()
