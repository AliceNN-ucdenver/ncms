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

    # Pipeline observability
    pipeline_debug: bool = False  # Emit candidate details in pipeline events

    # MCP
    mcp_transport: str = "stdio"
