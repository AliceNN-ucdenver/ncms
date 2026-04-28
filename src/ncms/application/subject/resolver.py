"""Ingest-time subject resolution + precedence chain (Phase A).

This module is the contract for claim A.3 — the precedence
chain that ``store_memory`` runs after SLM extraction and before
``Memory`` construction:

1. ``subjects=[...]`` (caller list) — wins.  When also passed
   ``subject="..."``, the legacy string MUST canonicalize to the
   same id as the explicit primary, otherwise raise (A.3
   cross-kwarg conflict).  The conflict check uses a deterministic
   formula — NO registry mutation happens before the raise.
2. ``subject="..."`` only — canonicalize and promote to a
   one-element list with ``primary=True``.
3. SLM ``role_head`` ``primary`` spans — claim A.17, the
   GLiNER-retirement path.  Gated on
   ``intent_slot_label.intent_confidence``
   ≥ ``config.slm_confidence_threshold``.
4. Otherwise — empty list.

The module also exposes :func:`link_resolved_subject_entities`,
which appends entity dicts for each resolved subject so
``MENTIONS_ENTITY`` edges in :mod:`edges` can find a target.
"""

from __future__ import annotations

from typing import Any

from ncms.application.subject.registry import SubjectRegistry
from ncms.application.subject.surface import slugify
from ncms.domain.models import Subject


def _canonicalize_subject_id(s: Subject) -> Subject:
    """Force a caller-asserted Subject's id into ``<type>:<slug>`` form.

    The Subject model docstring guarantees ids are always
    ``<type>:<canonical-slug>``.  Callers (notably the MSEB
    backend) sometimes construct Subjects with the raw surface
    as the id (``Subject(id="auth-service", type="subject")``),
    which violates that invariant.  This helper:

    * Returns the input unchanged when ``s.id`` already starts
      with ``"<s.type>:"``.
    * Otherwise mints a canonical id ``f"{s.type}:{slugify(s.id)}"``
      and prepends the original id to ``aliases`` so future
      surface-driven lookups via :meth:`SubjectRegistry.canonicalize`
      still find it.

    Pure function — no I/O, no registry mutation.  Idempotent.
    """
    expected_prefix = f"{s.type}:"
    if s.id.startswith(expected_prefix):
        return s
    canonical_id = f"{s.type}:{slugify(s.id)}"
    new_aliases = s.aliases if s.id in s.aliases else (s.id, *s.aliases)
    return s.model_copy(update={"id": canonical_id, "aliases": new_aliases})


def _validate_within_list_primary_count(subjects: list[Subject]) -> None:
    """Raise when more than one Subject has ``primary=True``.

    A.3 within-list conflict.  Distinct from the cross-kwarg
    conflict (handled in :func:`_validate_no_cross_kwarg_conflict`).
    """
    primaries = [s for s in subjects if s.primary]
    if len(primaries) > 1:
        raise ValueError(
            "Multiple subjects with primary=True: "
            + ", ".join(p.id for p in primaries),
        )


def _expected_canonical_id(
    subject_legacy: str,
    type_hint: str | None,
) -> str:
    """Compute the canonical id a legacy string WOULD slugify to.

    Pure formula (``f"{type}:{slugify(s)}"``) — does NOT touch the
    registry.  Used by :func:`_validate_no_cross_kwarg_conflict`
    so a failed validation does not leave behind a minted subject
    or alias row.

    The ``type_hint`` falls back to ``"subject"`` matching what
    :meth:`SubjectRegistry.canonicalize` does when no hint is
    given.
    """
    ctype = type_hint or "subject"
    return f"{ctype}:{slugify(subject_legacy)}"


def _validate_no_cross_kwarg_conflict(
    subject_legacy: str | None,
    explicit_subjects: list[Subject],
) -> None:
    """Raise when ``subject="x"`` and ``subjects=[Subject(id=y, primary)]``
    disagree on canonical id.

    The check is formula-based (no registry mutation): if
    ``slugify(subject_legacy)`` joined with the primary's type
    doesn't equal the primary's canonical id, the caller is
    asserting two different timelines as primary — raise.

    Trade-off: caller can't pin a Subject by canonical id and pass
    an alias-via-string and have them match through the alias
    chain.  They must agree at the slugify level OR pass canonical
    ids on both shapes.  Acceptable for Phase A — the conflict
    path catches typos, not alias resolution.
    """
    if not subject_legacy:
        return
    primary = next((s for s in explicit_subjects if s.primary), None)
    if primary is None:
        return
    expected = _expected_canonical_id(subject_legacy, primary.type)
    if expected != primary.id:
        raise ValueError(
            "Conflicting primary subjects: subject="
            f"{subject_legacy!r} canonicalizes to {expected!r}, "
            f"but subjects=[...] primary is {primary.id!r}.  "
            "Pass only one.",
        )


