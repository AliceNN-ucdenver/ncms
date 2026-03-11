"""Ablation configuration matrix.

Defines the pipeline configurations to evaluate. Each config controls
which retrieval stages are active at search time:

Core pipeline (no LLM required):
- BM25 (always on — Tantivy lexical retrieval)
- SPLADE (sparse neural retrieval + RRF fusion)
- Graph Expansion (entity-based cross-memory discovery)
- ACT-R Scoring (cognitive recency/frequency/spreading activation)

LLM-powered opt-in features (require Ollama or API):
- Keyword Bridges (LLM-extracted semantic bridge nodes for graph connectivity)
- LLM Judge (Tier 3 LLM reranking for relevance scoring)

Core configs always run. LLM configs run when --llm-model is provided.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AblationConfig:
    """Configuration for a single ablation variant."""

    name: str
    display_name: str  # Human-readable name for tables/charts
    use_splade: bool
    graph_expansion_enabled: bool
    scoring_weight_bm25: float
    scoring_weight_actr: float
    scoring_weight_splade: float
    actr_threshold: float  # -999.0 disables retrieval probability filter
    # LLM-powered features (opt-in)
    keyword_bridge_enabled: bool = False
    llm_judge_enabled: bool = False
    requires_llm: bool = False  # True if config needs an LLM backend


# Additive ablation: build up from BM25 baseline
CORE_CONFIGS: list[AblationConfig] = [
    AblationConfig(
        name="bm25_only",
        display_name="BM25 Only",
        use_splade=False,
        graph_expansion_enabled=False,
        scoring_weight_bm25=1.0,
        scoring_weight_actr=0.0,
        scoring_weight_splade=0.0,
        actr_threshold=-999.0,
    ),
    AblationConfig(
        name="bm25_graph",
        display_name="+ Graph",
        use_splade=False,
        graph_expansion_enabled=True,
        scoring_weight_bm25=1.0,
        scoring_weight_actr=0.0,
        scoring_weight_splade=0.0,
        actr_threshold=-999.0,
    ),
    AblationConfig(
        name="bm25_actr",
        display_name="+ ACT-R",
        use_splade=False,
        graph_expansion_enabled=False,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.4,
        scoring_weight_splade=0.0,
        actr_threshold=-2.0,
    ),
    AblationConfig(
        name="bm25_splade",
        display_name="+ SPLADE",
        use_splade=True,
        graph_expansion_enabled=False,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_splade=0.3,
        actr_threshold=-999.0,
    ),
    AblationConfig(
        name="bm25_splade_graph",
        display_name="+ SPLADE + Graph",
        use_splade=True,
        graph_expansion_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_splade=0.3,
        actr_threshold=-999.0,
    ),
    AblationConfig(
        name="full",
        display_name="Full Pipeline",
        use_splade=True,
        graph_expansion_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.4,
        scoring_weight_splade=0.3,
        actr_threshold=-2.0,
    ),
]

# LLM-powered configs: require Ollama or API endpoint
LLM_CONFIGS: list[AblationConfig] = [
    AblationConfig(
        name="full_keywords",
        display_name="+ Keyword Bridges",
        use_splade=True,
        graph_expansion_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.4,
        scoring_weight_splade=0.3,
        actr_threshold=-2.0,
        keyword_bridge_enabled=True,
        requires_llm=True,
    ),
    AblationConfig(
        name="full_keywords_judge",
        display_name="+ Keywords + Judge",
        use_splade=True,
        graph_expansion_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.4,
        scoring_weight_splade=0.3,
        actr_threshold=-2.0,
        keyword_bridge_enabled=True,
        llm_judge_enabled=True,
        requires_llm=True,
    ),
]

# All configs (for reference)
ABLATION_CONFIGS: list[AblationConfig] = CORE_CONFIGS + LLM_CONFIGS
