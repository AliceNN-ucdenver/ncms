"""v9 stratified-archetype corpus generation.

Produces per-domain training corpora (gold + sdg) for the v9 joint
5-head adapter.  Generation uses local Spark Nemotron (no foundation-
model runtime dependency) and enforces per-head class balance via
stratified archetypes loaded from the YAML plugin registry.

Design docs:

* ``docs/research/v9-corpus-generation-design.md`` — stratified
  archetype principle.
* ``docs/research/v9-domain-plugin-architecture.md`` — YAML plugin
  layout that feeds :class:`DomainSpec` → :func:`generate_domain`.

Public API:

* :class:`ArchetypeSpec` + :class:`RoleSpec` —
  archetype schema (loaded from ``archetypes.yaml``).
* :func:`validate_archetype_coverage` — per-head class-floor audit.
* :class:`LLMBackend` protocol + :class:`TemplateBackend` (no-LLM,
  deterministic) + :class:`SparkBackend` (live vLLM/Spark).
* :func:`build_archetype_prompt` — prompt construction for LLM backends.
* :func:`validate_and_label` — row validation + role-span labelling.
* :func:`generate_for_archetype` / :func:`generate_domain` — the
  orchestrators.

CLI entry point:

* ``ncms adapters generate-sdg --domain <name>`` (see
  ``interfaces/cli/adapters.py``).
"""

from ncms.application.adapters.sdg.v9.archetypes import (
    ArchetypeSpec,
    CoverageGap,
    RoleSpec,
    validate_archetype_coverage,
)
from ncms.application.adapters.sdg.v9.backends import (
    LLMBackend,
    SparkBackend,
    TemplateBackend,
)
from ncms.application.adapters.sdg.v9.generator import (
    GenerationStats,
    generate_domain,
    generate_for_archetype,
)
from ncms.application.adapters.sdg.v9.prompts import build_archetype_prompt
from ncms.application.adapters.sdg.v9.validation import (
    ValidationOutcome,
    validate_and_label,
)

__all__ = [
    "ArchetypeSpec",
    "CoverageGap",
    "GenerationStats",
    "LLMBackend",
    "RoleSpec",
    "SparkBackend",
    "TemplateBackend",
    "ValidationOutcome",
    "build_archetype_prompt",
    "generate_domain",
    "generate_for_archetype",
    "validate_and_label",
    "validate_archetype_coverage",
]
