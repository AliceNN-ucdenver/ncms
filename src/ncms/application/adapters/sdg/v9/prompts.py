"""Prompt construction for v9 stratified archetype generation.

The public entry is :func:`build_archetype_prompt`.  It renders a
single LLM prompt that asks for ``len(entity_rows)`` training rows
matching an archetype's joint-label specification.

Two design rules shape the prompt text:

1. **Never echo label vocabulary into the surface instructions.**
   Early B'.4 probing caught the LLM echoing prompt label strings
   ("persisting our decision to declare it...") directly into
   generated rows, which would teach a downstream classifier to
   recognise "persist / declare" as label indicators rather than
   learning real semantics.  The joint labels are therefore
   described behaviourally (see the ``_*_DESCRIPTIONS`` dicts
   below) instead of spelled out as their enum strings.

2. **One entity set per row, listed explicitly.**  A batch prompt
   that says "every row must mention metformin" degenerates into
   eight paraphrases of one sentence.  Instead the prompt
   enumerates ``row 1 → <entities_1>`` ... ``row N → <entities_N>``,
   which gives per-row diversity without blowing up the LLM call
   count.  Entity sampling happens in the generator; this module
   only renders what it's handed.
"""

from __future__ import annotations

from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec

# ---------------------------------------------------------------------------
# Behavioural descriptions for the five classification heads.
#
# These strings go into the prompt VERBATIM.  Keeping them out of
# the archetype YAML means every domain shares the same stable
# description vocabulary — if we later want to tune the phrasing,
# it's one edit here, not one edit per archetype.
# ---------------------------------------------------------------------------

_INTENT_DESCRIPTIONS: dict[str, str] = {
    "positive": (
        "Speaker expresses approval, adoption, enthusiasm, or commitment "
        "toward the subject."
    ),
    "negative": (
        "Speaker expresses disapproval, rejection, frustration, or rollback."
    ),
    "habitual": (
        "Describes a recurring routine, ongoing habit, or established "
        "pattern — no change of state is implied."
    ),
    "choice": (
        "Contrasts two named alternatives with a clear chosen winner "
        "(X over Y / X instead of Y / picked X over Y)."
    ),
    "difficulty": (
        "Expresses struggle, friction, or trouble — something isn't "
        "working as hoped."
    ),
    "none": (
        "Neutral factual statement — no expressed preference, emotion, "
        "or evaluation.  The subject is simply mentioned or described."
    ),
}

_ADMISSION_DESCRIPTIONS: dict[str, str] = {
    "persist": (
        "Meaningful, long-term content — facts, decisions, observations "
        "worth remembering weeks later."
    ),
    "ephemeral": (
        "Transient or time-bounded content — relevant right now but "
        "won't matter in a month (today's weather, one-off moods, "
        "scheduled one-time events)."
    ),
    "discard": (
        "Noise — chitchat, filler, meta-commentary, conversational "
        "lubricant — a memory system should drop this without storing "
        "anything."
    ),
}

_STATE_CHANGE_DESCRIPTIONS: dict[str, str] = {
    "declaration": (
        "Introduces a NEW state — a start, adoption, initiation, or "
        "first declaration of something."
    ),
    "retirement": (
        "Removes / stops / discontinues / deprecates something — the "
        "state is ending or has ended."
    ),
    "none": (
        "No state transition — ongoing, stable, or purely observational."
    ),
}


