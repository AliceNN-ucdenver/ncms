"""Surface-form normalisation — maps raw text to a :class:`CatalogEntry`.

Public API:

  :func:`lookup(surface, domain) -> CatalogEntry | None`
      Case-insensitive exact-match lookup (canonical OR alias).

  :func:`pool_values(domain, slot) -> tuple[str, ...]`
      Extract the canonical surface forms for one slot, suitable for
      feeding a :class:`SlotPool` in the SDG template engine.

  :func:`canonical_slot(surface, domain) -> str | None`
      Return the authoritative slot for ``surface`` — the single
      source of truth for "what slot does MongoDB belong to?".
      Returns ``None`` when ``surface`` isn't in the catalog
      (downstream code should fall back to LLM-suggested labels or
      flag as "novel surface").

  :func:`topic_for(surface, domain) -> str | None`
      The ``object_to_topic`` label for a known surface, or None
      when novel.
"""

from __future__ import annotations

from ncms.application.adapters.schemas import DetectedSpan, Domain
from ncms.application.adapters.sdg.catalog.primitives import CatalogEntry

# Registry of per-domain catalogs.  New domains register their
# catalog module here.
_REGISTRY: dict[str, dict[str, CatalogEntry]] = {}
_ENTRIES_BY_SLOT: dict[str, dict[str, tuple[CatalogEntry, ...]]] = {}
# Cached surface-form list per domain, sorted longest-first.  Each
# entry is ``(surface_lower, canonical_entry, source_alias)``.  Built
# lazily on first ``detect_spans`` call so import stays cheap.
_SURFACE_INDEX: dict[str, tuple[tuple[str, CatalogEntry, str], ...]] = {}


def _ensure_loaded() -> None:
    """Lazily import the per-domain catalog modules."""
    if _REGISTRY:
        return
    try:
        from ncms.application.adapters.sdg.catalog import software_dev
        _REGISTRY["software_dev"] = software_dev.CATALOG
        _ENTRIES_BY_SLOT["software_dev"] = software_dev.ENTRIES_BY_SLOT
    except ImportError:  # pragma: no cover
        pass
    try:
        from ncms.application.adapters.sdg.catalog import clinical
        _REGISTRY["clinical"] = clinical.CATALOG
        _ENTRIES_BY_SLOT["clinical"] = clinical.ENTRIES_BY_SLOT
    except ImportError:  # pragma: no cover
        pass
    try:
        from ncms.application.adapters.sdg.catalog import conversational
        _REGISTRY["conversational"] = conversational.CATALOG
        _ENTRIES_BY_SLOT["conversational"] = conversational.ENTRIES_BY_SLOT
    except ImportError:  # pragma: no cover
        pass


def lookup(surface: str, *, domain: Domain) -> CatalogEntry | None:
    """Case-insensitive lookup against the domain catalog.

    Returns the :class:`CatalogEntry` for ``surface`` when it matches
    any entry's canonical form or any alias; ``None`` otherwise.
    """
    _ensure_loaded()
    catalog = _REGISTRY.get(domain)  # type: ignore[arg-type]
    if catalog is None:
        return None
    return catalog.get(surface.lower().strip())


def canonical_slot(surface: str, *, domain: Domain) -> str | None:
    """Return the authoritative slot for ``surface``, or None if novel."""
    entry = lookup(surface, domain=domain)
    return entry.slot if entry is not None else None


def topic_for(surface: str, *, domain: Domain) -> str | None:
    """Return the canonical topic label for ``surface``, or None if novel."""
    entry = lookup(surface, domain=domain)
    return entry.topic if entry is not None else None


def pool_values(domain: Domain, slot: str) -> tuple[str, ...]:
    """Return every canonical surface for ``(domain, slot)``.

    SDG templates use this instead of hand-coded tuples so the SDG
    pools and the authoritative catalog cannot drift apart.
    """
    _ensure_loaded()
    by_slot = _ENTRIES_BY_SLOT.get(domain)  # type: ignore[arg-type]
    if by_slot is None:
        return ()
    entries = by_slot.get(slot, ())
    return tuple(e.canonical for e in entries)


def pool_topic(domain: Domain, slot: str) -> str:
    """Return a representative topic for ``(domain, slot)`` pool.

    Uses the first entry in the slot's group.  All entries in a
    slot share the same topic by design.
    """
    _ensure_loaded()
    by_slot = _ENTRIES_BY_SLOT.get(domain)  # type: ignore[arg-type]
    if by_slot is None:
        return "other"
    entries = by_slot.get(slot, ())
    return entries[0].topic if entries else "other"


