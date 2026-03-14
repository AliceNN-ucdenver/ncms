# NCMS V1 Architecture (Original Design)

This document preserves the original NCMS architecture diagram from the initial release — a flat entity graph with BM25 + SPLADE + ACT-R scoring and no memory hierarchy. This design served as the foundation and ablation baseline for the current HTMG (Hierarchical Temporal Memory Graph) architecture.

<p align="center">
  <img src="assets/architecture.svg" alt="NCMS V1 Architecture" width="100%">
</p>

## What V1 Had

- **Flat memory store** — All memories stored as equal-weight records in SQLite with Tantivy BM25 indexing
- **Entity graph** — NetworkX directed graph with GLiNER-extracted entities and memory-entity links
- **Three-tier retrieval** — BM25 + SPLADE candidates → graph expansion → ACT-R cognitive rescoring
- **Knowledge Bus** — AsyncIO domain-routed inter-agent communication with surrogate responses
- **Ablation-validated** — 0.698 nDCG@10 on SciFact (BEIR), outperforming published dense retrieval baselines

## What V1 Lacked

- **No memory hierarchy** — Every memory was treated identically regardless of type (fact, state change, episode, insight)
- **No temporal episodes** — Co-occurring memories had no structural grouping
- **No entity state tracking** — Entity attributes were static snapshots with no evolution history
- **No admission scoring** — All incoming content was stored unconditionally
- **No learned associations** — Spreading activation used uniform weights (`association_strengths=None`)
- **No offline consolidation** — No dream cycles, no rehearsal, no importance drift

## What Changed

The [keyword bridge catastrophic failure](../README.md#negative-results-keyword-bridges) (nDCG@10: 0.690 → 0.032) revealed that the flat entity graph needed **structural** cross-subgraph connectivity rather than **lexical** keyword bridges. This motivated the HTMG architecture with typed memory nodes, temporal episodes, entity state reconciliation, and dream-cycle-based offline learning.

See the [current README](../README.md) for the full architecture.
