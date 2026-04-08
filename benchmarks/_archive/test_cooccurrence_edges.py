"""Mini proof-of-concept: co-occurrence edges enable graph expansion.

Tests the hypothesis that adding edges between entities co-occurring in
the same document gives the graph connectivity for:
  1. Graph expansion (discovering candidate memories not found by BM25)
  2. PageRank (meaningful centrality for dream rehearsal)
  3. Spreading activation with association strengths

Run: uv run python benchmarks/test_cooccurrence_edges.py
"""

import logging
from itertools import combinations

from ncms.domain.models import Entity, Relationship
from ncms.infrastructure.graph.networkx_store import NetworkXGraph

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# === Synthetic documents with known entity overlap ===
# Mimics SWE-bench Django issues sharing code entities
DOCUMENTS = {
    "issue-001": {
        "content": "QuerySet.filter() raises FieldError when using select_related",
        "entities": ["QuerySet", "filter", "FieldError", "select_related"],
    },
    "issue-002": {
        "content": "select_related crashes with defer on multi-table inheritance",
        "entities": ["select_related", "defer", "Model", "multi_table_inheritance"],
    },
    "issue-003": {
        "content": "QuerySet.annotate() combined with filter produces wrong SQL",
        "entities": ["QuerySet", "annotate", "filter", "SQL"],
    },
    "issue-004": {
        "content": "Model.save() skips validation when update_fields is set",
        "entities": ["Model", "save", "validation", "update_fields"],
    },
    "issue-005": {
        "content": "Middleware ordering affects CSRF validation in admin views",
        "entities": ["Middleware", "CSRF", "validation", "admin"],
    },
    "issue-006": {
        "content": "admin site crashes when Model has custom Manager with annotate",
        "entities": ["admin", "Model", "Manager", "annotate"],
    },
}


def build_entities(graph: NetworkXGraph) -> dict[str, str]:
    """Create entities, deduplicating by name. Returns name→id mapping."""
    name_to_id: dict[str, str] = {}
    for doc in DOCUMENTS.values():
        for name in doc["entities"]:
            if name.lower() not in name_to_id:
                entity = Entity(name=name, type="code_element")
                graph.add_entity(entity)
                name_to_id[name.lower()] = entity.id
    return name_to_id


def link_memories(graph: NetworkXGraph, name_to_id: dict[str, str]) -> None:
    """Link each document to its entities."""
    for doc_id, doc in DOCUMENTS.items():
        for name in doc["entities"]:
            eid = name_to_id[name.lower()]
            graph.link_memory_entity(doc_id, eid)


def add_cooccurrence_edges(
    graph: NetworkXGraph,
    name_to_id: dict[str, str],
) -> int:
    """Add undirected co-occurrence edges between entities in same document."""
    edge_count = 0
    seen_pairs: set[tuple[str, str]] = set()

    for doc_id, doc in DOCUMENTS.items():
        entity_ids = [name_to_id[n.lower()] for n in doc["entities"]]
        for a, b in combinations(entity_ids, 2):
            pair = (min(a, b), max(a, b))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                # Bidirectional edges
                rel_fwd = Relationship(
                    source_entity_id=a,
                    target_entity_id=b,
                    type="co_occurs",
                    source_memory_id=doc_id,
                )
                rel_rev = Relationship(
                    source_entity_id=b,
                    target_entity_id=a,
                    type="co_occurs",
                    source_memory_id=doc_id,
                )
                graph.add_relationship(rel_fwd)
                graph.add_relationship(rel_rev)
                edge_count += 1
    return edge_count


def test_graph_diagnostics(graph: NetworkXGraph, label: str) -> dict:
    """Print graph diagnostics."""
    import networkx as nx

    g = graph._graph
    n_nodes = g.number_of_nodes()
    n_edges = g.number_of_edges()

    # Connected components (treat as undirected)
    undirected = g.to_undirected()
    components = list(nx.connected_components(undirected))
    n_components = len(components)
    largest = max(len(c) for c in components) if components else 0

    # PageRank
    pr = nx.pagerank(g) if n_edges > 0 else {n: 1.0 / n_nodes for n in g.nodes()}
    pr_max = max(pr.values()) if pr else 0
    pr_top3 = sorted(pr.items(), key=lambda x: -x[1])[:3]

    # Degree stats
    degrees = [g.degree(n) for n in g.nodes()]
    mean_degree = sum(degrees) / len(degrees) if degrees else 0

    log.info(f"\n{'='*60}")
    log.info(f"Graph Diagnostics: {label}")
    log.info(f"{'='*60}")
    log.info(f"  Nodes:          {n_nodes}")
    log.info(f"  Edges:          {n_edges}")
    log.info(f"  Components:     {n_components}")
    log.info(f"  Largest comp:   {largest}")
    log.info(f"  Mean degree:    {mean_degree:.2f}")
    log.info(f"  PR max:         {pr_max:.6f}")
    if n_edges > 0:
        for eid, score in pr_top3:
            name = g.nodes[eid].get("name", "?")
            log.info(f"    PR top: {name:20s} = {score:.6f}")

    return {
        "nodes": n_nodes,
        "edges": n_edges,
        "components": n_components,
        "largest": largest,
        "mean_degree": mean_degree,
        "pr_max": pr_max,
    }


