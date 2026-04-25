"""NCMS configuration via Pydantic Settings.

Configuration can be set via environment variables with the NCMS_ prefix,
or by passing values directly to NCMSConfig().

Feature Flag Tiers:
  - ALWAYS ON: No flag — behavior is unconditional (async indexing, graph
    expansion, co-occurrence edges, PPR, bus surrogates).  These matured
    through Phases 1-4 and have no reason to disable.
  - PRODUCTION: Default False, but all Docker configs enable them as a
    bundle.  Still useful to disable individually for debugging.
  - ADVANCED: Off by default, enable selectively (dream cycles, LLM
    fallbacks, synthesis, maintenance).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class NCMSConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NCMS_")

    # Storage paths
    db_path: str = str(Path.home() / ".ncms" / "ncms.db")
    index_path: str = str(Path.home() / ".ncms" / "index")

    # ACT-R parameters
    actr_decay: float = 0.5
    actr_noise: float = 0.25
    actr_threshold: float = -2.0
    actr_temperature: float = 0.4
    actr_max_spread: float = 1.0

    # Knowledge Bus
    bus_ask_timeout_ms: int = 5000

    # LLM (used by contradiction detection)
    llm_model: str = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    llm_api_base: str | None = "http://spark-ee7d.local:8000/v1"

    # SPLADE
    splade_enabled: bool = False
    splade_model: str = "naver/splade-v3"
    splade_top_k: int = 50
    scoring_weight_splade: float = 0.3  # Tuned: grid search on SciFact (2026-03-14)

    # Contradiction detection (uses llm_model + llm_api_base for the LLM)
    contradiction_detection_enabled: bool = False
    contradiction_candidate_limit: int = 5

    # Snapshot
    snapshot_max_entries: int = 50
    snapshot_ttl_hours: int = 168

    # Retrieval pipeline
    tier1_candidates: int = 50
    tier2_candidates: int = 20
    scoring_weight_bm25: float = 0.6   # Tuned: grid search on SciFact (2026-03-14)
    scoring_weight_actr: float = 0.0   # Tuned: ACT-R hurts on cold corpora (no access history)
    scoring_weight_graph: float = 0.3  # Restored: graph signal helps baseline (+10% AR)

    # Graph expansion (always on)
    graph_expansion_depth: int = 1
    graph_expansion_max: int = 10

    # Co-occurrence edges (always on)
    cooccurrence_max_entities: int = 12  # Reduced from 20 to cap clique inflation

    # Graph spreading activation (PPR, always on)
    graph_hop_decay: float = 0.5       # Activation multiplier per hop
    graph_spreading_max_hops: int = 2  # Maximum hops for graph traversal

    # Recency scoring
    scoring_weight_recency: float = 0.0   # Additive recency weight (0 = disabled)
    recency_half_life_days: float = 30.0  # Half-life for exponential recency decay

    # Model cache directory (for GLiNER / SPLADE / sentence-transformers downloads)
    # Defaults to ~/.cache/huggingface/hub if not set
    model_cache_dir: str | None = None

    # Content classification (Phase 4 content-aware ingestion)
    content_classification_enabled: bool = False

    # Content size gating (Phase 1 data integrity)
    max_content_length: int = 5000

    # GLiNER entity extraction (required dependency)
    gliner_model: str = "urchade/gliner_medium-v2.1"
    gliner_threshold: float = 0.3

    # Label detection via LLM (for `ncms topics detect`)
    label_detection_model: str = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    label_detection_api_base: str | None = "http://spark-ee7d.local:8000/v1"

    # Consolidation
    consolidation_importance_threshold: float = 50.0
    consolidation_enabled: bool = True

    # Knowledge consolidation (Phase 4)
    consolidation_knowledge_enabled: bool = False
    consolidation_knowledge_min_cluster_size: int = 3
    consolidation_knowledge_model: str = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    consolidation_knowledge_api_base: str | None = "http://spark-ee7d.local:8000/v1"
    consolidation_knowledge_max_insights_per_run: int = 5

    # Admission scoring (Phase 1)
    admission_enabled: bool = False
    admission_ephemeral_ttl_seconds: int = 3600

    # State reconciliation (Phase 2) — gated by ``temporal_enabled``.
    reconciliation_importance_boost: float = 0.5
    reconciliation_supersession_penalty: float = 0.3
    reconciliation_conflict_penalty: float = 0.15

    # Episode formation (Phase 3) — gated by ``temporal_enabled``.
    episode_window_minutes: int = 1440   # T_window for temporal proximity signal
    episode_close_minutes: int = 1440    # T_close for auto-closure
    episode_match_threshold: float = 0.30  # Weighted score threshold for joining
    episode_create_min_entities: int = 2   # Min entities to create new episode
    episode_candidate_limit: int = 10      # BM25/SPLADE candidate limit
    episode_weight_bm25: float = 0.20
    episode_weight_splade: float = 0.20    # Redistributed when SPLADE disabled
    episode_weight_entity_overlap: float = 0.25
    episode_weight_domain: float = 0.15
    episode_weight_temporal: float = 0.10
    episode_weight_agent: float = 0.05
    episode_weight_anchor: float = 0.05

    # Intent-aware retrieval (Phase 4) — gated by ``temporal_enabled``.
    intent_confidence_threshold: float = 0.6   # Below → fall back to fact_lookup
    intent_hierarchy_bonus: float = 0.5        # Raw bonus before weight
    scoring_weight_hierarchy: float = 0.0      # Additive weight (0 = no effect)
    intent_supplement_max: int = 20            # Max supplementary candidates per intent
    intent_llm_fallback_enabled: bool = False  # LLM fallback when BM25 confidence low

    # Phase H.3 — role-grounding bonus.  The 5-head SLM emits a
    # per-span ``role`` label (primary / alternative / casual /
    # not_relevant) for every gazetteer-detected span in a memory's
    # content.  When the query mentions an entity, memories where
    # that entity has ``role=primary`` are genuinely *about* the
    # entity (vs incidentally string-matching it).  This boost
    # rewards primary-role matches additively on ``combined``.
    #
    # Default ``weight=0.0`` (off).  The MSEB v1 ablation showed
    # the v9 role_head emits "syntactically primary" not "answer-
    # relevance primary" — for "what alternatives were considered
    # in <decision>?" queries, the role_head tags the OLD chosen
    # entity as primary, which inverts retrieval ordering on
    # ``predecessor`` / ``retirement`` shapes.  Net softwaredev r@1:
    # 0.7455 → 0.7212 (−2.4 pts) at weight=0.5.  Same opt-in pattern
    # as ``scoring_weight_hierarchy=0.0`` — the primitive ships as a
    # building block; deployments enable it after verifying role_head
    # accuracy on their domain.  Future v9 role_head retraining may
    # change the default once "answer-relevance primary" is the
    # supervised target.  See ``docs/v9-mseb-slm-lift-findings.md``.
    role_grounding_bonus: float = 0.5
    scoring_weight_role_grounding: float = 0.0

    # Phase H.2 — per-memory state_change × QueryIntent alignment.
    # When CHANGE_DETECTION queries arrive, memories whose 5-head SLM
    # tagged them as ``state_change ∈ {declaration, retirement}`` ARE
    # the change events the query is asking about.  Reuses the same
    # primitive as H.1 (``intent_alignment_bonus`` is generic over
    # label fields).  Default ``weight=0.5`` (on) — the table is
    # narrow (CHANGE_DETECTION only) and the surface area in MSEB v1
    # is small (5 queries), so the regression risk is bounded; verify
    # via the ``--state-change-alignment-weight 0.0`` ablation flag.
    state_change_alignment_bonus: float = 0.5
    scoring_weight_state_change_alignment: float = 0.5

    # Phase H.1 — per-memory intent-label × QueryIntent alignment bonus.
    # The 5-head SLM emits a per-memory intent label (positive /
    # negative / habitual / difficulty / choice / none).  When the
    # classified QueryIntent has a defined alignment rule (see
    # ``ScoringPipeline._INTENT_ALIGNMENT_TABLE``), memories whose
    # intent label is in the aligned set get an additive boost.
    # ``intent_alignment_bonus`` is the raw bonus magnitude; the
    # ``scoring_weight_intent_alignment`` weight scales it.  Both
    # default to 0.5 so the max contribution to ``combined`` is 0.25
    # — comparable to a moderate normalised BM25 signal.  Gated by
    # ``temporal_enabled`` (the master flag that turns on the BM25
    # exemplar QueryIntent classifier in the first place); without
    # a classified query intent there is nothing to align against.
    intent_alignment_bonus: float = 0.5
    scoring_weight_intent_alignment: float = 0.5

    # Episode LLM fallback (Phase 3 tuning)
    episode_llm_fallback_enabled: bool = False  # LLM fallback when no episode matches

    # Hierarchical consolidation (Phase 5)
    episode_consolidation_enabled: bool = False      # 5A: Episode summary generation
    trajectory_consolidation_enabled: bool = False   # 5B: State trajectory narratives
    pattern_consolidation_enabled: bool = False      # 5C: Recurring pattern detection
    trajectory_min_transitions: int = 3              # Min state transitions for trajectory
    pattern_min_episodes: int = 3                    # Min episodes for pattern cluster
    pattern_entity_overlap_threshold: float = 0.3    # Jaccard threshold for clustering
    pattern_stability_threshold: float = 0.7         # Promote to strategic_insight above this
    abstract_refresh_days: int = 7                   # Staleness window for re-synthesis
    consolidation_max_abstracts_per_run: int = 10    # Cap per consolidation pass

    # ── Master temporal reasoning flag ────────────────────────────────
    # When True, the retrieval pipeline runs the full temporal stack:
    #   - TLG grammar composition (query_parser + zone dispatch)
    #   - State reconciliation (supersedes/refines/conflicts edges)
    #   - Episode formation (7-signal hybrid linker)
    #   - Intent classification (BM25 exemplar)
    #   - Intent routing (supplementary candidates by classified intent)
    #   - Temporal scoring signal (``scoring_weight_temporal``)
    #   - Hierarchy bonus for intent-matched node types
    # When False, only the hybrid retrieval core (BM25+SPLADE+graph+ACT-R)
    # runs; the pipeline has no concept of state transitions or temporal
    # query shapes.  Tuning is via the ``scoring_weight_*`` floats + the
    # per-subsystem knobs (reconciliation_*, episode_*, intent_*, etc.).
    temporal_enabled: bool = False
    scoring_weight_temporal: float = 0.2  # Weight of the temporal signal when temporal_enabled=True

    # P1-temporal-experiment: GLiNER-extracted date ranges + hard-filter
    # retrieval.  See docs/retired/p1-temporal-experiment.md (historical).
    temporal_range_filter_enabled: bool = False
    # Policy for memories with no extracted content range when the query
    # produces one.  "include" = recall-safe (pass filter), "exclude" =
    # precision-safe (drop).  Default recall-safe.
    temporal_missing_range_policy: Literal["include", "exclude"] = "include"

    # ── 5-head SLM master flag ────────────────────────────────────────
    # When True, ``IngestionPipeline`` runs the LoRA multi-head
    # classifier on every ``store_memory`` call and persists
    # {intent, slot, topic, admission, state_change} to the
    # ``memories`` columns + ``memory_slots`` table.  Replaces the
    # regex-based admission scorer, the state-change regex in
    # index_worker, and the LLM topic labeller.
    #
    # Default flipped to True in Phase I.6 (2026-04-25).  The flag
    # remains as a kill-switch for cold-start deployments that don't
    # have an adapter deployed AND don't want the SLM startup cost.
    # When True but no ``intent_slot`` chain is injected (i.e.
    # ``MemoryService(intent_slot=None)``), the IngestionPipeline
    # short-circuits the SLM extraction and falls back to the
    # heuristic chain.  So flipping this default is safe even on
    # production paths that haven't yet been wired to load adapters.
    # The next retirement step (delete the flag entirely) is gated
    # on retiring the regex/heuristic code paths -- tracked in
    # ``docs/v9-mseb-slm-lift-findings.md``.
    slm_enabled: bool = True
    # Adapter artifact path (lora_adapter/ + heads.safetensors +
    # manifest.json).  None → skip the custom primary and fall
    # through to the generic/zero-shot chain.
    slm_checkpoint_dir: str | None = None
    # Confidence floor for head-by-head fallback — below this value
    # the chain moves to the next backend's output for that head.
    #
    # Default lowered 0.7 → 0.3 with the v9 adapters (Phase E.1).
    # v6/v7 adapters were trained on a smaller, less-diverse corpus
    # and produced over-confident predictions (≥ 0.9 on most
    # held-out rows).  v9 adapters are better calibrated: per-head
    # held-out distribution shows 100% accuracy at conf ≥ 0.3
    # across all three domains, with ~2-5% of correct predictions
    # falling in the 0.3-0.7 confidence band.  A 0.7 floor would
    # silently drop those to the heuristic fallback.  0.3 admits
    # the calibrated low-confidence-but-correct predictions while
    # still gating obvious model abstentions (which cluster near
    # 0.0 because cross-entropy spreads probability across classes
    # when the model is genuinely uncertain).
    slm_confidence_threshold: float = 0.3
    # When True, append the topic-head label to Memory.domains
    # automatically.  Set False during migration to keep callers
    # explicitly controlling the domain tag set.
    slm_populate_domains: bool = True
    # Include the E5-small-v2 zero-shot learned fallback in the
    # chain.  Set False for minimal-dependency deployments that
    # only ship the heuristic fallback.
    slm_e5_fallback_enabled: bool = True
    # Soft latency limit on the SLM forward pass.  Exceeding it
    # emits a warning but does not block ingest.
    slm_latency_budget_ms: float = 200.0
    # Phase I.1b — single-tenant adapter selection.  Production
    # constructors (CLI / MCP / dashboard / NemoClaw hub) read this
    # to load ONE adapter at startup via
    # :func:`ncms.application.intent_slot_chain.
    # build_default_intent_slot_chain`.  ``None`` means "don't load
    # any adapter" — the SLM stays dark even when ``slm_enabled=True``,
    # and ingestion falls through to the heuristic chain.  Set this
    # to a domain name (``conversational`` / ``software_dev`` /
    # ``clinical`` today, arbitrary custom names supported) at
    # deployment time via ``NCMS_DEFAULT_ADAPTER_DOMAIN`` to make
    # the SLM the primary classifier in production.
    default_adapter_domain: str | None = None

    # Level-first retrieval & synthesis (Phase 5)
    level_first_enabled: bool = False
    level_first_overfetch_factor: int = 3   # Over-fetch multiplier before node-type filter
    synthesis_enabled: bool = False
    synthesis_model: str = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    synthesis_api_base: str = "http://spark-ee7d.local:8000/v1"
    synthesis_token_budget: int = 4000      # Max tokens in synthesized output
    topic_map_enabled: bool = False
    topic_map_min_abstracts: int = 3        # Min abstracts to form a topic cluster
    topic_map_entity_overlap: float = 0.3   # Jaccard threshold for clustering

    # Phase 6: Export & Feedback
    search_feedback_enabled: bool = False    # Track search→access correlation
    bus_heartbeat_interval_seconds: int = 30  # Heartbeat ping interval
    bus_heartbeat_timeout_seconds: int = 90   # Mark offline after this silence
    auto_snapshot_on_disconnect: bool = False  # Publish snapshot when heartbeat fails
    scale_aware_flags_enabled: bool = False   # Auto-disable expensive features by corpus size
    scale_reranker_max_memories: int = 10000  # Disable reranker above this corpus size
    scale_intent_max_memories: int = 50000    # Disable intent classification above this

    # Per-intent signal weights (Phase 9 — RouteRAG-style).
    # Gated by ``temporal_enabled``; tunables remain independent.
    intent_weights_fact_lookup: str = "0.6,0.3,0.3,0.0"
    intent_weights_current_state_lookup: str = "0.4,0.2,0.5,0.1"
    intent_weights_historical_lookup: str = "0.5,0.3,0.3,0.1"
    intent_weights_event_reconstruction: str = "0.5,0.3,0.4,0.0"
    intent_weights_change_detection: str = "0.4,0.2,0.5,0.1"
    intent_weights_pattern_lookup: str = "0.3,0.5,0.3,0.0"
    intent_weights_strategic_reflection: str = "0.3,0.5,0.3,0.0"

    # Dream query expansion (Phase 9 — REM phase)
    dream_query_expansion_enabled: bool = False
    dream_expansion_max_terms: int = 20   # Tuned up from 5: more terms = more BM25 recall
    dream_expansion_min_pmi: float = 0.1

    # Active forgetting (Phase 9 — SleepGate-inspired)
    dream_active_forgetting_enabled: bool = False
    dream_forgetting_decay_rate: float = 0.05  # Tuned down from 0.2: 0.2 destroyed CR (-11.8%)
    dream_forgetting_access_prune_days: int = 90  # Tuned up from 30: preserve more access history
    dream_forgetting_conflict_age_days: int = 14  # Tuned up from 7: less aggressive on conflicts

    # Dream cycles (Phase 8)
    dream_cycle_enabled: bool = False
    dream_rehearsal_fraction: float = 0.10       # Top fraction of memories to rehearse
    dream_staleness_days: int = 7                # Memory considered stale after N days
    dream_min_access_count: int = 3              # Minimum accesses before eligible
    dream_rehearsal_weight_centrality: float = 0.40
    dream_rehearsal_weight_staleness: float = 0.30
    dream_rehearsal_weight_importance: float = 0.20
    dream_rehearsal_weight_access_count: float = 0.05
    dream_rehearsal_weight_recency: float = 0.05
    dream_importance_drift_window_days: int = 14  # Window for access rate comparison
    dream_importance_drift_rate: float = 0.1      # Max importance adjustment per cycle

    # Cross-encoder reranking (Phase 10)
    reranker_enabled: bool = False
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 50       # Rerank this many RRF candidates
    reranker_output_k: int = 20    # Keep this many after reranking
    scoring_weight_ce: float = 0.7  # Cross-encoder weight when reranker active

    # Background indexing (always on)
    index_workers: int = 3
    index_queue_size: int = 1000
    index_max_retries: int = 3
    index_drain_timeout_seconds: int = 30

    # Bulk import mode — defers all indexing until flush_indexing() called
    bulk_import_queue_size: int = 10000  # Larger queue for bulk loads

    # Pipeline observability
    pipeline_debug: bool = False  # Emit candidate details in pipeline events

    # MCP
    mcp_transport: str = "stdio"

    # HTTP API
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    auth_token: str | None = None

    # Maintenance scheduler
    maintenance_enabled: bool = False
    maintenance_consolidation_interval_minutes: int = 360   # 6 hours
    maintenance_dream_interval_minutes: int = 1440          # 24 hours
    maintenance_episode_close_interval_minutes: int = 60    # 1 hour
    maintenance_decay_interval_minutes: int = 720           # 12 hours
    # TLG L2 marker induction — runs under the master ``temporal_enabled``
    # flag.  Default 6 hours; induction is cheap (bounded by
    # |transition edges|) so higher-frequency re-runs are safe.
    maintenance_tlg_induction_interval_minutes: int = 360   # 6 hours

