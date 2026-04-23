"""Authoritative-catalog primitives.

A :class:`CatalogEntry` is one authoritative surface-form entry.
Each entry binds:

  - ``canonical``:  the canonical lowercased surface form (dict key).
  - ``slot``:       the slot assignment — the single source of truth
                    for "what slot does MongoDB go in?".
  - ``topic``:      the ``object_to_topic`` label carried by this
                    surface (feeds the topic head).
  - ``aliases``:    alternative surface forms that map to the same
                    canonical entry ("postgres" / "postgresql" /
                    "postgres sql" all → postgres).
  - ``source``:     free-form citation — Wikidata QID, Wikipedia
                    category, SO tag wiki reference, etc.  Optional
                    but strongly encouraged so future maintainers
                    can audit the assignment.
  - ``notes``:      optional reviewer notes.

Catalogs are Python dicts (not YAML) so they're type-checked at
import time and grep-able in one file per domain.

Lookup order in :mod:`normalize` is: exact canonical → lowercase →
each alias.  No fuzzy matching — if a surface isn't in the catalog,
downstream code (LLM labeller fallback, "novel surface" flag)
handles it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogEntry:
    """One authoritative surface-form entry in a domain catalog."""

    canonical: str
    slot: str
    topic: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    notes: str = ""

    def matches(self, surface: str) -> bool:
        """True when ``surface`` (case-insensitive) is canonical or an alias."""
        s = surface.lower().strip()
        if s == self.canonical.lower():
            return True
        return any(s == a.lower() for a in self.aliases)


__all__ = ["CatalogEntry"]