def test_graph_expansion(
    graph: NetworkXGraph,
    name_to_id: dict[str, str],
    label: str,
) -> None:
    """Test graph expansion from a query about QuerySet."""
    log.info(f"\nGraph Expansion Test: {label}")
    log.info("-" * 40)

    # Simulate query: "QuerySet filter problem"
    query_entities = ["queryset", "filter"]
    query_eids = [name_to_id[n] for n in query_entities if n in name_to_id]

    log.info(f"  Query entities: {query_entities}")
    log.info(f"  Query entity IDs found: {len(query_eids)}")

    # Direct: which memories directly mention these entities?
    direct_memories: set[str] = set()
    for eid in query_eids:
        mids = graph.get_memory_ids_for_entity(eid)
        direct_memories.update(mids)
    log.info(f"  Direct memories (entity overlap): {sorted(direct_memories)}")

    # Graph expansion depth=1: traverse edges to find related entities → memories
    expanded = graph.get_related_memory_ids(query_eids, depth=1)
    novel = expanded - direct_memories
    log.info(f"  Expanded memories (depth=1):      {sorted(expanded)}")
    log.info(f"  Novel discoveries:                {sorted(novel)}")

    # Graph expansion depth=2
    expanded_d2 = graph.get_related_memory_ids(query_eids, depth=2)
    novel_d2 = expanded_d2 - direct_memories
    log.info(f"  Expanded memories (depth=2):      {sorted(expanded_d2)}")
    log.info(f"  Novel discoveries (depth=2):      {sorted(novel_d2)}")


def test_spreading_activation(
    graph: NetworkXGraph,
    name_to_id: dict[str, str],
    label: str,
) -> None:
    """Test spreading activation scores for each document."""
    from ncms.domain.scoring import spreading_activation

    log.info(f"\nSpreading Activation Test: {label}")
    log.info("-" * 40)

    # Query: "QuerySet filter problem"
    query_entities = ["queryset", "filter"]
    context_eids = [name_to_id[n] for n in query_entities if n in name_to_id]

    for doc_id, doc in DOCUMENTS.items():
        mem_eids = [name_to_id[n.lower()] for n in doc["entities"]]
        score = spreading_activation(
            memory_entity_ids=mem_eids,
            context_entity_ids=context_eids,
        )
        overlap = set(mem_eids) & set(context_eids)
        overlap_names = [
            n for n, eid in name_to_id.items() if eid in overlap
        ]
        log.info(f"  {doc_id}: spread={score:.4f}  overlap={overlap_names}")


def main() -> None:
    log.info("=" * 60)
    log.info("Co-occurrence Edge Proof of Concept")
    log.info("=" * 60)
    log.info(f"\nDocuments: {len(DOCUMENTS)}")
    for doc_id, doc in DOCUMENTS.items():
        log.info(f"  {doc_id}: {doc['entities']}")

    # Expected overlap structure:
    log.info("\nExpected entity sharing:")
    log.info("  issue-001 ↔ issue-002: select_related")
    log.info("  issue-001 ↔ issue-003: QuerySet, filter")
    log.info("  issue-002 ↔ issue-004: Model")
    log.info("  issue-002 ↔ issue-006: Model")
    log.info("  issue-003 ↔ issue-006: annotate")
    log.info("  issue-004 ↔ issue-005: validation")
    log.info("  issue-004 ↔ issue-006: Model")
    log.info("  issue-005 ↔ issue-006: admin")

    # --- Phase 1: Without co-occurrence edges ---
    graph_before = NetworkXGraph()
    name_to_id = build_entities(graph_before)
    link_memories(graph_before, name_to_id)

    log.info(f"\nEntities created: {len(name_to_id)}")

    diag_before = test_graph_diagnostics(graph_before, "BEFORE co-occurrence edges")
    test_graph_expansion(graph_before, name_to_id, "BEFORE")
    test_spreading_activation(graph_before, name_to_id, "BEFORE (no edges)")

    # --- Phase 2: Add co-occurrence edges ---
    graph_after = NetworkXGraph()
    name_to_id2 = build_entities(graph_after)
    link_memories(graph_after, name_to_id2)
    n_edges = add_cooccurrence_edges(graph_after, name_to_id2)

    log.info(f"\nCo-occurrence edges added: {n_edges} unique pairs")

    diag_after = test_graph_diagnostics(graph_after, "AFTER co-occurrence edges")
    test_graph_expansion(graph_after, name_to_id2, "AFTER")
    test_spreading_activation(graph_after, name_to_id2, "AFTER (with edges)")

    # --- Comparison ---
    log.info(f"\n{'='*60}")
    log.info("COMPARISON")
    log.info(f"{'='*60}")
    log.info(f"  {'Metric':<20s} {'Before':>10s} {'After':>10s}")
    log.info(f"  {'-'*40}")
    for key in ["nodes", "edges", "components", "largest", "mean_degree", "pr_max"]:
        log.info(
            f"  {key:<20s} {diag_before[key]:>10.4f} {diag_after[key]:>10.4f}"
            if isinstance(diag_before[key], float)
            else f"  {key:<20s} {diag_before[key]:>10d} {diag_after[key]:>10d}"
        )


if __name__ == "__main__":
    main()
