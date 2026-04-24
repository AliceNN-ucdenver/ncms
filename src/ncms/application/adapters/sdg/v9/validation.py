"""Post-generation row validation + role-span labelling.

Every raw text row produced by a backend runs through
:func:`validate_and_label` before becoming a :class:`GoldExample`.
The validator enforces four invariants:

1. **Length envelope** — ``len(text)`` must fall inside the
   archetype's ``[target_min_chars, target_max_chars]`` window.
2. **Entity presence** — every pre-sampled entity from the
   generator MUST appear in the text (case-insensitive substring
   match, allowing for minor inflection).
3. **Role-span composition** — the gazetteer pass (or open-vocab
   fallback) must produce spans whose ``(role, slot, count)``
   distribution matches ``archetype.role_spans`` exactly.  Extra
   spans are allowed if they fit the ``not_relevant`` role bucket;
   missing spans are a rejection.
4. **No placeholder leakage** — text must not contain literal
   ``{name}`` tokens, which would indicate template substitution
   failed upstream.

Returns a :class:`ValidationOutcome` so the generator can log
rejections and retry at the batch level without losing context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ncms.application.adapters.schemas import Domain, RoleSpan
from ncms.application.adapters.sdg.catalog import detect_spans
from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec


RejectionReason = Literal[
    "too_short",
    "too_long",
    "placeholder_leak",
    "missing_entity",
    "wrong_role_spans",
    "empty_text",
]


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of validating one candidate row.

    ``ok`` is ``True`` iff every invariant passed.  When ``ok`` is
    ``False``, ``reason`` carries a machine-readable rejection
    category and ``detail`` a human-readable explanation.  When
    ``ok`` is ``True``, ``role_spans`` is the labelled span list
    ready to attach to the emitted :class:`GoldExample`.
    """

    ok: bool
    role_spans: tuple[RoleSpan, ...] = ()
    reason: RejectionReason | None = None
    detail: str = ""


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def validate_and_label(
    text: str,
    *,
    archetype: ArchetypeSpec,
    entities: dict[tuple[str, str], str],
    domain: Domain,
) -> ValidationOutcome:
    """Validate one candidate row and produce its labelled role spans.

    Parameters
    ----------
    text : str
        The raw text row from the backend.
    archetype : ArchetypeSpec
        Archetype the row was generated for.  Its ``role_spans`` +
        length envelope drive validation.
    entities : dict[(role, slot), str]
        Pre-sampled entities the generator asked the backend to
        express.  Every one must appear in ``text``.
    domain : Domain
        Used to run the gazetteer pass via :func:`detect_spans`.

    Returns
    -------
    ValidationOutcome
        ``ok=True`` on success with ``role_spans`` set; otherwise
        ``ok=False`` with a categorised rejection reason.
    """
    clean = text.strip()
    if not clean:
        return ValidationOutcome(ok=False, reason="empty_text", detail="blank row")

    if len(clean) < archetype.target_min_chars:
        return ValidationOutcome(
            ok=False, reason="too_short",
            detail=f"len={len(clean)} < {archetype.target_min_chars}",
        )
    if len(clean) > archetype.target_max_chars:
        return ValidationOutcome(
            ok=False, reason="too_long",
            detail=f"len={len(clean)} > {archetype.target_max_chars}",
        )

    if _PLACEHOLDER_RE.search(clean):
        leaks = _PLACEHOLDER_RE.findall(clean)
        return ValidationOutcome(
            ok=False, reason="placeholder_leak",
            detail=f"unfilled placeholders: {leaks}",
        )

    # Entity presence — case-insensitive substring match is a cheap
    # proxy for "the LLM mentioned this entity".  We don't require
    # exact canonical form since natural prose may inflect.
    clean_lower = clean.lower()
    missing: list[str] = []
    for (_role, _slot), surface in entities.items():
        if surface.lower() not in clean_lower:
            missing.append(surface)
    if missing:
        return ValidationOutcome(
            ok=False, reason="missing_entity",
            detail=f"entities absent from text: {missing}",
        )

    # Run the gazetteer pass.  Works for catalog-backed domains
    # (software_dev, clinical).  For open-vocab domains (conversational)
    # detect_spans returns () — we fall through to synthetic spans
    # built from the ``entities`` dict so the row still carries
    # ground-truth role labels.
    gaz_spans = detect_spans(clean, domain=domain)

    role_spans: list[RoleSpan]
    if gaz_spans:
        role_spans = _label_from_gazetteer(gaz_spans, entities)
    else:
        role_spans = _label_open_vocab(clean, entities)

    # Verify role-span composition matches archetype declaration.
    composition_ok, detail = _check_role_composition(role_spans, archetype)
    if not composition_ok:
        return ValidationOutcome(
            ok=False, reason="wrong_role_spans", detail=detail,
        )

    return ValidationOutcome(ok=True, role_spans=tuple(role_spans))


