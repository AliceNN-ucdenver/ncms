"""Persistent wrapper around :class:`ncms.domain.tlg.QueryShapeCache`.

Bridges the pure in-memory cache to the ``grammar_shape_cache``
SQLite table so memoised shapes survive process restarts.  The
wrapper keeps a hot in-memory copy for fast lookups and writes-
through to the store on every learn event.

Usage is transparent to dispatch: the cache is consulted *before*
running the production list and populated *after* a successful
parse.  Abstentions and ``none`` intents are never cached (same
rule as the in-memory cache) so the persistent tier stays clean.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ncms.domain.tlg.shape_cache import QueryShapeCache, extract_skeleton
from ncms.domain.tlg.vocabulary import InducedVocabulary

logger = logging.getLogger(__name__)


class ShapeCacheStore:
    """Persistent QueryShapeCache backed by ``grammar_shape_cache``.

    Construction is cheap — the in-memory tier starts empty; call
    :meth:`warm` once at startup to hydrate from the store.  Every
    ``learn`` additionally writes through to the store so subsequent
    processes see the same skeletons.
    """

    def __init__(self) -> None:
        self._mem = QueryShapeCache()
        self._warmed = False

    async def warm(self, store: object) -> None:
        """Load persisted skeletons into the in-memory tier."""
        if self._warmed:
            return
        try:
            snapshot = await store.load_shape_cache()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover — defensive guard
            logger.warning(
                "TLG: could not warm shape cache from store: %s", exc,
            )
            return
        if snapshot:
            self._mem = QueryShapeCache.from_snapshot(snapshot)
        self._warmed = True

    def lookup(
        self, query: str, vocabulary: InducedVocabulary,
    ) -> tuple[str, dict[str, str]] | None:
        return self._mem.lookup(query, vocabulary)

    async def learn(
        self,
        store: object,
        query: str,
        intent: str,
        vocabulary: InducedVocabulary,
    ) -> None:
        """Memoise + persist a successful parse.

        Idempotent on conflict — same skeleton + same intent just
        bumps hit_count; same skeleton + different intent is a
        no-op (productions stay authoritative).
        """
        if intent in ("none", "abstain"):
            return
        self._mem.learn(query, intent, vocabulary)
        skel, slots = extract_skeleton(query, vocabulary)
        if not skel:
            return
        # Re-fetch the cached record to get the updated hit_count
        # + last_used for the persistence write.
        cached = self._mem._cache.get(skel)
        if cached is None:
            return
        try:
            await store.save_shape_cache_entry(  # type: ignore[attr-defined]
                skeleton=skel,
                intent=cached.intent,
                slot_names=list(cached.slot_names),
                hit_count=cached.hit_count,
                last_used=(
                    cached.last_used.isoformat()
                    if cached.last_used else datetime.now(UTC).isoformat()
                ),
            )
        except Exception:  # pragma: no cover — defensive guard
            logger.debug(
                "TLG: shape cache persistence failed for %s", skel,
                exc_info=True,
            )

    def size(self) -> int:
        return len(self._mem)