def _build_surface_index(
    domain: Domain,
) -> tuple[tuple[str, CatalogEntry, str], ...]:
    """Materialise (surface_lower, entry, alias) rows sorted longest-first.

    Longest-first ordering is essential for the scanner: "Docker
    Compose" must match before the shorter "Docker" surface when the
    input text contains "Docker Compose".  Aliases get their own row
    so the scanner records which alias fired (useful for debugging).
    """
    _ensure_loaded()
    catalog = _REGISTRY.get(domain)  # type: ignore[arg-type]
    if catalog is None:
        return ()
    # De-duplicate (surface, entry-identity) — the catalog dict keys
    # the canonical form; aliases contribute additional surfaces.
    seen: set[tuple[str, int]] = set()
    rows: list[tuple[str, CatalogEntry, str]] = []
    for entry in catalog.values():
        # Canonical form as a surface.
        key = (entry.canonical.lower(), id(entry))
        if key not in seen:
            seen.add(key)
            rows.append((entry.canonical.lower(), entry, ""))
        for alias in entry.aliases:
            k = (alias.lower(), id(entry))
            if k in seen:
                continue
            seen.add(k)
            rows.append((alias.lower(), entry, alias))
    # Longest-first so multi-word surfaces win over prefix matches.
    rows.sort(key=lambda r: len(r[0]), reverse=True)
    return tuple(rows)


def _surface_index(
    domain: Domain,
) -> tuple[tuple[str, CatalogEntry, str], ...]:
    """Cached accessor for the per-domain longest-first surface list."""
    cached = _SURFACE_INDEX.get(domain)  # type: ignore[arg-type]
    if cached is None:
        cached = _build_surface_index(domain)
        _SURFACE_INDEX[domain] = cached  # type: ignore[index]
    return cached


def _is_word_char(c: str) -> bool:
    """Identifier-character class.

    Only alphanumerics + underscore count.  Characters like ``.``,
    ``/``, ``#``, ``+`` are NOT identifier chars — so a sentence-
    ending period after ``FastAPI`` is a boundary, not a
    word-continuation.  Surfaces that legitimately contain those
    chars (``asp.net``, ``C#``, ``C++``, ``docker/compose``) still
    match because the longest-first scan tries the literal surface
    string first; the single word-char rule only decides whether a
    match candidate lives *inside* a larger token.
    """
    return c.isalnum() or c == "_"


def detect_spans(
    text: str,
    *,
    domain: Domain,
    min_len: int = 2,
) -> tuple[DetectedSpan, ...]:
    """Longest-match, non-overlapping gazetteer pass over ``text``.

    Scans every character offset in ``text`` and matches the longest
    catalog surface (canonical or alias) that starts there, respecting
    token boundaries (so ``ruby`` doesn't match inside ``rubygems``).
    When a match lands, the scanner advances past it — no overlapping
    hits.

    Case-insensitive on match; the returned ``surface`` preserves the
    original case as it appeared in the text.  ``canonical`` + ``slot``
    + ``topic`` come from the catalog entry.

    ``min_len`` guards against trivially short aliases (single-letter
    languages like ``R``) which almost always produce false positives
    in natural prose.  Callers that genuinely need 1-char matches can
    pass ``min_len=1``.
    """
    if not text:
        return ()
    index = _surface_index(domain)
    if not index:
        return ()
    lowered = text.lower()
    n = len(text)
    spans: list[DetectedSpan] = []
    pos = 0
    while pos < n:
        # Require left word boundary.
        if pos > 0 and _is_word_char(text[pos - 1]) and \
                _is_word_char(text[pos]):
            pos += 1
            continue
        # Try the longest surface that fits at this offset.
        matched = False
        for surface_lower, entry, alias in index:
            slen = len(surface_lower)
            if slen < min_len:
                continue
            end = pos + slen
            if end > n:
                continue
            if lowered[pos:end] != surface_lower:
                continue
            # Require right word boundary.
            if end < n and _is_word_char(text[end]) and \
                    _is_word_char(text[end - 1]):
                continue
            spans.append(DetectedSpan(
                char_start=pos,
                char_end=end,
                surface=text[pos:end],
                canonical=entry.canonical,
                slot=entry.slot,
                topic=entry.topic,
                source_alias=alias,
            ))
            pos = end
            matched = True
            break
        if not matched:
            pos += 1
    return tuple(spans)


def all_canonical_surfaces(domain: Domain) -> tuple[str, ...]:
    """Every canonical surface known for ``domain`` (excludes aliases).

    Used by the LLM-label validator to detect hallucinations — if
    the LLM emits a slot value that is neither a canonical surface
    NOR a substring of the content, it's rejected.
    """
    _ensure_loaded()
    by_slot = _ENTRIES_BY_SLOT.get(domain)  # type: ignore[arg-type]
    if by_slot is None:
        return ()
    return tuple(
        e.canonical for group in by_slot.values() for e in group
    )


__all__ = [
    "lookup",
    "canonical_slot",
    "topic_for",
    "pool_values",
    "pool_topic",
    "all_canonical_surfaces",
    "detect_spans",
]
