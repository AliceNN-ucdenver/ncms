"""Prompt construction for v9 stratified archetype generation.

One function, :func:`build_archetype_prompt`, renders a full prompt
from an :class:`ArchetypeSpec` plus sampled entities.  Kept in its
own module so the prompt template is reviewable in one place and
can be unit-tested independently of the generator loop.

Prompt anatomy (top-to-bottom):

1. **Task** — one sentence naming the generation intent.
2. **Joint labels** — the fixed (intent, admission, state_change,
   topic) tuple every row must express.
3. **Entities to use** — the pre-sampled entity set, one per
   role_span, with the expected role annotation.
4. **Style envelope** — target length range, surface-diversity
   guidance, what to avoid.
5. **Few-shot examples** — the archetype's ``example_utterances``.
6. **Phrasing inspirations** — ``phrasings`` (as loose guidance,
   not strict templates — we want surface diversity).
7. **Output format** — JSON list of exactly ``n`` strings.

The prompt intentionally asks for natural prose, not
placeholder-filled templates: the LLM should *express* the entities
rather than *inject* them, so the emitted rows aren't detectable
as templated.
"""

from __future__ import annotations

from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec


# ---------------------------------------------------------------------------
# Entity bundle — produced by the generator, consumed here.
# ---------------------------------------------------------------------------


def build_archetype_prompt(
    archetype: ArchetypeSpec,
    *,
    entities: dict[tuple[str, str], str],
    n: int,
    domain_description: str = "",
) -> str:
    """Render the full LLM prompt for ``archetype``.

    ``entities`` maps ``(role, slot)`` tuples to the canonical entity
    surface the LLM should express in each row, in whatever word
    form fits the sentence.  Keys line up with
    ``archetype.role_spans`` — the generator pre-samples them from
    the gazetteer or diversity taxonomy.
    """
    sections: list[str] = []

    # ── Task ─────────────────────────────────────────────────────────
    sections.append(
        f"# Task\nGenerate {n} short training rows for the "
        f"archetype **{archetype.name}**.",
    )
    if domain_description:
        sections.append(f"Domain context: {domain_description}")

    # ── Joint labels ─────────────────────────────────────────────────
    labels_lines = [
        f"- intent: {archetype.intent}",
        f"- admission: {archetype.admission}",
        f"- state_change: {archetype.state_change}",
    ]
    if archetype.topic:
        labels_lines.append(f"- topic: {archetype.topic}")
    sections.append(
        "# Joint labels (every row must express all of these)\n"
        + "\n".join(labels_lines),
    )

    # ── Archetype description ────────────────────────────────────────
    sections.append(f"# Archetype intent\n{archetype.description.strip()}")

    # ── Entities ─────────────────────────────────────────────────────
    if entities:
        ent_lines = ["Every row MUST naturally mention:"]
        # Group by role for readability.
        by_role: dict[str, list[tuple[str, str]]] = {}
        for (role, slot), surface in entities.items():
            by_role.setdefault(role, []).append((slot, surface))
        for role, items in by_role.items():
            for slot, surface in items:
                ent_lines.append(
                    f"- **{surface}** (role={role}, slot={slot}) — "
                    "use the surface as-is or inflect it naturally "
                    "(no paraphrasing to a different entity)."
                )
        sections.append("# Entities to express\n" + "\n".join(ent_lines))

    # ── Style envelope ───────────────────────────────────────────────
    style_lines = [
        f"- Each row between {archetype.target_min_chars} and "
        f"{archetype.target_max_chars} characters.",
        "- Natural conversational or clinical prose — not template fills.",
        "- Vary sentence structure, word choice, and register across rows.",
        "- Do NOT include any labels, JSON fields, or commentary in the rows "
        "themselves — the text IS the row.",
    ]
    if archetype.admission == "discard":
        style_lines.append(
            "- This archetype models content that should be DISCARDED "
            "(noise, chit-chat, meta-commentary).  Write rows that a "
            "memory system should reasonably choose to drop.",
        )
    if archetype.admission == "ephemeral":
        style_lines.append(
            "- This archetype models EPHEMERAL content — transient, "
            "time-bounded, not long-term knowledge.",
        )
    sections.append("# Style rules\n" + "\n".join(style_lines))

    # ── Few-shot examples ────────────────────────────────────────────
    if archetype.example_utterances:
        shots = "\n".join(f"- {ex.strip()}" for ex in archetype.example_utterances)
        sections.append(f"# Reference examples (match the STYLE, not the content)\n{shots}")

    # ── Phrasings as inspiration (not templates) ────────────────────
    if archetype.phrasings:
        ph = "\n".join(f"- {p.strip()}" for p in archetype.phrasings[:8])
        sections.append(
            "# Phrasing inspirations (paraphrase freely, don't copy verbatim)\n"
            + ph,
        )

    # ── Output format ────────────────────────────────────────────────
    sections.append(
        "# Output format\n"
        f"Return a JSON array of exactly {n} strings — nothing else, "
        "no keys, no markdown, no surrounding prose.",
    )

    return "\n\n".join(sections)


__all__ = ["build_archetype_prompt"]
