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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from benchmarks.mseb.backends.base import BackendRanking

if TYPE_CHECKING:
    from benchmarks.mseb.harness import FeatureSet
    from benchmarks.mseb.schema import CorpusMemory

logger = logging.getLogger("mseb.backends.ncms")


def _subject_diagnostics(
    *,
    baseline: tuple[str, ...],
    subject_map: dict[str, str],
    id_map: dict[str, str],
    gold_subject: str | None,
) -> dict[str, object]:
    """Summarize whether retrieved candidates expose the gold subject."""
    ranked_subjects: list[dict[str, object]] = []
    first_subject_rank: dict[str, int] = {}
    counts: Counter[str] = Counter()
    gold_rank: int | None = None
    for rank, ncms_id in enumerate(baseline, start=1):
        subject = subject_map.get(ncms_id)
        if not subject:
            continue
        counts[subject] += 1
        first_subject_rank.setdefault(subject, rank)
        if gold_subject is not None and subject == gold_subject and gold_rank is None:
            gold_rank = rank
        ranked_subjects.append(
            {
                "rank": rank,
                "candidate": id_map.get(ncms_id, ncms_id),
                "subject": subject,
            }
        )

    top_subject: str | None = None
    top_subject_count = 0
    if counts:
        top_subject, top_subject_count = counts.most_common(1)[0]

    return {
        "gold_subject": gold_subject,
        "gold_subject_rank": gold_rank,
        "gold_subject_in_candidates": gold_rank is not None,
        "top_subject": top_subject,
        "top_subject_count": top_subject_count,
        "top_subject_is_gold": bool(gold_subject and top_subject == gold_subject),
        "unique_subjects": len(counts),
        "subject_counts": dict(counts.most_common()),
        "first_subject_ranks": first_subject_rank,
        "ranked_subjects": ranked_subjects,
    }


