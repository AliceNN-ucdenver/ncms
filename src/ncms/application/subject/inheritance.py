"""Parent-document subject inheritance (claim A.10).

When a child memory is ingested with ``parent_doc_id`` and the
caller did not provide subjects + the SLM did not auto-suggest,
the child inherits the parent document's primary subject.

Lookup chain (hash-independent, infrastructure-agnostic):

1. ``parent_doc_id`` → fetch :class:`Document` from the document
   store (validates existence; early-out on miss).
2. ``MemoryStore.find_memory_by_doc_id(parent_doc_id)`` returns
   the profile :class:`Memory` whose ``structured.doc_id`` equals
   ``parent_doc_id``.  This is the existing ``section_service``
   invariant: every profile memory created via
   ``ingest_navigable`` stamps ``structured["doc_id"]`` with the
   originating Document id.
3. Read ``structured["subjects"]`` on the parent profile memory,
   take the first ``primary=True`` entry, return it tagged with
   ``source="document"`` and a dampened confidence so audit
   trails reflect that this was inherited.

Application layer only — no SQLite imports, no row mappers.
The store protocol owns the JSON-extract query.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ncms.domain.models import Subject

if TYPE_CHECKING:
    from ncms.domain.protocols import MemoryStore

logger = logging.getLogger(__name__)


def _extract_primary_from_payload(
    raw_subjects: list,
) -> Subject | None:
    """Pick the first ``primary=True`` Subject dict from the payload."""
    for entry in raw_subjects:
        if not isinstance(entry, dict) or not entry.get("primary"):
            continue
        try:
            return Subject(**entry)
        except (TypeError, ValueError):
            continue
    return None


async def inherit_primary_subject_from_parent_doc(
    *,
    store: MemoryStore,
    document_service: Any | None,
    parent_doc_id: str,
) -> Subject | None:
    """Look up the parent doc's primary subject, or return ``None``.

    Returns ``None`` when:

    * No document service is wired.
    * No parent document with that id exists.
    * No profile memory carries
      ``structured.doc_id == parent_doc_id``.
    * The profile memory's payload has no ``primary=True`` entry.

    The inherited Subject is returned with ``source="document"``
    and ``confidence`` capped at ``0.9`` so audit trails know this
    was inherited rather than caller-asserted.
    """
    if document_service is None:
        logger.debug(
            "[subjects] parent-doc inheritance: no document_service wired",
        )
        return None
    parent_doc = await document_service.get_document(parent_doc_id)
    if parent_doc is None:
        logger.debug(
            "[subjects] parent-doc inheritance: no document with id %s",
            parent_doc_id,
        )
        return None

    parent_mem = await store.find_memory_by_doc_id(parent_doc_id)
    if parent_mem is None or not parent_mem.structured:
        logger.debug(
            "[subjects] parent-doc inheritance: no profile memory "
            "with doc_id=%s",
            parent_doc_id,
        )
        return None

    raw_subjects = parent_mem.structured.get("subjects") or []
    inherited = _extract_primary_from_payload(raw_subjects)
    if inherited is None:
        logger.debug(
            "[subjects] parent-doc inheritance: parent profile memory "
            "for %s has no primary subject",
            parent_doc_id,
        )
        return None
    return inherited.model_copy(
        update={
            "source": "document",
            "confidence": min(inherited.confidence, 0.9),
        },
    )


__all__ = ["inherit_primary_subject_from_parent_doc"]
