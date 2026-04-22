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

        # Base config = FULL NCMS (temporal stack on + SLM on baseline).
        # ``temporal_enabled=True`` is the master flag that gates: the
        # TLG grammar, reconciliation, episodes, intent classification,
        # intent routing, and the temporal scoring signal.  ``--temporal-off``
        # flips it back to False.  ``admission_enabled`` stays OFF — the
        # 3-way admission gate can DROP gold memories into ephemeral cache,
        # corrupting retrieval scoring.  Benchmark never wants that.
        base_kwargs: dict[str, object] = {
            "db_path": ":memory:",
            "actr_noise": 0.0,
            "splade_enabled": True,
            "scoring_weight_bm25": 0.6,
            "scoring_weight_actr": 0.0,
            "scoring_weight_splade": 0.3,
            "scoring_weight_graph": 0.3,
            "scoring_weight_hierarchy": 0.5,   # tuned temporal-layer weight
            "contradiction_detection_enabled": False,

            # -------- Master feature flags --------
            "temporal_enabled": True,
            "slm_enabled": self.feature_set.slm and intent_slot is not None,
            "slm_populate_domains": True,
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
        # Master flags appear first; scoring weights second; sub-knobs
        # third.  Grep this line in any run-log to verify what was ON.
        logger.info(
            "NCMS runtime config: "
            "temporal_enabled=%s slm_enabled=%s | "
            "bm25=%.2f splade=%.2f graph=%.2f actr=%.2f "
            "temporal=%.2f hierarchy=%.2f recency=%.2f | "
            "admission=%s populate_domains=%s",
            config.temporal_enabled, config.slm_enabled,
            config.scoring_weight_bm25, config.scoring_weight_splade,
            config.scoring_weight_graph, config.scoring_weight_actr,
            config.scoring_weight_temporal, config.scoring_weight_hierarchy,
            config.scoring_weight_recency,
            config.admission_enabled, config.slm_populate_domains,
        )
        logger.info(
            "NCMS feature_set (harness flags): temporal=%s slm=%s head=%s",
            self.feature_set.temporal, self.feature_set.slm,
            self.feature_set.head,
        )

        # Adapter provenance — make the loaded adapter's
        # domain/version/head set self-describing in the run-log.
        # Forensic tooling can grep "SLM adapter:" and prove which
        # adapter shipped each run.
        if intent_slot is not None:
            primary = None
            backends_list = getattr(intent_slot, "_backends", None) or []
            for b in backends_list:
                m = getattr(b, "_manifest", None) or getattr(b, "manifest", None)
                if m is not None:
                    primary = m
                    break
            if primary is not None:
                logger.info(
                    "SLM adapter: domain=%s version=%s encoder=%s "
                    "heads: intent=%d slot=%d topic=%d admission=%d "
                    "state_change=%d shape_intent=%d",
                    getattr(primary, "domain", "?"),
                    getattr(primary, "version", "?"),
                    getattr(primary, "encoder", "?"),
                    len(getattr(primary, "intent_labels", []) or []),
                    len(getattr(primary, "slot_labels", []) or []),
                    len(getattr(primary, "topic_labels", []) or []),
                    len(getattr(primary, "admission_labels", []) or []),
                    len(getattr(primary, "state_change_labels", []) or []),
                    len(getattr(primary, "shape_intent_labels", []) or []),
                )
                # The 6th head's trained vocabulary — critical for
                # knowing whether TLG dispatch has anything to route.
                shape_labels = (
                    getattr(primary, "shape_intent_labels", []) or []
                )
                if shape_labels:
                    logger.info(
                        "SLM shape_intent_labels: %s",
                        ", ".join(shape_labels),
                    )
                else:
                    logger.info(
                        "SLM shape_intent_labels: (empty — pre-v6 adapter, "
                        "TLG dispatch will abstain on every query)",
                    )
        else:
            logger.info(
                "SLM adapter: (none wired — feature_set.slm=%s, "
                "adapter_domain=%s)",
                self.feature_set.slm, self.adapter_domain,
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
    # Forensic: expose per-query SLM head outputs for predictions dump
    # -------------------------------------------------------------------

    def classify_query(self, query: str) -> dict[str, object]:
        """Run all 6 SLM heads on ``query`` and return a dict suitable
        for the harness's ``predictions.jsonl`` dump.

        Called once per query by the harness, alongside ``search()``.
        The output is persisted verbatim so post-hoc forensic tooling
        can trace — for any given query — which shape the SLM
        classified and whether dispatch abstained or routed.  This
        closes the gap that hid the ``tlg_enabled=False`` wiring
        bug in earlier runs.

        When no SLM is wired (``--slm-off`` or an adapter without a
        chain), returns an empty dict so downstream tooling can tell
        "SLM wasn't active" apart from "SLM returned None for every
        head".
        """
        svc = getattr(self, "_svc", None)
        if svc is None:
            return {}
        extractor = getattr(svc, "_intent_slot", None)
        if extractor is None:
            return {}
        adapter_name = (
            getattr(extractor, "adapter_domain", None)
            or getattr(self, "adapter_domain", None)
            or "unknown"
        )
        try:
            r = extractor.extract(query, domain=adapter_name or "unknown")
        except Exception as exc:  # pragma: no cover — forensic path
            return {"adapter": adapter_name, "error": repr(exc)}
        # Pull the manifest's version off the primary backend when
        # available so the dumped record self-describes the adapter.
        version: str | None = None
        backends = getattr(extractor, "_backends", None) or []
        for b in backends:
            m = getattr(b, "_manifest", None) or getattr(b, "manifest", None)
            if m is not None:
                version = getattr(m, "version", None)
                break
        adapter_label = (
            f"{adapter_name}/{version}" if version else adapter_name or "?"
        )
        return {
            "adapter":            adapter_label,
            "admission":          r.admission,
            "admission_conf":     r.admission_confidence,
            "state_change":       r.state_change,
            "state_change_conf":  r.state_change_confidence,
            "topic":              r.topic,
            "topic_conf":         r.topic_confidence,
            "intent":             r.intent,
            "intent_conf":        r.intent_confidence,
            "slots":              dict(r.slots),
            "shape_intent":       r.shape_intent,
            "shape_intent_conf":  r.shape_intent_confidence,
            "slm_latency_ms":     r.latency_ms,
        }

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