def _subject_anchor_candidates(
    *,
    candidate_ids: tuple[str, ...],
    subject_map: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Order one representative candidate per subject for anchor probing."""
    counts: Counter[str] = Counter()
    harmonic: Counter[str] = Counter()
    best_rank: dict[str, int] = {}
    best_candidate: dict[str, str] = {}
    for rank, ncms_id in enumerate(candidate_ids, start=1):
        subject = subject_map.get(ncms_id)
        if not subject:
            continue
        counts[subject] += 1
        harmonic[subject] += 1.0 / rank
        if subject not in best_rank:
            best_rank[subject] = rank
            best_candidate[subject] = ncms_id
    ordered_subjects = sorted(
        counts,
        key=lambda s: (
            -counts[s],
            -harmonic[s],
            best_rank[s],
            s,
        ),
    )
    return tuple((best_candidate[subject], subject) for subject in ordered_subjects)


def _subject_anchor_stats(
    *,
    candidate_ids: tuple[str, ...],
    subject_map: dict[str, str],
) -> dict[str, dict[str, float | int | str]]:
    counts: Counter[str] = Counter()
    harmonic: Counter[str] = Counter()
    best_rank: dict[str, int] = {}
    best_candidate: dict[str, str] = {}
    for rank, ncms_id in enumerate(candidate_ids, start=1):
        subject = subject_map.get(ncms_id)
        if not subject:
            continue
        counts[subject] += 1
        harmonic[subject] += 1.0 / rank
        if subject not in best_rank:
            best_rank[subject] = rank
            best_candidate[subject] = ncms_id
    return {
        subject: {
            "count": counts[subject],
            "harmonic": float(harmonic[subject]),
            "best_rank": best_rank[subject],
            "best_candidate": best_candidate[subject],
        }
        for subject in counts
    }


def _oracle_subject_candidate(
    *,
    gold_subject: str | None,
    candidate_ids: tuple[str, ...],
    subject_map: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return a single candidate tuple pinned to the gold subject."""
    if not gold_subject:
        return ()
    for ncms_id in candidate_ids:
        if subject_map.get(ncms_id) == gold_subject:
            return ((ncms_id, gold_subject),)
    return ((f"oracle:{gold_subject}", gold_subject),)


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
    ctlg_adapter_domain: str | None = None
    ctlg_adapter_version: str | None = None
    shared_splade: object | None = None
    shared_intent_slot: object | None = None
    shared_ctlg_cue_tagger: object | None = None
    #: Extra NCMSConfig fields (e.g. ``scoring_weight_temporal=0.5``,
    #: ``splade_enabled=False``) passed by the harness when running
    #: ablation sweeps.  Applied after feature-set overrides, so
    #: sweep flags win.
    ncms_config_overrides: dict[str, object] = field(default_factory=dict)

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
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(db_path=":memory:")
        await store.initialize()

        index = TantivyEngine()
        index.initialize()

        graph = NetworkXGraph()

        intent_slot = self.shared_intent_slot
        if self.feature_set.slm and intent_slot is None and self.adapter_domain is not None:
            from benchmarks.intent_slot_adapter import get_intent_slot_chain

            intent_slot = get_intent_slot_chain(
                domain=self.adapter_domain,
                include_e5_fallback=False,  # deterministic benchmarks
            )

        # The ``--slm-off`` ablation now expresses itself as
        # ``intent_slot=None`` rather than a config boolean — the
        # legacy ``slm_enabled`` config field was deleted in the
        # Phase I.6 cleanup.  When ``feature_set.slm=False`` we
        # simply don't pass a chain to MemoryService; ingestion
        # short-circuits and falls back to the heuristic chain.
        if not self.feature_set.slm:
            intent_slot = None

        ctlg_cue_tagger = self.shared_ctlg_cue_tagger
        if (
            self.feature_set.temporal
            and ctlg_cue_tagger is None
            and self.ctlg_adapter_domain is not None
        ):
            from ncms.application.ctlg_cue_tagger import build_default_ctlg_cue_tagger

            ctlg_cue_tagger = build_default_ctlg_cue_tagger(
                domain=self.ctlg_adapter_domain,
                version=self.ctlg_adapter_version,
                required=False,
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
            "scoring_weight_hierarchy": 0.5,  # tuned temporal-layer weight
            "contradiction_detection_enabled": False,
            # -------- Master feature flag --------
            "temporal_enabled": True,
            "slm_populate_domains": True,
        }
        base_kwargs.update(self.feature_set.to_ncms_config_overrides())
        # Sweep-level overrides land last so they beat ablation flags.
        if self.ncms_config_overrides:
            base_kwargs.update(self.ncms_config_overrides)
        config = NCMSConfig(**base_kwargs)

        splade = None
        if config.splade_enabled:
            from ncms.infrastructure.indexing.splade_engine import SpladeEngine

            splade = self.shared_splade if self.shared_splade is not None else SpladeEngine()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=config,
            splade=splade,
            intent_slot=intent_slot,
            ctlg_cue_tagger=ctlg_cue_tagger,
        )
        await svc.start_index_pool()
        self._svc = svc

        # Log the actual runtime config so the run-log is self-describing.
        # Master flags appear first; scoring weights second; sub-knobs
        # third.  Grep this line in any run-log to verify what was ON.
        logger.info(
            "NCMS runtime config: "
            "temporal_enabled=%s slm_chain=%s ctlg_chain=%s | "
            "entity_mode=%s splade_enabled=%s | "
            "bm25=%.2f splade=%.2f graph=%.2f actr=%.2f "
            "temporal=%.2f hierarchy=%.2f recency=%.2f | "
            "admission=%s populate_domains=%s",
            config.temporal_enabled,
            "loaded" if intent_slot is not None else "off",
            "loaded" if ctlg_cue_tagger is not None else "off",
            config.entity_extraction_mode,
            config.splade_enabled,
            config.scoring_weight_bm25,
            config.scoring_weight_splade,
            config.scoring_weight_graph,
            config.scoring_weight_actr,
            config.scoring_weight_temporal,
            config.scoring_weight_hierarchy,
            config.scoring_weight_recency,
            config.admission_enabled,
            config.slm_populate_domains,
        )
        logger.info(
            "NCMS feature_set (harness flags): temporal=%s slm=%s head=%s",
            self.feature_set.temporal,
            self.feature_set.slm,
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
                    "heads: intent=%d role=%d topic=%d admission=%d "
                    "state_change=%d shape_intent=%d cue=%d",
                    getattr(primary, "domain", "?"),
                    getattr(primary, "version", "?"),
                    getattr(primary, "encoder", "?"),
                    len(getattr(primary, "intent_labels", []) or []),
                    len(getattr(primary, "role_labels", []) or []),
                    len(getattr(primary, "topic_labels", []) or []),
                    len(getattr(primary, "admission_labels", []) or []),
                    len(getattr(primary, "state_change_labels", []) or []),
                    len(getattr(primary, "shape_intent_labels", []) or []),
                    len(getattr(primary, "cue_labels", []) or []),
                )
                # CTLG cue vocabulary — critical for knowing whether
                # the v8+ TLG dispatch path has anything to route.
                cue_labels = getattr(primary, "cue_labels", []) or []
                if cue_labels:
                    logger.info(
                        "SLM cue_labels: %d tags (%s...)",
                        len(cue_labels),
                        ", ".join(cue_labels[:8]),
                    )
                else:
                    logger.info(
                        "SLM cue_labels: (empty — pre-v8 adapter, "
                        "CTLG synthesizer will abstain on every query)",
                    )
        else:
            logger.info(
                "SLM adapter: (none wired — feature_set.slm=%s, adapter_domain=%s)",
                self.feature_set.slm,
                self.adapter_domain,
            )
        if ctlg_cue_tagger is not None:
            manifest = getattr(ctlg_cue_tagger, "manifest", None)
            logger.info(
                "CTLG adapter: domain=%s version=%s encoder=%s labels=%d",
                getattr(manifest, "domain", self.ctlg_adapter_domain),
                getattr(manifest, "version", self.ctlg_adapter_version),
                getattr(manifest, "encoder", "?"),
                len(getattr(manifest, "cue_labels", []) or []),
            )
        else:
            logger.info(
                "CTLG adapter: (none wired — ctlg_adapter_domain=%s)",
                self.ctlg_adapter_domain,
            )

    # -------------------------------------------------------------------
    # Ingest
    # -------------------------------------------------------------------

    async def ingest(
        self,
        memories: list[CorpusMemory],
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
                # Option D' Part 4: pass MSEB subject as first-class
                # kwarg so the ingest pipeline forces creation of an
                # L2 ENTITY_STATE node with entity_id=subject, seeding
                # the TLG L1 vocabulary induction.
                subject=m.subject,
                tags=["mseb", f"mid:{m.mid}"],
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

        # Cache reverse map (ncms_id -> mseb_mid) for the per-stage
        # candidate translation in :meth:`search_with_stages`.  Each
        # search stage returns NCMS internal IDs; gold-recall stats
        # need MSEB mids on both sides.
        self._ncms_to_mseb: dict[str, str] = {
            ncms_id: mseb_mid for mseb_mid, ncms_id in mid_map.items()
        }
        subject_by_mid = {m.mid: m.subject for m in memories}
        self._ncms_to_subject: dict[str, str] = {
            ncms_id: subject_by_mid[mseb_mid]
            for mseb_mid, ncms_id in mid_map.items()
            if mseb_mid in subject_by_mid
        }
        return mid_map

    # -------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[BackendRanking]:
        rankings, _ = await self.search_with_stages(
            query,
            limit=limit,
            capture_stages=False,
        )
        return rankings

    async def search_with_stages(
        self,
        query: str,
        *,
        limit: int = 10,
        capture_stages: bool = True,
    ) -> tuple[list[BackendRanking], dict[str, list[str]]]:
        """Search with optional per-stage candidate capture.

        When ``capture_stages=True``, returns ``(rankings, stages)``
        where ``stages`` maps stage name → memory-ID list at that
        stage (``bm25``, ``splade``, ``rrf_fused``, ``expanded``,
        ``scored``, ``returned``).  Used by the harness to compute
        recall@K-by-stage diagnostics; production callers go through
        :meth:`search` (no stage capture, no overhead).

        The IDs in each stage list are NCMS memory IDs (UUID-ish).
        Translation to MSEB ``mid`` tag values happens in the
        harness when needed (gold IDs are MSEB tags, not memory
        IDs — the harness already does this lookup for the final
        ranking).
        """
        from ncms.application.memory_service import MemoryService

        if self._svc is None:
            raise RuntimeError("setup() must be called before search()")
        svc: MemoryService = self._svc  # type: ignore[assignment]

        stages: dict[str, list[str]] = {} if capture_stages else None  # type: ignore[assignment]
        self._last_stage_ncms = {}
        try:
            results = await svc.search(
                query=query,
                limit=limit,
                stage_candidates_out=stages,
            )
        except Exception as exc:  # pragma: no cover — surface to harness
            logger.warning("search failed: %s", exc)
            return [], (stages or {})

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

        # Translate per-stage NCMS internal IDs -> MSEB mids using
        # the reverse map cached during ingest.  IDs without a tag
        # mapping (e.g. supplementary candidates injected by
        # intent-driven expansion that aren't in the gold corpus)
        # are dropped from the stage list.
        if stages is not None:
            self._last_stage_ncms = {stage: list(nids) for stage, nids in stages.items()}
        if stages:
            ncms_to_mseb = getattr(self, "_ncms_to_mseb", {})
            stages = {
                stage: [ncms_to_mseb[nid] for nid in nids if nid in ncms_to_mseb]
                for stage, nids in stages.items()
            }
        return rankings, (stages or {})

    async def ctlg_shadow_query(
        self,
        query: str,
        *,
        gold_mids: set[str],
        gold_subject: str | None = None,
    ) -> dict[str, object]:
        """Run CTLG in shadow mode against the last captured search ranking.

        This is benchmark-only diagnostics: it computes the rank movement CTLG
        would propose, but the live ``search()`` result remains the only ranking
        the MSEB scorer sees.
        """
        from ncms.application.adapters.ctlg import (
            run_candidate_grounded_ctlg_shadow,
            run_ctlg_shadow,
            serialize_harness_result,
        )
        from ncms.application.memory_service import MemoryService

        if self._svc is None:
            return {}
        svc: MemoryService = self._svc  # type: ignore[assignment]
        baseline = tuple((getattr(self, "_last_stage_ncms", {}) or {}).get("returned", []))
        if not baseline:
            return {}
        id_map = getattr(self, "_ncms_to_mseb", {})
        subject_map = getattr(self, "_ncms_to_subject", {})

        async def _resolve_node_id(node_id: str) -> str | None:
            try:
                node = await svc.store.get_memory_node(node_id)
            except Exception:
                return None
            return node.memory_id if node is not None else node_id

        result = await run_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            resolve_id_fn=_resolve_node_id,
        )
        payload = serialize_harness_result(
            result,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        candidate_subjects = subject_map
        grounded = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=candidate_subjects,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
        )
        payload["candidate_grounded"] = serialize_harness_result(
            grounded,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        guarded = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=candidate_subjects,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
            require_answer_in_ranking=True,
        )
        payload["candidate_grounded_existing_only"] = serialize_harness_result(
            guarded,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        stages = getattr(self, "_last_stage_ncms", {}) or {}
        anchor_stage = "scored"
        anchor_pool = tuple(stages.get(anchor_stage) or ())
        if not anchor_pool:
            anchor_stage = "expanded"
            anchor_pool = tuple(stages.get(anchor_stage) or ())
        if not anchor_pool:
            anchor_stage = "returned"
            anchor_pool = baseline
        subject_anchor_candidates = _subject_anchor_candidates(
            candidate_ids=anchor_pool,
            subject_map=subject_map,
        )
        subject_anchor = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=subject_anchor_candidates,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
            require_answer_in_ranking=True,
        )
        subject_anchor_payload = serialize_harness_result(
            subject_anchor,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        subject_anchor_payload["anchor_stage"] = anchor_stage
        subject_anchor_payload["anchor_pool_size"] = len(anchor_pool)
        subject_anchor_payload["anchor_subject_diagnostics"] = _subject_diagnostics(
            baseline=anchor_pool,
            subject_map=subject_map,
            id_map=id_map,
            gold_subject=gold_subject,
        )
        payload["subject_anchor_existing_only"] = subject_anchor_payload
        subject_anchor_scored = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=subject_anchor_candidates,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
            require_answer_in_ranking=True,
            allowed_answer_ids=anchor_pool,
        )
        subject_anchor_scored_payload = serialize_harness_result(
            subject_anchor_scored,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        subject_anchor_scored_payload["anchor_stage"] = anchor_stage
        subject_anchor_scored_payload["anchor_pool_size"] = len(anchor_pool)
        subject_anchor_scored_payload["anchor_subject_diagnostics"] = _subject_diagnostics(
            baseline=anchor_pool,
            subject_map=subject_map,
            id_map=id_map,
            gold_subject=gold_subject,
        )
        payload["subject_anchor_scored_pool"] = subject_anchor_scored_payload

        anchor_stats = _subject_anchor_stats(
            candidate_ids=anchor_pool,
            subject_map=subject_map,
        )
        max_count = max((float(stat["count"]) for stat in anchor_stats.values()), default=1.0)
        max_harmonic = max(
            (float(stat["harmonic"]) for stat in anchor_stats.values()), default=1.0
        )
        resolver_rows: list[dict[str, object]] = []
        resolver_results = []
        for candidate_id, subject in subject_anchor_candidates:
            candidate_result = await run_candidate_grounded_ctlg_shadow(
                query,
                retrieve_lg_fn=svc.retrieve_lg,
                bm25_ranking=baseline,
                candidate_subjects=((candidate_id, subject),),
                domain=self.adapter_domain or "",
                cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
                cue_tags=result.cue_tags,
                tlg_query=result.tlg_query,
                resolve_id_fn=_resolve_node_id,
                override_existing_subject=True,
                require_answer_in_ranking=True,
                allowed_answer_ids=anchor_pool,
            )
            stat = anchor_stats.get(subject, {})
            best_rank = int(stat.get("best_rank", len(anchor_pool) + 1))
            count_norm = float(stat.get("count", 0)) / max_count
            harmonic_norm = float(stat.get("harmonic", 0.0)) / max_harmonic
            inverse_rank = 1.0 / max(best_rank, 1)
            attempt = (
                dict(candidate_result.candidate_attempts[0])
                if candidate_result.candidate_attempts
                else {}
            )
            relation_supported = bool(candidate_result.would_compose)
            answer_in_pool = bool(attempt.get("answer_in_allowed_ids"))
            score = (
                0.25 * harmonic_norm
                + 0.20 * count_norm
                + 0.15 * inverse_rank
                + 0.25 * float(relation_supported)
                + 0.15 * float(answer_in_pool)
            )
            row = {
                "candidate": id_map.get(candidate_id, candidate_id),
                "subject": subject,
                "score": round(score, 6),
                "count": stat.get("count", 0),
                "harmonic": round(float(stat.get("harmonic", 0.0)), 6),
                "best_rank": best_rank,
                "relation_supported": relation_supported,
                "answer_in_pool": answer_in_pool,
                "grammar_answer": (
                    id_map.get(
                        str(attempt.get("grammar_answer")),
                        str(attempt.get("grammar_answer")),
                    )
                    if attempt.get("grammar_answer")
                    else None
                ),
                "proof": attempt.get("proof"),
            }
            resolver_rows.append(row)
            resolver_results.append((score, subject, candidate_id, candidate_result))

        resolver_results.sort(key=lambda item: (-item[0], item[1]))
        selected_resolver = resolver_results[0] if resolver_results else None
        second_score = resolver_results[1][0] if len(resolver_results) > 1 else 0.0
        selected_score = selected_resolver[0] if selected_resolver is not None else 0.0
        selected_result = selected_resolver[3] if selected_resolver is not None else None
        selected_margin = selected_score - second_score
        source_tlg_query = result.tlg_query
        ambiguous_deictic_subject = (
            source_tlg_query is not None
            and source_tlg_query.subject is None
            and source_tlg_query.referent is None
            and sum(1 for _, _, _, candidate in resolver_results if candidate.would_compose) > 1
        )
        resolver_accept = bool(
            selected_result is not None
            and selected_result.would_compose
            and not ambiguous_deictic_subject
            and selected_score >= 0.45
            and selected_margin >= 0.03
        )
        if resolver_accept and selected_result is not None:
            resolver_payload = serialize_harness_result(
                selected_result,
                gold_ids=tuple(gold_mids),
                id_map=id_map,
            )
            resolver_payload["abstention_reason"] = ""
        else:
            resolver_payload = serialize_harness_result(
                await run_candidate_grounded_ctlg_shadow(
                    query,
                    retrieve_lg_fn=svc.retrieve_lg,
                    bm25_ranking=baseline,
                    candidate_subjects=(),
                    domain=self.adapter_domain or "",
                    cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
                    cue_tags=result.cue_tags,
                    tlg_query=result.tlg_query,
                    resolve_id_fn=_resolve_node_id,
                    override_existing_subject=True,
                    require_answer_in_ranking=True,
                    allowed_answer_ids=anchor_pool,
                ),
                gold_ids=tuple(gold_mids),
                id_map=id_map,
            )
            if selected_result is not None and not selected_result.would_compose:
                resolver_payload["abstention_reason"] = "resolver_selected_no_composition"
            elif ambiguous_deictic_subject:
                resolver_payload["abstention_reason"] = "resolver_ambiguous_deictic_subject"
            elif selected_result is not None and selected_margin < 0.03:
                resolver_payload["abstention_reason"] = "resolver_margin_too_low"
            elif selected_result is not None and selected_score < 0.45:
                resolver_payload["abstention_reason"] = "resolver_score_too_low"
        resolver_payload["resolver_selected_subject"] = (
            selected_resolver[1] if selected_resolver is not None else None
        )
        resolver_payload["resolver_selected_score"] = round(selected_score, 6)
        resolver_payload["resolver_margin"] = round(selected_margin, 6)
        resolver_payload["resolver_candidates"] = sorted(
            resolver_rows,
            key=lambda row: (-float(row["score"]), str(row["subject"])),
        )
        resolver_payload["anchor_stage"] = anchor_stage
        resolver_payload["anchor_pool_size"] = len(anchor_pool)
        payload["subject_resolver_scored_pool"] = resolver_payload

        oracle_subject_candidates = _oracle_subject_candidate(
            gold_subject=gold_subject,
            candidate_ids=anchor_pool,
            subject_map=subject_map,
        )
        oracle_existing = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=oracle_subject_candidates,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
            require_answer_in_ranking=True,
        )
        oracle_existing_payload = serialize_harness_result(
            oracle_existing,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        oracle_existing_payload["anchor_stage"] = "returned"
        oracle_existing_payload["anchor_pool_size"] = len(baseline)
        payload["oracle_subject_existing_only"] = oracle_existing_payload
        oracle_scored = await run_candidate_grounded_ctlg_shadow(
            query,
            retrieve_lg_fn=svc.retrieve_lg,
            bm25_ranking=baseline,
            candidate_subjects=oracle_subject_candidates,
            domain=self.adapter_domain or "",
            cue_tagger=getattr(svc, "_ctlg_cue_tagger", None),
            cue_tags=result.cue_tags,
            tlg_query=result.tlg_query,
            resolve_id_fn=_resolve_node_id,
            override_existing_subject=True,
            require_answer_in_ranking=True,
            allowed_answer_ids=anchor_pool,
        )
        oracle_scored_payload = serialize_harness_result(
            oracle_scored,
            gold_ids=tuple(gold_mids),
            id_map=id_map,
        )
        oracle_scored_payload["anchor_stage"] = anchor_stage
        oracle_scored_payload["anchor_pool_size"] = len(anchor_pool)
        payload["oracle_subject_scored_pool"] = oracle_scored_payload
        payload["subject_diagnostics"] = _subject_diagnostics(
            baseline=baseline,
            subject_map=subject_map,
            id_map=id_map,
            gold_subject=gold_subject,
        )
        return payload

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
        adapter_label = f"{adapter_name}/{version}" if version else adapter_name or "?"
        return {
            "adapter": adapter_label,
            "admission": r.admission,
            "admission_conf": r.admission_confidence,
            "state_change": r.state_change,
            "state_change_conf": r.state_change_confidence,
            "topic": r.topic,
            "topic_conf": r.topic_confidence,
            "intent": r.intent,
            "intent_conf": r.intent_confidence,
            "slots": dict(r.slots),
            "shape_intent": r.shape_intent,
            "shape_intent_conf": r.shape_intent_confidence,
            "slm_latency_ms": r.latency_ms,
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
