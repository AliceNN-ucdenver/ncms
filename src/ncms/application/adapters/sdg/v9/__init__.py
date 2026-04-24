"""v9 stratified-archetype corpus generation.

Produces per-domain training corpora (gold + sdg) for the v9 joint
5-head adapter.  Generation uses local Spark Nemotron (no foundation-
model runtime dependency) and enforces per-head class balance via
stratified archetypes.

Design doc: ``docs/research/v9-corpus-generation-design.md``.

Entry points:

* :class:`ArchetypeSpec` — one generation archetype (joint label
  combination + prompt template + target count)
* :mod:`ncms.application.adapters.sdg.v9.domains` — per-domain
  archetype registries

CLI:

* ``scripts/v9/generate_corpus.py`` — batch generation runner
"""

from ncms.application.adapters.sdg.v9.archetypes import (
    ArchetypeSpec,
    RoleSpec,
    validate_archetype_coverage,
)

__all__ = [
    "ArchetypeSpec",
    "RoleSpec",
    "validate_archetype_coverage",
]
