"""NCMS configuration via Pydantic Settings.

Configuration can be set via environment variables with the NCMS_ prefix,
or by passing values directly to NCMSConfig().
"""

from __future__ import annotations

from pathlib import Path

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
    bus_surrogate_enabled: bool = True

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

    # Graph expansion (Tier 1.5)
    graph_expansion_enabled: bool = True
    graph_expansion_depth: int = 1
    graph_expansion_max: int = 10

    # Co-occurrence edges (entity graph connectivity)
    cooccurrence_edges_enabled: bool = True
    cooccurrence_max_entities: int = 12  # Fix #6: reduced from 20 to cap clique inflation

    # Graph-based spreading activation parameters
    graph_hop_decay: float = 0.5       # Activation multiplier per hop
    graph_spreading_max_hops: int = 2  # Maximum hops for graph traversal
    graph_ppr_enabled: bool = True     # Use Personalized PageRank (replaces BFS)

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

    # State reconciliation (Phase 2)
    reconciliation_enabled: bool = False
    reconciliation_importance_boost: float = 0.5
    reconciliation_supersession_penalty: float = 0.3
    reconciliation_conflict_penalty: float = 0.15

    # Episode formation (Phase 3)
    episodes_enabled: bool = False
    episode_window_minutes: int = 1440   # T_window for temporal proximity signal
    episode_close_minutes: int = 1440    # T_close for auto-closure
    episode_min_supporting_signals: int = 2  # Legacy: kept for backward compat
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

    # Intent-aware retrieval (Phase 4)
    intent_classification_enabled: bool = False
    intent_confidence_threshold: float = 0.6   # Below → fall back to fact_lookup
    intent_hierarchy_bonus: float = 0.5        # Raw bonus before weight
    scoring_weight_hierarchy: float = 0.0      # Additive weight (0 = no effect)
    intent_supplement_max: int = 20            # Max supplementary candidates per intent
    intent_llm_fallback_enabled: bool = False  # LLM fallback when BM25 confidence low

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

    # Temporal query scoring (Phase 4 temporal)
    temporal_enabled: bool = False
    scoring_weight_temporal: float = 0.2  # Additive weight when temporal ref detected

    # Per-intent signal weights (Phase 9 — RouteRAG-style)
    intent_routing_enabled: bool = False
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

    # Background indexing (Phase 2 performance)
    async_indexing_enabled: bool = True
    index_workers: int = 3
    index_queue_size: int = 1000
    index_max_retries: int = 3
    index_drain_timeout_seconds: int = 30

    # Pipeline observability
    pipeline_debug: bool = False  # Emit candidate details in pipeline events

    # MCP
    mcp_transport: str = "stdio"

    # HTTP API
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    auth_token: str | None = None

    # OpenTelemetry
    otel_enabled: bool = False
    otel_service_name: str = "ncms"
    otel_endpoint: str | None = None  # Uses OTEL_EXPORTER_OTLP_ENDPOINT env var if not set
    otel_protocol: str = "http/protobuf"  # or "grpc"

    # Maintenance scheduler
    maintenance_enabled: bool = False
    maintenance_consolidation_interval_minutes: int = 360   # 6 hours
    maintenance_dream_interval_minutes: int = 1440          # 24 hours
    maintenance_episode_close_interval_minutes: int = 60    # 1 hour
    maintenance_decay_interval_minutes: int = 720           # 12 hours

    # Filesystem watcher
    watch_enabled: bool = False
    watch_debounce_seconds: float = 2.0
    watch_exclude_patterns: str = "*.pyc,__pycache__,.git,.DS_Store,*.swp,*.swo,*~,*.tmp,.#*"
    watch_default_importance: float = 6.0
    watch_hash_persistence: bool = True