async def _derive_subjects_from_slm(
    registry: SubjectRegistry,
    *,
    intent_slot_label: Any,
    config: Any,
    domains: list[str] | None,
) -> list[Subject]:
    """Derive subjects from the SLM ``role_head`` ``primary`` spans.

    Implements claim A.17.  Skipped silently when:

    * ``intent_slot_label`` is ``None`` (chain dark).
    * ``intent_slot_label.intent_confidence`` is below
      ``config.slm_confidence_threshold``.  (The original code
      used ``getattr(label, "is_confident")``; that method is
      adapter-only — the domain ``ExtractedLabel`` doesn't have
      it, so the gate was silently always-False and let
      below-threshold extractions through.  Caught by codex
      round-1 audit on the Phase A PR.)
    * No span has role="primary".
    """
    if intent_slot_label is None:
        return []
    threshold = float(getattr(config, "slm_confidence_threshold", 0.3) or 0.3)
    intent_conf = float(getattr(intent_slot_label, "intent_confidence", 0.0) or 0.0)
    if intent_conf < threshold:
        return []

    role_spans = list(getattr(intent_slot_label, "role_spans", ()) or ())
    primary_spans = [rs for rs in role_spans if _role_of(rs) == "primary"]
    if not primary_spans:
        return []

    domain_hint = domains[0] if domains else None
    slot_confidences = dict(getattr(intent_slot_label, "slot_confidences", {}) or {})

    resolved: list[Subject] = []
    for i, rs in enumerate(primary_spans):
        surface, slot = _surface_and_slot(rs)
        if not surface:
            continue
        s = await registry.canonicalize(
            surface,
            type_hint=slot,
            domain=domain_hint,
            source="slm_role",
        )
        span_conf = float(slot_confidences.get(slot, intent_conf))
        resolved.append(
            s.model_copy(
                update={
                    "primary": (i == 0),
                    "confidence": min(s.confidence, span_conf),
                },
            ),
        )
    return resolved


def _role_of(rs: Any) -> str:
    if isinstance(rs, dict):
        return rs.get("role") or ""
    return getattr(rs, "role", "") or ""


def _surface_and_slot(rs: Any) -> tuple[str, str]:
    if isinstance(rs, dict):
        surface = rs.get("surface") or rs.get("canonical") or ""
        slot = rs.get("slot") or "subject"
    else:
        surface = getattr(rs, "surface", "") or getattr(rs, "canonical", "")
        slot = getattr(rs, "slot", "subject")
    return surface, slot


async def resolve_subjects(
    *,
    registry: SubjectRegistry,
    config: Any,
    domains: list[str] | None,
    subject_legacy: str | None,
    subjects_explicit: list[Subject] | None,
    intent_slot_label: Any | None,
) -> list[Subject]:
    """Compute the final subject list per claim A.3 precedence.

    Raises:
        ValueError: when (a) ``subjects_explicit`` contains more
            than one ``Subject`` with ``primary=True``, OR (b)
            ``subject_legacy`` and ``subjects_explicit`` disagree
            on the canonical id of the primary subject.  Both
            checks happen BEFORE any registry mutation.
    """
    # Precedence 1: caller list wins.
    if subjects_explicit is not None:
        # Pre-canonicalize each Subject's id to satisfy the
        # ``<type>:<slug>`` invariant before any validation runs.
        # Pure function; no registry mutation.
        canonicalized = [_canonicalize_subject_id(s) for s in subjects_explicit]

        # Validate FIRST — before any registry mutation.
        _validate_within_list_primary_count(canonicalized)
        _validate_no_cross_kwarg_conflict(subject_legacy, canonicalized)

        out = list(canonicalized)
        if out and not any(s.primary for s in out):
            out[0] = out[0].model_copy(update={"primary": True})

        # Persist only after validation succeeds.
        for s in out:
            await registry.register_caller_subject(s)
        return out

    # Precedence 2: legacy single-subject string.
    if subject_legacy:
        domain_hint = domains[0] if domains else None
        s = await registry.canonicalize(
            subject_legacy,
            type_hint=None,
            domain=domain_hint,
            source="caller",
        )
        return [s.model_copy(update={"primary": True})]

    # Precedence 3: SLM auto-suggest (A.17 GLiNER-retirement path).
    return await _derive_subjects_from_slm(
        registry,
        intent_slot_label=intent_slot_label,
        config=config,
        domains=domains,
    )


def link_resolved_subject_entities(
    merged_entities: list[dict],
    resolved_subjects: list[Subject],
) -> None:
    """Append entity dicts for any Subject not already linked.

    Phase A sub-PR 4 — generalizes the legacy ``if subject:``
    entity-link block to every resolved Subject.  Skips when an
    entity already exists for this subject (matched by canonical
    id OR any alias).  Mutates ``merged_entities`` in place.
    """
    for s in resolved_subjects:
        existing = {e["name"].lower() for e in merged_entities}
        if s.id.lower() in existing:
            continue
        if any(a.lower() in existing for a in s.aliases):
            continue
        merged_entities.append(
            {
                "name": s.id,
                "type": "subject",
                "attributes": {
                    "source": s.source,
                    "subject_type": s.type,
                    "primary": s.primary,
                    "aliases": list(s.aliases),
                    "confidence": s.confidence,
                },
            },
        )
