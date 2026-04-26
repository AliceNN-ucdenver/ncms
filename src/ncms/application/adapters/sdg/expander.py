"""Typed-slot SDG expander (v7).

Walks ``DomainTemplates.templates`` × matching :class:`SlotPool` and
emits :class:`GoldExample` rows that populate every slot declared in
the domain's ``SLOT_TAXONOMY``.  Deterministic for a given ``--seed``.

Rendering pipeline per template:

  1. Collect ``SlotPool`` instances whose ``slot_name`` matches the
     template's ``slot_name`` (a slot may have multiple pools that
     share the same slot with different topics — e.g. software_dev's
     ``tool`` slot has a tooling pool and an infra pool).
  2. Pick one pool with uniform weighting across pools (topic
     balancing), then draw one value from that pool.
  3. Fill template auxiliaries (``{verb}``, ``{alt}``, ``{freq}``,
     ``{phrase}``, ``{area}``, ``{aside}``, ``{role}``) from the
     domain's shared vocab.  Auxiliary slot values (alt / freq) ALSO
     populate the emitted ``GoldExample.slots`` dict — a "choice"
     template's ``{primary}`` fills ``library`` and its ``{alt}``
     fills ``alternative``.
  4. Emit ``GoldExample(slots={...}, topic=pool.topic, intent=...,
     state_change=..., admission="persist")``.

Output composition (target-dependent):

  - Preference intents (positive / negative / habitual / difficulty
    / choice)        — ~50% of rows
  - Neutral / none templates                                    — ~35%
  - state_change=declaration                                    — ~7.5%
  - state_change=retirement                                     — ~7.5%

The expander uses the per-template ``state_change`` + ``intent``
fields so we don't need separate "preference vs state-change" code
paths — a template IS its own configuration.

Usage::

    uv run python -m ncms.application.adapters.sdg.expander \
        --domain software_dev --target 10000 \
        --output experiments/intent_slot_distillation/corpus/sdg_software_dev.jsonl
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from ncms.application.adapters.corpus.loader import dump_jsonl
from ncms.application.adapters.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
    RoleSpan,
)
from ncms.application.adapters.sdg.catalog import detect_spans
from ncms.application.adapters.sdg.templates import (
    TEMPLATE_REGISTRY,
    DomainTemplates,
    SlotPool,
    SlotTemplate,
)


def _pools_for_slot(
    vocab: DomainTemplates,
    slot_name: str,
) -> tuple[SlotPool, ...]:
    """Pools whose ``slot_name`` matches; empty tuple when none."""
    return tuple(p for p in vocab.slot_pools if p.slot_name == slot_name)


def _pool(vocab: DomainTemplates, slot_name: str) -> SlotPool | None:
    """First pool for ``slot_name``, or ``None``.  Used when we just
    need a single pool for an auxiliary slot (frequency / alternative).
    """
    pools = _pools_for_slot(vocab, slot_name)
    return pools[0] if pools else None


def _draw_auxiliary(
    rng: random.Random,
    template: SlotTemplate,
    vocab: DomainTemplates,
    subs: dict[str, str],
    slots: dict[str, str],
) -> None:
    """Fill auxiliary placeholders the pattern requires.

    Mutates ``subs`` (for ``str.format``) and ``slots`` (for the
    emitted GoldExample) in place.  Supports:

      - ``{verb}``  ← positive_verbs / negative_verbs
      - ``{alt}``   ← alternative pool  (also populates slots[alternative])
      - ``{freq}``  ← frequency pool    (also populates slots[frequency])
      - ``{phrase}``← difficulty_phrasings
      - ``{area}``  ← areas
      - ``{aside}`` ← asides
      - ``{role}``  ← roles
    """
    pattern = template.pattern
    if "{verb}" in pattern:
        pool = (
            vocab.positive_verbs
            if template.intent == "positive"
            else vocab.negative_verbs
            if template.intent == "negative"
            else vocab.positive_verbs  # fallback
        )
        if pool:
            subs["verb"] = rng.choice(pool)
    if "{alt}" in pattern:
        alt_pool = _pool(vocab, "alternative")
        if alt_pool:
            alt = rng.choice(alt_pool.values)
            subs["alt"] = alt
            # Only add to slots if template's primary slot isn't alternative
            # (avoids duplicate-key overwrite).
            if template.slot_name != "alternative":
                slots["alternative"] = alt
    if "{freq}" in pattern:
        freq_pool = _pool(vocab, "frequency")
        if freq_pool:
            freq = rng.choice(freq_pool.values)
            subs["freq"] = freq
            if template.slot_name != "frequency":
                slots["frequency"] = freq
    if "{phrase}" in pattern and vocab.difficulty_phrasings:
        subs["phrase"] = rng.choice(vocab.difficulty_phrasings)
    if "{area}" in pattern and vocab.areas:
        subs["area"] = rng.choice(vocab.areas)
    if "{aside}" in pattern and vocab.asides:
        subs["aside"] = rng.choice(vocab.asides)
    if "{role}" in pattern and vocab.roles:
        subs["role"] = rng.choice(vocab.roles)


def _label_role_spans(
    text: str,
    slots: dict[str, str],
    domain: Domain,
) -> list[RoleSpan]:
    """Post-render gazetteer pass → role-labeled spans.

    Every surface the gazetteer detects in the rendered SDG text gets
    a role assignment:

      - ``primary`` when the span's canonical form matches the
        value of its own catalog slot in ``slots``.  Templates fill
        one slot value as ``{primary}`` so the emitted surface and
        ``slots[template.slot_name]`` line up exactly.
      - ``alternative`` when the canonical matches ``slots["alternative"]``
        (``{alt}`` template placeholders).
      - ``not_relevant`` when the span was detected but does not
        match any labelled slot value.  Catalog-heavy templates like
        software_dev "We have standardised on Postgres for the main
        API, and we have adopted Grafana for dashboards." can
        generate spurious hits; labelling them as ``not_relevant``
        explicitly teaches the role head that raw detection is not
        enough for a positive assignment.
    """
    gaz = detect_spans(text, domain=domain)
    # Normalise slot values to lowercase canonicals for comparison.
    slot_values_lower = {k: v.lower().strip() for k, v in slots.items() if v}
    alt_value = slot_values_lower.get("alternative")
    out: list[RoleSpan] = []
    for span in gaz:
        canon = span.canonical.lower()
        if alt_value is not None and canon == alt_value:
            role = "alternative"
        elif slot_values_lower.get(span.slot) == canon:
            role = "primary"
        else:
            role = "not_relevant"
        out.append(
            RoleSpan(
                char_start=span.char_start,
                char_end=span.char_end,
                surface=span.surface,
                canonical=span.canonical,
                slot=span.slot,
                role=role,  # type: ignore[arg-type]
                source="sdg-template",
            )
        )
    return out


def _render(
    rng: random.Random,
    domain: Domain,
    template: SlotTemplate,
    vocab: DomainTemplates,
) -> GoldExample | None:
    """Render one template into a ``GoldExample``.  Returns ``None``
    when the pattern requires an auxiliary that the domain's vocab
    doesn't provide (keeps the caller code simple).
    """
    pools = _pools_for_slot(vocab, template.slot_name)
    if not pools:
        return None
    # Topic balancing: uniform across pools for the same slot.
    pool = rng.choice(pools)
    if not pool.values:
        return None
    primary = rng.choice(pool.values)

    subs: dict[str, str] = {"primary": primary}
    slots: dict[str, str] = {template.slot_name: primary}
    _draw_auxiliary(rng, template, vocab, subs, slots)

    try:
        text = template.pattern.format(**subs)
    except KeyError:
        return None

    role_spans = _label_role_spans(text, slots, domain)

    return GoldExample(
        text=text,
        domain=domain,
        intent=template.intent,
        slots=slots,
        topic=pool.topic,
        admission="persist",  # type: ignore[arg-type]
        state_change=template.state_change,  # type: ignore[arg-type]
        role_spans=role_spans,
        split="sdg",
        source=f"template-v7 seed={rng.getstate()[1][0]}",
    )


def _bucketize(
    templates: tuple[SlotTemplate, ...],
) -> dict[str, list[SlotTemplate]]:
    """Group templates by (intent, state_change) so the expander can
    hit target per-bucket shares without biasing toward any single
    phrasing."""
    buckets: dict[str, list[SlotTemplate]] = {}
    for t in templates:
        if t.state_change == "declaration":
            key = "declaration"
        elif t.state_change == "retirement":
            key = "retirement"
        elif t.intent == "none":
            key = "none"
        else:
            key = f"pref_{t.intent}"
        buckets.setdefault(key, []).append(t)
    return buckets


def _resolve_share_targets(
    *,
    target: int,
    has_preferences: bool,
    has_declaration: bool,
    has_retirement: bool,
) -> tuple[int, int, int, int]:
    """Return (pref, none, declaration, retirement) absolute counts."""
    pref_share = 0.50
    none_share = 0.35
    decl_share = 0.075
    ret_share = 0.075
    if not has_preferences:
        none_share += pref_share
        pref_share = 0.0
    if not has_declaration:
        none_share += decl_share
        decl_share = 0.0
    if not has_retirement:
        none_share += ret_share
        ret_share = 0.0
    return (
        int(target * pref_share),
        int(target * none_share),
        int(target * decl_share),
        int(target * ret_share),
    )


def _draw_from_bucket(
    *,
    rng: random.Random,
    domain: Domain,
    templates: list[SlotTemplate],
    vocab: DomainTemplates,
    n: int,
    out: list[GoldExample],
) -> None:
    if n <= 0 or not templates:
        return
    for _ in range(n):
        ex = _render(rng, domain, rng.choice(templates), vocab)
        if ex is not None:
            out.append(ex)


def expand_domain(
    domain: Domain,
    *,
    target: int = 2000,
    seed: int = 17,
) -> list[GoldExample]:
    """Produce ``target`` synthetic examples for ``domain``.

    Target composition (rounded):
      - 50% preference intents (positive + negative + habitual +
        difficulty + choice).
      - 35% neutral / none.
      - 7.5% state_change=declaration.
      - 7.5% state_change=retirement.

    Conversational has no state_change templates; its share collapses
    into the neutral / none bucket.
    """
    rng = random.Random(seed)
    vocab = TEMPLATE_REGISTRY[domain]
    buckets = _bucketize(vocab.templates)
    preference_keys = [k for k in buckets if k.startswith("pref_")]

    pref_target, none_target, decl_target, ret_target = _resolve_share_targets(
        target=target,
        has_preferences=bool(preference_keys),
        has_declaration="declaration" in buckets,
        has_retirement="retirement" in buckets,
    )

    out: list[GoldExample] = []

    if preference_keys:
        per_bucket = max(1, pref_target // len(preference_keys))
        for key in preference_keys:
            _draw_from_bucket(
                rng=rng,
                domain=domain,
                templates=buckets[key],
                vocab=vocab,
                n=per_bucket,
                out=out,
            )

    for bucket_name, n in (
        ("none", none_target),
        ("declaration", decl_target),
        ("retirement", ret_target),
    ):
        _draw_from_bucket(
            rng=rng,
            domain=domain,
            templates=buckets.get(bucket_name, []),
            vocab=vocab,
            n=n,
            out=out,
        )

    return out


def _dedupe(examples: list[GoldExample]) -> list[GoldExample]:
    seen: set[str] = set()
    out: list[GoldExample] = []
    for ex in examples:
        if ex.text in seen:
            continue
        seen.add(ex.text)
        out.append(ex)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expand typed-slot templates into SDG data",
    )
    parser.add_argument("--domain", required=True, choices=DOMAINS)
    parser.add_argument(
        "--target",
        type=int,
        default=2000,
        help="Target pre-dedup example count (default: 2000).",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="JSONL output path.",
    )
    args = parser.parse_args()

    raw = expand_domain(args.domain, target=args.target, seed=args.seed)
    deduped = _dedupe(raw)
    dump_jsonl(deduped, args.output)
    print(
        f"[template-expander-v7] domain={args.domain} "
        f"raw={len(raw)} deduped={len(deduped)} → {args.output}",
    )


if __name__ == "__main__":
    main()
