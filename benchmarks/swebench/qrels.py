"""Ground truth construction for SWE-bench memory competency evaluation.

Generates qrels (relevance judgments) for 4 memory competency splits:
- AR (Accurate Retrieval): file overlap between issues
- TTL (Test-Time Learning): subsystem classification accuracy
- LRU (Long-Range Understanding): entity coverage in holistic queries
- CR (Conflict Resolution): temporal state ordering accuracy

All qrels are auto-generated from the SWE-bench patch metadata —
no manual annotation required.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from benchmarks.swebench.loader import SWEInstance

logger = logging.getLogger(__name__)


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── AR: Accurate Retrieval ───────────────────────────────────────────────


def build_ar_qrels(
    train: list[SWEInstance],
    test: list[SWEInstance],
    min_jaccard: float = 0.0,
) -> dict[str, dict[str, int]]:
    """Build AR qrels from patch file overlap.

    For each test issue (query), a train issue (corpus doc) is relevant if
    their patches modify overlapping files.

    Relevance grades:
        2: Jaccard(files_touched) >= 0.5 (highly relevant — same code area)
        1: 0 < Jaccard < 0.5 (relevant — related code area)

    Args:
        train: Corpus instances.
        test: Query instances.
        min_jaccard: Minimum Jaccard to count as relevant (default 0 = any overlap).

    Returns:
        BEIR-style qrels: {query_id: {doc_id: relevance_grade}}
    """
    qrels: dict[str, dict[str, int]] = {}

    for q in test:
        q_files = set(q.files_touched)
        if not q_files:
            continue

        rels: dict[str, int] = {}
        for c in train:
            c_files = set(c.files_touched)
            jac = _jaccard(q_files, c_files)
            if jac > min_jaccard:
                grade = 2 if jac >= 0.5 else 1
                rels[c.instance_id] = grade

        if rels:
            qrels[q.instance_id] = rels

    logger.info(
        "AR qrels: %d queries with judgments, avg %.1f relevant docs/query",
        len(qrels),
        sum(len(v) for v in qrels.values()) / max(len(qrels), 1),
    )
    return qrels


# ── TTL: Test-Time Learning (subsystem classification) ───────────────────


def build_ttl_labels(
    instances: list[SWEInstance],
) -> dict[str, str]:
    """Build TTL ground truth: instance_id → subsystem label.

    Returns:
        {instance_id: subsystem_label}
    """
    labels = {inst.instance_id: inst.subsystem for inst in instances}
    # Distribution
    dist: dict[str, int] = defaultdict(int)
    for s in labels.values():
        dist[s] += 1
    logger.info("TTL subsystem distribution: %s", dict(sorted(dist.items(), key=lambda x: -x[1])))
    return labels


# ── CR: Conflict Resolution (temporal state tracking) ────────────────────


def build_cr_qrels(
    instances: list[SWEInstance],
    min_issues_per_file: int = 3,
) -> tuple[dict[str, dict[str, int]], dict[str, str]]:
    """Build CR qrels from temporal file evolution.

    Identifies files modified by 3+ issues across time. For each such file,
    the ground truth is that the most recent issue should rank highest.

    Args:
        instances: All instances (sorted by created_at).
        min_issues_per_file: Minimum issues touching a file to create a CR query.

    Returns:
        Tuple of:
        - qrels: {file_query_id: {instance_id: grade}} where most recent = grade 2
        - queries: {file_query_id: query_text} for each tracked file
    """
    # Group instances by file
    file_to_instances: dict[str, list[SWEInstance]] = defaultdict(list)
    for inst in instances:
        for f in inst.files_touched:
            file_to_instances[f].append(inst)

    qrels: dict[str, dict[str, int]] = {}
    queries: dict[str, str] = {}

    for filepath, insts in file_to_instances.items():
        if len(insts) < min_issues_per_file:
            continue

        # Sort by created_at (should already be, but be safe)
        insts_sorted = sorted(insts, key=lambda x: x.created_at)

        # Query ID based on file path
        qid = f"cr_{filepath.replace('/', '_')}"

        # Most recent issue is grade 2 (current state), others grade 1
        rels: dict[str, int] = {}
        for i, inst in enumerate(insts_sorted):
            if i == len(insts_sorted) - 1:
                rels[inst.instance_id] = 2  # Most recent = current state
            else:
                rels[inst.instance_id] = 1  # Older = superseded

        qrels[qid] = rels

        # Generate natural language query
        short_path = filepath.split("/")[-1] if "/" in filepath else filepath
        queries[qid] = f"What is the current state of {short_path} in {filepath}?"

    logger.info(
        "CR qrels: %d file-state queries (from files with %d+ issues)",
        len(qrels),
        min_issues_per_file,
    )
    return qrels, queries


# ── LRU: Long-Range Understanding (holistic queries) ────────────────────


def build_lru_queries(
    instances: list[SWEInstance],
    min_subsystem_count: int = 5,
) -> tuple[dict[str, str], dict[str, dict[str, int]]]:
    """Build LRU queries and qrels for holistic understanding evaluation.

    Generates template-based questions about subsystem evolution.
    Ground truth: any issue in the queried subsystem is relevant.

    Args:
        instances: All instances.
        min_subsystem_count: Minimum issues in a subsystem to generate queries.

    Returns:
        Tuple of:
        - queries: {query_id: query_text}
        - qrels: {query_id: {instance_id: 1}}
    """
    # Group by subsystem
    subsystem_instances: dict[str, list[SWEInstance]] = defaultdict(list)
    for inst in instances:
        subsystem_instances[inst.subsystem].append(inst)

    queries: dict[str, str] = {}
    qrels: dict[str, dict[str, int]] = {}

    templates = [
        "What are the recurring problems in Django's {subsystem} module?",
        "How has Django's {subsystem} component evolved over time?",
        "What patterns emerge across Django {subsystem} issues?",
    ]

    for subsystem, insts in subsystem_instances.items():
        if len(insts) < min_subsystem_count:
            continue
        if subsystem in ("other", "tests"):
            continue

        for i, template in enumerate(templates):
            qid = f"lru_{subsystem}_{i}"
            queries[qid] = template.format(subsystem=subsystem)
            # All issues in this subsystem are relevant
            qrels[qid] = {inst.instance_id: 1 for inst in insts}

    logger.info(
        "LRU queries: %d holistic questions across %d subsystems",
        len(queries),
        len({q.split("_")[1] for q in queries}),
    )
    return queries, qrels
