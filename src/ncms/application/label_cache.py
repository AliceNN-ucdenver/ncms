"""Shared helper for loading cached entity labels from consolidation_state.

Used by ``MemoryService.recall``, the retrieval pipeline, and the
ingestion pipeline.  Before this extraction each pipeline held a
callable pointer back into ``MemoryService._get_cached_labels``, which
was awkward DI.  This module owns the logic as a free async function
that takes only the store it needs.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ncms.domain.protocols import MemoryStore

logger = logging.getLogger(__name__)


async def load_cached_labels(
    store: MemoryStore, domains: list[str],
) -> dict:
    """Return cached entity labels for the given domains plus the
    ``_keep_universal`` flag.

    Returns an empty dict when no labels are cached.  Exceptions in
    the per-key JSON parse are swallowed — this is read-only and
    non-fatal.
    """
    cached: dict = {}
    for domain in domains:
        raw = await store.get_consolidation_value(
            f"entity_labels:{domain}",
        )
        if not raw:
            continue
        try:
            labels = json.loads(raw)
            if isinstance(labels, list):
                cached[domain] = labels
        except Exception:
            logger.debug(
                "Invalid JSON for entity_labels:%s, ignoring",
                domain, exc_info=True,
            )

    raw_ku = await store.get_consolidation_value("_keep_universal")
    if raw_ku:
        with contextlib.suppress(Exception):
            cached["_keep_universal"] = json.loads(raw_ku)

    return cached
