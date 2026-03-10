"""Consolidation Service - background memory maintenance.

Handles memory decay, pruning, and importance-threshold-triggered consolidation.
"""

from __future__ import annotations

import logging

from ncms.config import NCMSConfig
from ncms.domain.scoring import base_level_activation
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ConsolidationService:
    """Background maintenance for memory health."""

    def __init__(self, store: SQLiteStore, config: NCMSConfig | None = None):
        self._store = store
        self._config = config or NCMSConfig()

    async def run_decay_pass(self) -> int:
        """Recompute activation scores and flag low-activation memories.

        Returns the number of memories below threshold.
        """
        memories = await self._store.list_memories(limit=100000)
        below_threshold = 0

        for memory in memories:
            access_ages = await self._store.get_access_times(memory.id)
            bl = base_level_activation(access_ages, decay=self._config.actr_decay)

            if bl < self._config.actr_threshold:
                below_threshold += 1
                logger.debug(
                    "Memory %s below threshold: activation=%.2f", memory.id, bl
                )

        logger.info(
            "Decay pass complete: %d/%d memories below threshold",
            below_threshold,
            len(memories),
        )
        return below_threshold
