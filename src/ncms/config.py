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

    # LLM-as-Judge
    llm_model: str = "gpt-4o-mini"
    llm_judge_enabled: bool = False

    # SPLADE
    splade_enabled: bool = False
    splade_model: str = "prithivida/Splade_PP_en_v1"

    # Snapshot
    snapshot_max_entries: int = 50
    snapshot_ttl_hours: int = 168

    # Retrieval pipeline
    tier1_candidates: int = 50
    tier2_candidates: int = 20
    tier3_judge_top_k: int = 10

    # Consolidation
    consolidation_importance_threshold: float = 50.0
    consolidation_enabled: bool = True

    # MCP
    mcp_transport: str = "stdio"
