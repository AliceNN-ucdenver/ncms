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
    splade_model: str = "prithivida/Splade_PP_en_v1"
    splade_top_k: int = 50
    scoring_weight_splade: float = 0.0

    # Contradiction detection (uses llm_model + llm_api_base for the LLM)
    contradiction_detection_enabled: bool = False
    contradiction_candidate_limit: int = 5

    # Snapshot
    snapshot_max_entries: int = 50
    snapshot_ttl_hours: int = 168

    # Retrieval pipeline
    tier1_candidates: int = 50
    tier2_candidates: int = 20
    scoring_weight_bm25: float = 0.6
    scoring_weight_actr: float = 0.4
    scoring_weight_graph: float = 0.0  # Graph-expansion entity overlap (spreading activation)

    # Graph expansion (Tier 1.5)
    graph_expansion_enabled: bool = True
    graph_expansion_depth: int = 1
    graph_expansion_max: int = 10

    # Model cache directory (for GLiNER / SPLADE / fastembed downloads)
    # Defaults to ~/.cache/huggingface/hub if not set
    model_cache_dir: str | None = None

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
    admission_novelty_search_limit: int = 3
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

    # Pipeline observability
    pipeline_debug: bool = False  # Emit candidate details in pipeline events

    # MCP
    mcp_transport: str = "stdio"
