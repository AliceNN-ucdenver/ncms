"""Bake the resolved subject list into ``Memory.structured`` (claim A.2).

Pure function.  Lives in its own module so the bake step is
trivially testable and the orchestrator can call one named thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ncms.domain.models import Subject


def bake_subjects_payload(
    *,
    subjects: list[Subject],
    structured: dict | None,
) -> dict:
    """Serialise resolved subjects into ``structured["subjects"]``.

    Claim A.2: every persisted memory has
    ``structured["subjects"] = list[dict]`` where each dict is
    ``Subject.model_dump(mode="json")`` (JSON-ready — tuples
    serialise as lists so SQLite round-trip preserves equality).
    When the resolved list is empty the key is set to ``[]``
    rather than omitted so downstream consumers can rely on the
    key existing on every post-Phase-A memory.
    """
    result = dict(structured or {})
    result["subjects"] = [s.model_dump(mode="json") for s in subjects]
    return result
