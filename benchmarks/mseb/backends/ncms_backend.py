"""NCMS backend — the reference implementation for MSEB.

Wraps :class:`ncms.application.memory_service.MemoryService` behind
the :class:`MemoryBackend` protocol.  Translates the harness's
:class:`FeatureSet` into :class:`NCMSConfig` overrides (every
ablation flag maps to a real NCMSConfig field — validated by
``benchmarks/mseb/harness.py``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from benchmarks.mseb.backends.base import BackendRanking

if TYPE_CHECKING:
    from benchmarks.mseb.harness import FeatureSet
    from benchmarks.mseb.schema import CorpusMemory

logger = logging.getLogger("mseb.backends.ncms")


@dataclass
class NcmsBackend:
    """Full NCMS pipeline: SQLite + Tantivy + NetworkX + SPLADE + SLM.

    Constructed by the harness's backend registry via
    ``make_backend("ncms", feature_set=..., adapter_domain=...)``.
    Heavy NCMS imports happen inside :meth:`setup` so the factory
    stays cheap when another backend is selected.
    """

    feature_set: FeatureSet
    adapter_domain: str | None = None
    shared_splade: object | None = None
    shared_intent_slot: object | None = None
    #: Extra NCMSConfig fields (e.g. ``scoring_weight_temporal=0.5``)
    #: passed by the harness when running ablation sweeps.  Applied
    #: after the feature-set overrides, so sweep-flags win.
    ncms_config_overrides: dict[str, float] = field(default_factory=dict)

    name: str = "ncms"
    _svc: object | None = field(default=None, init=False, repr=False)

    # -------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------

    async def setup(self) -> None:
        """Boot an in-memory NCMS instance with feature-set overrides."""
        from ncms.application.memory_service import MemoryService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.splade_engine import SpladeEngine
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(db_path=":memory:")
        await store.initialize()

        index = TantivyEngine()
        index.initialize()

        graph = NetworkXGraph()
        splade = self.shared_splade if self.shared_splade is not None else SpladeEngine()

        intent_slot = self.shared_intent_slot
        if (
            self.feature_set.slm
            and intent_slot is None
            and self.adapter_domain is not None
        ):
            from benchmarks.intent_slot_adapter import get_intent_slot_chain

            intent_slot = get_intent_slot_chain(
                domain=self.adapter_domain,
                include_e5_fallback=False,  # deterministic benchmarks
            )

        # Base config = FULL NCMS (TLG-on + SLM-on baseline).  Every
        # TLG feature that NCMSConfig ships default-off is explicitly
        # turned on here, so the benchmark actually measures TLG on
        # the baseline side.  --tlg-off flips these back off.  We
        # deliberately leave `admission_enabled` OFF — admission's
        # 3-way gate can DROP gold memories into ephemeral cache,
        # corrupting retrieval scoring.  Benchmark never wants that.
        base_kwargs: dict[str, object] = {
            "db_path": ":memory:",
            "actr_noise": 0.0,
            "splade_enabled": True,
            "scoring_weight_bm25": 0.6,
            "scoring_weight_actr": 0.0,
            "scoring_weight_splade": 0.3,
            "scoring_weight_graph": 0.3,
            "contradiction_detection_enabled": False,

            # -------- TLG features (all default-off on NCMSConfig) --------
            "reconciliation_enabled": True,         # Phase 2 — supersession penalties
            "episodes_enabled": True,               # Phase 3 — episode linker
            "intent_classification_enabled": True,  # Phase 4 — intent routing + hierarchy
            "intent_routing_enabled": True,
            "scoring_weight_hierarchy": 0.5,        # Phase 4 — intent hierarchy bonus
            "temporal_enabled": True,               # Phase 6 — temporal scoring signal
            # scoring_weight_temporal already 0.2 by default

            # -------- SLM (P2) --------
            "intent_slot_enabled": self.feature_set.slm and intent_slot is not None,
            "intent_slot_populate_domains": True,
        }
        base_kwargs.update(self.feature_set.to_ncms_config_overrides())
        # Sweep-level overrides land last so they beat ablation flags.
        if self.ncms_config_overrides:
            base_kwargs.update(self.ncms_config_overrides)
        config = NCMSConfig(**base_kwargs)

        svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
            splade=splade, intent_slot=intent_slot,
        )
        await svc.start_index_pool()
        self._svc = svc

        # Log the actual runtime config so the run-log is self-describing.
        # No guessing — every subsequent line in the log can be validated
        # against these values.
        logger.info(
            "NCMS runtime config: "
            "bm25=%.2f splade=%.2f graph=%.2f actr=%.2f temporal=%.2f "
            "hierarchy=%.2f recency=%.2f | "
            "reconciliation=%s episodes=%s "
            "intent_classification=%s intent_routing=%s temporal_enabled=%s "
            "admission=%s intent_slot=%s populate_domains=%s",
            config.scoring_weight_bm25, config.scoring_weight_splade,
            config.scoring_weight_graph, config.scoring_weight_actr,
            config.scoring_weight_temporal, config.scoring_weight_hierarchy,
            config.scoring_weight_recency,
            config.reconciliation_enabled, config.episodes_enabled,
            config.intent_classification_enabled, config.intent_routing_enabled,
            config.temporal_enabled,
            config.admission_enabled, config.intent_slot_enabled,
            config.intent_slot_populate_domains,
        )
        logger.info(
            "NCMS feature_set (harness flags): temporal=%s ordinal=%s "
            "retirement=%s causal=%s preference=%s slm=%s head=%s",
            self.feature_set.temporal, self.feature_set.ordinal,
            self.feature_set.retirement, self.feature_set.causal,
            self.feature_set.preference, self.feature_set.slm,
            self.feature_set.head,
        )

    # -------------------------------------------------------------------
    # Ingest
    # -------------------------------------------------------------------

    async def ingest(
        self, memories: list[CorpusMemory],
    ) -> dict[str, str]:
        """Store every memory in (subject, observed_at) order.

        Tags include ``mid:<corpus_mid>`` so we can recover the
        MSEB mid on the search side.
        """
        from ncms.application.memory_service import MemoryService

        if self._svc is None:
            raise RuntimeError("setup() must be called before ingest()")
        svc: MemoryService = self._svc  # type: ignore[assignment]

        mid_map: dict[str, str] = {}
        ordered = sorted(memories, key=lambda m: (m.subject, m.observed_at, m.mid))
        for m in ordered:
            try:
                observed_at = datetime.fromisoformat(
                    m.observed_at.replace("Z", "+00:00"),
                ).astimezone(UTC)
            except ValueError:
                observed_at = None
            memory = await svc.store_memory(
                content=m.content,
                memory_type="fact",
                source_agent=m.metadata.get("source_agent", "mseb"),
                domains=m.metadata.get("domains") or [],
                tags=["mseb", f"subject:{m.subject}", f"mid:{m.mid}"],
                observed_at=observed_at,
            )
            mid_map[m.mid] = memory.id

        # Block until every memory is fully indexed (BM25 + SPLADE +
        # GLiNER + graph edges).  store_memory() returns as soon as
        # SQLite persists; indexing is queued to a background pool.
        # Without this drain, early search() calls race against the
        # queue draining and miss recently-ingested memories — cell 1
        # of the previous ablation showed only 1,597/1,835 memories
        # indexed when queries ran, tanking rank-1.
        await svc.flush_indexing()
        logger.info(
            "ingest complete: %d memories, all indexed (pool drained)",
            len(mid_map),
        )
        return mid_map

    # -------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------

    async def search(
        self, query: str, *, limit: int = 10,
    ) -> list[BackendRanking]:
        from ncms.application.memory_service import MemoryService

        if self._svc is None:
            raise RuntimeError("setup() must be called before search()")
        svc: MemoryService = self._svc  # type: ignore[assignment]

        try:
            results = await svc.search(query=query, limit=limit)
        except Exception as exc:  # pragma: no cover — surface to harness
            logger.warning("search failed: %s", exc)
            return []

        rankings: list[BackendRanking] = []
        for rank, r in enumerate(results):
            memory = getattr(r, "memory", r)
            tags = getattr(memory, "tags", []) or []
            mid = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("mid:")),
                None,
            )
            if mid is None:
                continue
            score = float(getattr(r, "score", 1.0 / (rank + 1)))
            rankings.append(BackendRanking(mid=mid, score=score))
        return rankings

    # -------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------

    async def shutdown(self) -> None:
        svc = self._svc
        if svc is not None:
            # MemoryService has no explicit shutdown; the index pool
            # will tear down on GC.  If a future NCMS version adds
            # close(), wire it here.
            stop = getattr(svc, "stop_index_pool", None)
            if stop is not None:
                try:
                    await stop()
                except Exception as exc:  # pragma: no cover
                    logger.debug("stop_index_pool raised: %s", exc)

        # Flush the clock so downstream logs show a clean boundary
        # between runs in the same process.
        _ = time.perf_counter()


__all__ = ["NcmsBackend"]