def _scenario_lines(archetype: ArchetypeSpec) -> list[str]:
    """Render the three joint labels as behavioural cues — NO literal
    label tokens appear in the returned text.

    If a head's value is missing from its description dict we fall
    through to the literal label; that keeps future label additions
    graceful at the cost of a one-time leak until the dict is
    updated.
    """
    intent_desc = _INTENT_DESCRIPTIONS.get(archetype.intent, archetype.intent)
    admission_desc = _ADMISSION_DESCRIPTIONS.get(
        archetype.admission, archetype.admission,
    )
    state_desc = _STATE_CHANGE_DESCRIPTIONS.get(
        archetype.state_change, archetype.state_change,
    )
    return [
        f"- Speaker stance: {intent_desc}",
        f"- Persistence: {admission_desc}",
        f"- State transition: {state_desc}",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_archetype_prompt(
    archetype: ArchetypeSpec,
    *,
    entity_rows: list[dict[tuple[str, str], str]],
    domain_description: str = "",
    speaker_voice: str = "",
) -> str:
    """Render the full LLM prompt for a batch of ``len(entity_rows)`` rows.

    Parameters
    ----------
    archetype : ArchetypeSpec
        The archetype being generated.
    entity_rows : list[dict[(role, slot), str]]
        One entity assignment per row — the LLM is told that row ``i``
        must naturally express ``entity_rows[i]``.  Per-row assignment
        is what gives the batch output surface diversity (without it
        all rows collapse to paraphrases of one sentence).
    domain_description : str
        Short blurb used once to frame the domain; optional.

    Returns
    -------
    str
        Complete user prompt.  The caller prepends a system message
        asking for a JSON array of strings.

    Notes
    -----
    This function is PURE: given the same archetype + entity_rows it
    produces byte-identical output.  That's intentional — the prompt
    is the single variable we control in LLM generation, so it should
    be reproducible in logs and diffs.
    """
    n = len(entity_rows)
    if n <= 0:
        raise ValueError("entity_rows must be non-empty")

    sections: list[str] = []

    # ── Task header ──────────────────────────────────────────────────
    sections.append(
        f"# Task\nGenerate {n} short training rows for the "
        f"**{archetype.name}** scenario.  Each row is a single "
        "self-contained sentence or short sentence pair.",
    )
    if domain_description:
        sections.append(f"Domain framing: {domain_description}")

    # ── Speaker voice constraint ─────────────────────────────────────
    # Strong hint to the LLM about WHO is speaking — pulled from
    # ``DomainSpec.speaker_voice``.  Prevents the pathological
    # subjects gpt-4o flagged in B'.7 ("zoroastrianism adopted keras
    # over huggingface" — the LLM had no constraint that the subject
    # had to be a software-engineering speaker).  Goes in its own
    # section so it stays visible to the model rather than being
    # buried in domain framing.
    if speaker_voice:
        sections.append(
            "# Speaker voice (every row must use this voice)\n"
            f"{speaker_voice.strip()}",
        )

    # ── Scenario (behavioural descriptions — NO label strings) ──────
    sections.append(
        "# Scenario (every row must express ALL three cues)\n"
        + "\n".join(_scenario_lines(archetype)),
    )

    # ── Archetype description ────────────────────────────────────────
    sections.append(f"# Scenario detail\n{archetype.description.strip()}")

    # ── Per-row entity assignments ──────────────────────────────────
    row_lines: list[str] = []
    for i, row_entities in enumerate(entity_rows, start=1):
        if not row_entities:
            row_lines.append(f"Row {i}: (no specific entities required)")
            continue
        parts: list[str] = []
        for (role, slot), surface in row_entities.items():
            # Strip any "#N" disambiguator from slot keys (internal-only).
            base_slot = slot.split("#", 1)[0]
            parts.append(f"{role}={surface!r} (slot={base_slot})")
        row_lines.append(f"Row {i}: must mention " + ", ".join(parts))
    sections.append(
        "# Per-row entity assignments\n"
        "Each row MUST naturally mention its assigned entities.  Use "
        "the surface as-is or inflect it naturally, but do NOT "
        "substitute different entities.\n"
        + "\n".join(row_lines),
    )

    # ── Style envelope ───────────────────────────────────────────────
    style_lines = [
        f"- Each row between {archetype.target_min_chars} and "
        f"{archetype.target_max_chars} characters.",
        "- Natural human prose appropriate to the domain — clinical "
        "notes, casual conversation, or engineering discussion as "
        "the scenario dictates.",
        "- Vary sentence structure, register, and word choice across "
        "the batch — do NOT reuse phrasings from earlier rows or "
        "from the reference examples.",
        "- Do NOT include JSON keys, labels, commentary, or row "
        "numbers INSIDE the row text — the string IS the row.",
        "- Do NOT echo any of the scenario cues ('stance', "
        "'persistence', 'transition') as literal words in the "
        "text; express the meaning naturally instead.",
    ]
    sections.append("# Style rules\n" + "\n".join(style_lines))

    # ── Few-shot examples (optional) ────────────────────────────────
    if archetype.example_utterances:
        shots = "\n".join(
            f"- {ex.strip()}" for ex in archetype.example_utterances[:6]
        )
        sections.append(
            "# Reference examples (match the STYLE; don't copy the content)\n"
            + shots,
        )

    # ── Phrasings as loose inspiration (not templates) ──────────────
    if archetype.phrasings:
        ph = "\n".join(f"- {p.strip()}" for p in archetype.phrasings[:6])
        sections.append(
            "# Phrasing inspirations (paraphrase freely, don't copy verbatim)\n"
            + ph,
        )

    # ── Output format ────────────────────────────────────────────────
    sections.append(
        "# Output format\n"
        f"Return a JSON array of exactly {n} strings — row 1 first, "
        f"row {n} last.  No keys, no markdown, no surrounding prose.",
    )

    return "\n\n".join(sections)


__all__ = ["build_archetype_prompt"]
