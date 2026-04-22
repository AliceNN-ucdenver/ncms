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
    object_to_topic: dict[str, str] | None = None,
    *,
    state_change: str = "none",
) -> GoldExample | None:
    """Realise one template using vocabulary drawn with ``rng``.

    If ``object_to_topic`` is provided, the chosen ``object`` value
    is mapped to a topic label (or ``"other"`` when unmapped).  A
    heuristic admission label is always ``persist`` — every
    template in the registry produces a structured, persist-worthy
    utterance.

    The caller passes ``state_change`` explicitly so the same render
    function can emit preference rows (``state_change="none"``),
    pure-none rows (also ``"none"``), and state-change rows
    (``"declaration"`` or ``"retirement"``).
    """
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
    # ``intent == "none"`` templates only use {object}; nothing extra.

    # State-change retirement templates that use {alt} ("deprecated
    # in favour of {alt}") need an alternative filled.
    if "{alt}" in template.pattern and "alt" not in subs:
        alt = rng.choice(vocab.alternatives)
        subs["alt"] = alt
        slots["alternative"] = alt

    # Render — tolerate templates that don't use every key.
    try:
        text = template.pattern.format(**subs)
    except KeyError:
        return None

    topic: str | None = None
    if object_to_topic is not None:
        topic = object_to_topic.get(obj) or object_to_topic.get(
            obj.lower(),
        ) or "other"

    # Every template in the registry emits structured, persist-worthy
    # content — admission heuristic is uniform.
    admission = "persist"

    return GoldExample(
        text=text,
        domain=domain,
        intent=intent,
        slots=slots,
        topic=topic,
        admission=admission,  # type: ignore[arg-type]
        state_change=state_change,  # type: ignore[arg-type]
        split="sdg",
        source=f"template-v2 seed={rng.getstate()[1][0]}",
    )


def expand_domain(
    domain: Domain,
    *,
    target: int = 2000,
    seed: int = 17,
    object_to_topic: dict[str, str] | None = None,
) -> list[GoldExample]:
    """Produce ``target`` synthetic examples for ``domain``.

    Output composition (approximate, target-dependent):

    * 50% preference (positive / negative / habitual / difficulty /
      choice) — ``intent=<label>``, ``state_change=none``
    * 35% pure-none distractors — ``intent=none``,
      ``state_change=none``
    * 15% state-change (declaration + retirement) — ``intent=none``,
      ``state_change=declaration|retirement``

    Conversational has no state-change templates by design, so for
    convo the 15% state-change share collapses into additional
    pure-none rows (total ~50% none).

    When ``object_to_topic`` is provided (loaded from the per-domain
    taxonomy YAML), emitted examples carry ``topic`` labels derived
    from the object surface-form they were built around.  Without a
    map, the topic field stays ``None``.
    """
    rng = random.Random(seed)
    vocab = TEMPLATE_REGISTRY[domain]

    # Target share per category.
    preference_target = int(target * 0.50)
    none_target = int(target * 0.35)
    decl_target = int(target * 0.075)
    ret_target = int(target * 0.075)

    out: list[GoldExample] = []

    # ── Preference intents ────────────────────────────────────────
    preference_intents: list[Intent] = [
        "positive", "negative", "habitual", "difficulty", "choice",
    ]
    per_preference = preference_target // len(preference_intents)
    for intent in preference_intents:
        templates = vocab.intent_templates.get(intent, ())
        if not templates:
            continue
        for _ in range(per_preference):
            template = rng.choice(templates)
            example = _render_example(
                rng, domain, intent, template, vocab, object_to_topic,
                state_change="none",
            )
            if example is not None:
                out.append(example)

    # ── Pure-none distractors ─────────────────────────────────────
    # Teaches the intent head that descriptive / question /
    # assistant prose is NOT a preference.  Without this the
    # adapter over-fires on corpus content at eval time (v4 bug:
    # 93%+ non-none predictions on prose corpora).
    none_templates = vocab.none_templates
    if none_templates:
        # If the domain has no state-change templates, fold the
        # state-change budget into pure-none so the total still hits
        # the target row count.
        effective_none_target = none_target
        if not vocab.state_change_decl_templates and not vocab.state_change_ret_templates:
            effective_none_target += decl_target + ret_target
        for _ in range(effective_none_target):
            template = rng.choice(none_templates)
            example = _render_example(
                rng, domain, "none", template, vocab, object_to_topic,
                state_change="none",
            )
            if example is not None:
                out.append(example)

    # ── State-change declaration ──────────────────────────────────
    decl_templates = vocab.state_change_decl_templates
    if decl_templates:
        for _ in range(decl_target):
            template = rng.choice(decl_templates)
            example = _render_example(
                rng, domain, "none", template, vocab, object_to_topic,
                state_change="declaration",
            )
            if example is not None:
                out.append(example)

    # ── State-change retirement ──────────────────────────────────
    ret_templates = vocab.state_change_ret_templates
    if ret_templates:
        for _ in range(ret_target):
            template = rng.choice(ret_templates)
            example = _render_example(
                rng, domain, "none", template, vocab, object_to_topic,
                state_change="retirement",
            )
            if example is not None:
                out.append(example)

    return out


def _load_object_to_topic(taxonomy_path: Path | None) -> dict[str, str] | None:
    """Load the ``object_to_topic`` map from a taxonomy YAML.

    Returns ``None`` when no path given or the file lacks the key,
    leaving the expander to emit topic-less rows.
    """
    if taxonomy_path is None:
        return None
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return None
    data = yaml.safe_load(taxonomy_path.read_text()) or {}
    mapping = data.get("object_to_topic")
    if not mapping:
        return None
    return {str(k): str(v) for k, v in mapping.items()}


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
    parser.add_argument(
        "--taxonomy", type=Path, default=None,
        help=(
            "Optional per-domain taxonomy YAML.  When supplied and the "
            "file has an object_to_topic map, emitted rows carry topic "
            "labels + admission=persist + state_change=none."
        ),
    )
    args = parser.parse_args()

    object_to_topic = _load_object_to_topic(args.taxonomy)
    raw = expand_domain(
        args.domain,
        target=args.target,
        seed=args.seed,
        object_to_topic=object_to_topic,
    )
    deduped = _dedupe(raw)
    dump_jsonl(deduped, args.output)
    topic_note = (
        f" (with topic labels, {len(object_to_topic)} object→topic entries)"
        if object_to_topic else ""
    )
    print(
        f"[template-expander] domain={args.domain} "
        f"raw={len(raw)} deduped={len(deduped)}{topic_note} → {args.output}",
    )


if __name__ == "__main__":
    main()
