"""Hub replay harness — ingest hub fixture data, run queries, collect metrics.

Replays the exact 67-memory ingest sequence from the live hub to provide
deterministic before/after comparison for resilience improvements.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import time

logger = logging.getLogger(__name__)

# Junk entity patterns from resilience doc Section 3.3 / Appendix C
_JUNK_ENTITY_PATTERNS = [
    re.compile(r"^\d+(\.\d+)?%?$"),        # Pure numeric: "85%", "25789"
    re.compile(r"^\d+ \w+\(s\)$"),          # Count patterns: "1 item(s)"
    re.compile(r"^\d+ chars$"),             # Size patterns: "2783 chars"
    re.compile(r"^[a-f0-9]{8,}$"),          # Hex IDs: "6f01603fe96a"
    re.compile(r"^Document: "),             # Prefixed IDs
    re.compile(r"^[A-Z]\d+$"),              # Citation labels: "S5", "S6"
    re.compile(r"^avg \d"),                 # Aggregate labels: "avg 85%"
]


def _is_junk_entity(name: str) -> bool:
    """Return True if the entity name matches a known noise pattern."""
    name = name.strip()
    if len(name) <= 1:
        return True
    if not name.replace(" ", "").replace("-", ""):
        return True  # Pure punctuation/whitespace
    return any(p.match(name) for p in _JUNK_ENTITY_PATTERNS)


class ReplayState:
    """Holds backends and timing data from an ingest replay."""

    def __init__(
        self,
        store: object,
        index: object,
        graph: object,
        splade: object,
        config: object,
        svc: object,
        ingest_timings_ms: list[float],
        memory_ids: list[str],
    ):
        self.store = store
        self.index = index
        self.graph = graph
        self.splade = splade
        self.config = config
        self.svc = svc
        self.ingest_timings_ms = ingest_timings_ms
        self.memory_ids = memory_ids


async def replay_ingest(
    memories: list[dict],
    config: object | None = None,
) -> ReplayState:
    """Ingest hub memories in created_at order into in-memory backends.

    Args:
        memories: List of memory dicts (from fixtures.HUB_MEMORIES).
        config: Optional NCMSConfig override.

    Returns:
        ReplayState with populated backends and per-memory timing.
    """
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

    splade = SpladeEngine()

    if config is None:
        config = NCMSConfig(
            db_path=":memory:",
            actr_noise=0.0,  # Deterministic scoring
            splade_enabled=True,
            graph_expansion_enabled=True,
            scoring_weight_bm25=0.6,
            scoring_weight_actr=0.0,
            scoring_weight_splade=0.3,
            scoring_weight_graph=0.3,
            contradiction_detection_enabled=False,
        )

    # Seed domain-specific topics for GLiNER entity extraction
    from benchmarks.core.datasets import HUB_REPLAY_TOPICS

    topic_info = HUB_REPLAY_TOPICS.get("hub_replay", {})
    hub_domain = topic_info.get("domain", "architecture")
    hub_labels = topic_info.get("labels", [])
    if hub_labels:
        await store.set_consolidation_value(
            f"entity_labels:{hub_domain}",
            json.dumps(hub_labels),
        )
        logger.info("Seeded hub replay entity labels: %s", hub_labels)

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )

    # Sort by created_at to replay in exact order
    sorted_memories = sorted(memories, key=lambda m: m["created_at"])

    ingest_timings_ms: list[float] = []
    memory_ids: list[str] = []

    for i, mem in enumerate(sorted_memories):
        content = mem["content"]
        memory_type = mem.get("type", "fact")
        source_agent = mem.get("source_agent")
        importance = float(mem.get("importance", 5.0))

        # Parse domains from JSON string or list
        domains_raw = mem.get("domains", "[]")
        if isinstance(domains_raw, str):
            try:
                domains = json.loads(domains_raw)
            except (json.JSONDecodeError, TypeError):
                domains = []
        else:
            domains = domains_raw or []

        # Parse tags similarly
        tags_raw = mem.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = tags_raw or []

        t0 = time.perf_counter()
        memory = await svc.store_memory(
            content=content,
            memory_type=memory_type,
            source_agent=source_agent,
            importance=importance,
            domains=domains if domains else None,
            tags=tags if tags else None,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        ingest_timings_ms.append(elapsed_ms)
        memory_ids.append(memory.id)

        if (i + 1) % 10 == 0 or i == len(sorted_memories) - 1:
            logger.info(
                "  Ingested %d/%d memories (last: %.1f ms)",
                i + 1, len(sorted_memories), elapsed_ms,
            )

    logger.info(
        "Ingest complete: %d memories, p50=%.1f ms, p95=%.1f ms",
        len(memory_ids),
        statistics.median(ingest_timings_ms),
        _percentile(ingest_timings_ms, 95),
    )

    return ReplayState(
        store=store,
        index=index,
        graph=graph,
        splade=splade,
        config=config,
        svc=svc,
        ingest_timings_ms=ingest_timings_ms,
        memory_ids=memory_ids,
    )


async def run_queries(
    state: ReplayState,
    queries: dict[str, str],
) -> dict[str, list[dict]]:
    """Run each query via search(), return ranked results with scores.

    Args:
        state: ReplayState from replay_ingest().
        queries: {query_name: query_text}

    Returns:
        {query_name: [{memory_id, score, content_preview}, ...]}
    """
    results: dict[str, list[dict]] = {}
    search_timings_ms: list[float] = []

    for name, query_text in queries.items():
        t0 = time.perf_counter()
        scored = await state.svc.search(query=query_text, limit=10)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        search_timings_ms.append(elapsed_ms)

        results[name] = [
            {
                "memory_id": s.memory.id,
                "score": round(s.total_activation, 4),
                "content_preview": s.memory.content[:200],
            }
            for s in scored
        ]

        logger.info(
            "  Query '%s': %d results in %.1f ms (top score: %.4f)",
            name,
            len(scored),
            elapsed_ms,
            scored[0].total_activation if scored else 0.0,
        )

    return results


async def evaluate_replay(
    memories: list[dict],
    queries: dict[str, str],
) -> dict:
    """Full orchestration: ingest -> query -> compute metrics.

    Args:
        memories: Hub memory fixtures.
        queries: Test queries.

    Returns:
        Dict with latency metrics, data integrity counts, and per-query results.
    """
    logger.info("=" * 60)
    logger.info("Hub Replay Benchmark")
    logger.info("  Memories: %d, Queries: %d", len(memories), len(queries))
    logger.info("=" * 60)

    # --- Ingest ---
    logger.info("Phase 1: Ingesting memories...")
    state = await replay_ingest(memories)

    # --- Data integrity metrics ---
    logger.info("Phase 2: Computing data integrity metrics...")

    # Duplicate detection by content hash
    content_hashes: dict[str, list[str]] = {}
    for mem in memories:
        h = hashlib.sha256(mem["content"].encode()).hexdigest()[:16]
        content_hashes.setdefault(h, []).append(mem["id"])
    duplicate_count = sum(len(ids) - 1 for ids in content_hashes.values() if len(ids) > 1)

    # Entity metrics from the graph
    all_entities = list(state.graph._graph.nodes()) if hasattr(state.graph, "_graph") else []
    total_entities = len(all_entities)
    junk_entities = [e for e in all_entities if _is_junk_entity(str(e))]
    junk_entity_count = len(junk_entities)

    logger.info(
        "  Data: %d total memories, %d duplicates, %d entities (%d junk)",
        len(memories), duplicate_count, total_entities, junk_entity_count,
    )

    # --- Queries ---
    logger.info("Phase 3: Running test queries...")
    query_results = await run_queries(state, queries)

    # Collect search timings by re-running (already timed above)
    search_timings_ms: list[float] = []
    for _name, query_text in queries.items():
        t0 = time.perf_counter()
        await state.svc.search(query=query_text, limit=10)
        search_timings_ms.append((time.perf_counter() - t0) * 1000)

    # --- Cleanup ---
    await state.store.close()

    # --- Assemble results ---
    result = {
        "total_memories": len(memories),
        "ingested_count": len(state.memory_ids),
        "duplicate_count": duplicate_count,
        "total_entities": total_entities,
        "junk_entity_count": junk_entity_count,
        "junk_entity_rate": round(junk_entity_count / max(total_entities, 1) * 100, 1),
        "junk_entity_samples": [str(e) for e in junk_entities[:20]],
        "ingest_latency_p50": round(statistics.median(state.ingest_timings_ms), 2),
        "ingest_latency_p95": round(_percentile(state.ingest_timings_ms, 95), 2),
        "ingest_latency_p99": round(_percentile(state.ingest_timings_ms, 99), 2),
        "search_latency_p50": round(statistics.median(search_timings_ms), 2),
        "queries": {
            name: {
                "query": queries[name],
                "result_count": len(results),
                "top_3": results[:3],
            }
            for name, results in query_results.items()
        },
    }

    logger.info("=" * 60)
    logger.info("Hub Replay Summary:")
    logger.info("  Ingest: p50=%.1f ms, p95=%.1f ms, p99=%.1f ms",
                result["ingest_latency_p50"],
                result["ingest_latency_p95"],
                result["ingest_latency_p99"])
    logger.info("  Search: p50=%.1f ms", result["search_latency_p50"])
    logger.info("  Data: %d memories, %d duplicates, %d entities (%d junk / %.1f%%)",
                result["total_memories"],
                result["duplicate_count"],
                result["total_entities"],
                result["junk_entity_count"],
                result["junk_entity_rate"])
    logger.info("=" * 60)

    return result


def _percentile(data: list[float], pct: int) -> float:
    """Compute the pct-th percentile of a sorted list."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (pct / 100) * (len(sorted_data) - 1)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