# ---------------------------------------------------------------------------
# Role-label assignment
# ---------------------------------------------------------------------------


def _label_from_gazetteer(
    gaz_spans: tuple,
    entities: dict[tuple[str, str], str],
) -> list[RoleSpan]:
    """Turn gazetteer-detected spans into labelled :class:`RoleSpan`s.

    Every sampled entity of role ``primary`` / ``alternative`` /
    ``casual`` in ``entities`` carries a ``(canonical, slot)``
    signature.  Match gazetteer spans against those signatures:

    * signature hit → span gets the declared role
    * signature miss → span gets role ``not_relevant`` (the
      gazetteer detected a known surface the generator didn't ask
      for — useful negative training signal)
    """
    signature_to_role: dict[tuple[str, str], str] = {}
    for (role, slot), surface in entities.items():
        signature_to_role[(surface.lower().strip(), slot)] = role

    out: list[RoleSpan] = []
    for span in gaz_spans:
        key = (span.canonical.lower().strip(), span.slot)
        role = signature_to_role.get(key, "not_relevant")
        out.append(RoleSpan(
            char_start=span.char_start,
            char_end=span.char_end,
            surface=span.surface,
            canonical=span.canonical,
            slot=span.slot,
            role=role,  # type: ignore[arg-type]
            source="sdg-v9",
        ))
    return out


def _label_open_vocab(
    text: str,
    entities: dict[tuple[str, str], str],
) -> list[RoleSpan]:
    """For open-vocab domains: synthesise :class:`RoleSpan` rows by
    locating each sampled entity surface in the text.

    This is coarser than the gazetteer path (case-insensitive
    substring; picks the first occurrence) but it's sufficient to
    produce training labels for conversational rows where no
    catalog exists.
    """
    text_lower = text.lower()
    out: list[RoleSpan] = []
    for (role, slot), surface in entities.items():
        idx = text_lower.find(surface.lower())
        if idx < 0:
            continue
        out.append(RoleSpan(
            char_start=idx,
            char_end=idx + len(surface),
            surface=text[idx:idx + len(surface)],
            canonical=surface,
            slot=slot,
            role=role,  # type: ignore[arg-type]
            source="sdg-v9-openvocab",
        ))
    return out


def _check_role_composition(
    role_spans: list[RoleSpan],
    archetype: ArchetypeSpec,
) -> tuple[bool, str]:
    """Check that ``role_spans`` matches the archetype's declared
    composition.

    We count spans per ``(role, slot)`` and compare against
    ``archetype.role_spans``'s declared counts.  ``not_relevant``
    spans don't count toward required slots but are always
    permitted — they represent real gazetteer hits the archetype
    didn't request.
    """
    required: dict[tuple[str, str], int] = {}
    for rs in archetype.role_spans:
        if rs.count <= 0:
            continue
        required[(rs.role, rs.slot)] = required.get((rs.role, rs.slot), 0) + rs.count

    found: dict[tuple[str, str], int] = {}
    for rs in role_spans:
        if rs.role == "not_relevant":
            continue
        key = (rs.role, rs.slot)
        found[key] = found.get(key, 0) + 1

    if found != required:
        return False, f"expected {required}, got {found}"
    return True, ""


__all__ = [
    "RejectionReason",
    "ValidationOutcome",
    "validate_and_label",
]
