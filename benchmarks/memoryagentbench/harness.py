"""MemoryAgentBench evaluation harness.

Evaluates NCMS against the MemoryAgentBench benchmark across 4 memory
competencies: Accurate Retrieval (AR), Test-Time Learning (TTL),
Long-Range Understanding (LRU), and Conflict Resolution (CR).

Each sample has a long context, 100 questions, and answer lists.
The protocol for each sample:
  1. Chunk the context into ~2000-char pieces
  2. Ingest each chunk as a separate memory into a fresh NCMS instance
  3. For each question: search NCMS, score retrieved context vs answers
  4. Aggregate per-split and overall

This is a retrieval-quality baseline (no LLM answer generation).
"""

from __future__ import annotations

import json
import logging
import re
import string
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Retrieval weights (from SciFact-tuned config)
TUNED_WEIGHTS = {
    "bm25": 0.6,
    "splade": 0.3,
    "graph": 0.3,
    "actr": 0.0,
    "hierarchy": 0.0,
}

# Split names as used in the MAB benchmark
ALL_SPLITS = ("ar", "ttl", "lru", "cr")

# Chunk size for splitting context into memories (chars)
DEFAULT_CHUNK_SIZE = 2000
DEFAULT_CHUNK_OVERLAP = 200

# How many results to retrieve per question
DEFAULT_TOP_K = 10


# -- Scoring helpers --------------------------------------------------------


def _normalize(text: str) -> str:
    """Normalize text for scoring: lowercase, strip punctuation/articles/whitespace."""
    text = text.lower()
    # Remove punctuation
    text = text.translate(str.maketrans("", "", string.punctuation))
    # Remove articles
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    # Collapse whitespace
    text = " ".join(text.split())
    return text


def _tokenize(text: str) -> list[str]:
    """Tokenize normalized text."""
    return _normalize(text).split()


def token_f1(prediction: str, ground_truth: str) -> float:
    """Compute token-level F1 between prediction and ground truth."""
    pred_tokens = _tokenize(prediction)
    gold_tokens = _tokenize(ground_truth)
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_common = sum(common.values())
    if num_common == 0:
        return 0.0
    precision = num_common / len(pred_tokens)
    recall = num_common / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def exact_match(prediction: str, ground_truth: str) -> bool:
    """Check if normalized prediction equals normalized ground truth."""
    return _normalize(prediction) == _normalize(ground_truth)


def substring_match(prediction: str, ground_truth: str) -> bool:
    """Check if normalized ground truth appears in normalized prediction."""
    return _normalize(ground_truth) in _normalize(prediction)


def score_answer(retrieved_text: str, answer_list: list[str]) -> dict[str, float]:
    """Score retrieved text against a list of acceptable answers.

    Args:
        retrieved_text: Concatenated top-k retrieved memory contents.
        answer_list: List of acceptable answers (from multiple annotators).

    Returns:
        Dict with contains_any, best_f1, best_exact_match, best_substring.
    """
    if not answer_list or not retrieved_text:
        return {
            "contains_any": 0.0,
            "best_f1": 0.0,
            "best_exact_match": 0.0,
            "best_substring": 0.0,
        }

    best_f1 = 0.0
    best_em = 0.0
    best_sub = 0.0

    for answer in answer_list:
        ans = str(answer).strip()
        if not ans:
            continue
        f1 = token_f1(retrieved_text, ans)
        em = 1.0 if exact_match(retrieved_text, ans) else 0.0
        sub = 1.0 if substring_match(retrieved_text, ans) else 0.0

        best_f1 = max(best_f1, f1)
        best_em = max(best_em, em)
        best_sub = max(best_sub, sub)

    return {
        "contains_any": best_sub,  # Does retrieved text contain any answer?
        "best_f1": best_f1,
        "best_exact_match": best_em,
        "best_substring": best_sub,
    }


# -- Text chunking ---------------------------------------------------------


def chunk_context(context: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                  overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split context into overlapping chunks at sentence boundaries.

    Args:
        context: Full context string to chunk.
        chunk_size: Target chunk size in characters.
        overlap: Overlap between consecutive chunks in characters.

    Returns:
        List of text chunks.
    """
    if not context:
        return []

    # Split on sentence boundaries (period/question/exclamation + space)
    sentences = re.split(r'(?<=[.!?])\s+', context)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)
        if current_len + sentence_len > chunk_size and current:
            chunks.append(" ".join(current))
            # Keep overlap: walk backwards to find sentences within overlap window
            overlap_parts: list[str] = []
            overlap_len = 0
            for s in reversed(current):
                if overlap_len + len(s) > overlap:
                    break
                overlap_parts.insert(0, s)
                overlap_len += len(s) + 1
            current = overlap_parts
            current_len = overlap_len
        current.append(sentence)
        current_len += sentence_len + 1

    if current:
        chunks.append(" ".join(current))

    return chunks


# -- Per-sample NCMS instance creation --------------------------------------


async def _create_ncms_instance(
    domain: str = "mab",
) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Create a fresh in-memory NCMS instance for one sample.

    Returns:
        Tuple of (memory_service, store, index, graph, splade, config).
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

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,  # Deterministic for benchmarks
        splade_enabled=True,
        graph_expansion_enabled=True,
        scoring_weight_bm25=TUNED_WEIGHTS["bm25"],
        scoring_weight_actr=TUNED_WEIGHTS["actr"],
        scoring_weight_splade=TUNED_WEIGHTS["splade"],
        scoring_weight_graph=TUNED_WEIGHTS["graph"],
        actr_threshold=-999.0,  # Effectively disable ACT-R threshold
        # Disable phases that add overhead without helping retrieval baseline
        admission_enabled=False,
        reconciliation_enabled=False,
        episodes_enabled=False,
        intent_classification_enabled=False,
    )

    # Seed domain-specific topics for GLiNER entity extraction
    from benchmarks.core.datasets import MAB_TOPICS

    topic_info = MAB_TOPICS.get("mab", {})
    mab_labels = topic_info.get("labels", [])
    if mab_labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}",
            json.dumps(mab_labels),
        )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade,
    )

    return svc, store, index, graph, splade, config


# -- Per-sample evaluation --------------------------------------------------


@dataclass
class SampleResult:
    """Results for a single MAB sample (one context, 100 questions)."""

    sample_id: str
    split: str
    source: str
    num_chunks: int
    num_questions: int
    ingestion_seconds: float
    search_seconds: float
    # Per-question scores
    contains_any: list[float] = field(default_factory=list)
    best_f1: list[float] = field(default_factory=list)
    best_substring: list[float] = field(default_factory=list)
    best_exact_match: list[float] = field(default_factory=list)
    # Per-question types (if available)
    question_types: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)

    @property
    def avg_contains_any(self) -> float:
        return sum(self.contains_any) / max(len(self.contains_any), 1)

    @property
    def avg_f1(self) -> float:
        return sum(self.best_f1) / max(len(self.best_f1), 1)

    @property
    def avg_substring(self) -> float:
        return sum(self.best_substring) / max(len(self.best_substring), 1)

    @property
    def avg_exact_match(self) -> float:
        return sum(self.best_exact_match) / max(len(self.best_exact_match), 1)


async def evaluate_sample(
    sample: dict[str, Any],
    split: str,
    sample_index: int,
    top_k: int = DEFAULT_TOP_K,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> SampleResult:
    """Evaluate a single MAB sample.

    Creates a fresh NCMS instance, ingests chunked context, then
    searches for each question and scores the retrieved content.

    Args:
        sample: MAB sample dict with context, questions, answers, metadata.
        split: Split name (ar, ttl, lru, cr).
        sample_index: Index within the split.
        top_k: Number of results to retrieve per question.
        chunk_size: Context chunk size in characters.

    Returns:
        SampleResult with per-question scores.
    """
    context = sample.get("context", "")
    questions = sample.get("questions", [])
    answers = sample.get("answers", [])
    metadata = sample.get("metadata", {})
    source = metadata.get("source", split)
    q_types = metadata.get("question_types") or []
    q_ids = metadata.get("question_ids") or []

    sample_id = f"{split}-{sample_index}"

    # Create fresh NCMS instance
    svc, store, index, graph, splade, config = await _create_ncms_instance()

    # Chunk context
    chunks = chunk_context(context, chunk_size=chunk_size)
    if not chunks:
        logger.warning("Sample %s: empty context, skipping", sample_id)
        return SampleResult(
            sample_id=sample_id, split=split, source=source,
            num_chunks=0, num_questions=len(questions),
            ingestion_seconds=0.0, search_seconds=0.0,
        )

    # Ingest chunks
    t_ingest = time.perf_counter()
    for i, chunk_text in enumerate(chunks):
        await svc.store_memory(
            content=chunk_text,
            memory_type="fact",
            domains=["mab"],
            tags=[],
            structured={"chunk_index": i, "sample_id": sample_id},
        )
    ingestion_secs = time.perf_counter() - t_ingest

    logger.debug(
        "Sample %s: ingested %d chunks (%.1fs), context=%d chars",
        sample_id, len(chunks), ingestion_secs, len(context),
    )

    # Search and score each question
    result = SampleResult(
        sample_id=sample_id, split=split, source=source,
        num_chunks=len(chunks), num_questions=len(questions),
        ingestion_seconds=ingestion_secs, search_seconds=0.0,
    )

    t_search_total = time.perf_counter()
    for qi in range(len(questions)):
        question = questions[qi]
        answer_list = answers[qi] if qi < len(answers) else []
        q_type = q_types[qi] if qi < len(q_types) else ""
        q_id = q_ids[qi] if qi < len(q_ids) else f"{sample_id}-q{qi}"

        # Ensure answer_list is a list
        if isinstance(answer_list, str):
            answer_list = [answer_list]

        # Search NCMS
        search_results = await svc.search(
            query=question, domain="mab", limit=top_k,
        )

        # Concatenate retrieved memory contents
        retrieved_parts: list[str] = []
        for scored in search_results:
            retrieved_parts.append(scored.memory.content)
        retrieved_text = "\n".join(retrieved_parts)

        # Score
        scores = score_answer(retrieved_text, answer_list)

        result.contains_any.append(scores["contains_any"])
        result.best_f1.append(scores["best_f1"])
        result.best_substring.append(scores["best_substring"])
        result.best_exact_match.append(scores["best_exact_match"])
        result.question_types.append(q_type)
        result.question_ids.append(str(q_id))

    result.search_seconds = time.perf_counter() - t_search_total

    logger.info(
        "Sample %s [%s]: contains_any=%.3f  f1=%.3f  substring=%.3f  "
        "(%d questions, %d chunks, ingest=%.1fs, search=%.1fs)",
        sample_id, source,
        result.avg_contains_any, result.avg_f1, result.avg_substring,
        len(questions), len(chunks),
        result.ingestion_seconds, result.search_seconds,
    )

    return result


# -- Aggregate metrics ------------------------------------------------------


def aggregate_results(
    results: list[SampleResult],
    split_name: str | None = None,
) -> dict[str, Any]:
    """Aggregate SampleResults into summary metrics.

    Args:
        results: List of per-sample results.
        split_name: Optional split label for logging.

    Returns:
        Dict with averaged metrics and per-question-type breakdowns.
    """
    if not results:
        return {
            "num_samples": 0,
            "num_questions": 0,
            "contains_any": 0.0,
            "f1": 0.0,
            "substring": 0.0,
            "exact_match": 0.0,
        }

    all_contains: list[float] = []
    all_f1: list[float] = []
    all_sub: list[float] = []
    all_em: list[float] = []
    total_ingest = 0.0
    total_search = 0.0

    # Per question-type breakdown
    by_type: dict[str, dict[str, list[float]]] = {}

    for r in results:
        all_contains.extend(r.contains_any)
        all_f1.extend(r.best_f1)
        all_sub.extend(r.best_substring)
        all_em.extend(r.best_exact_match)
        total_ingest += r.ingestion_seconds
        total_search += r.search_seconds

        for i, qt in enumerate(r.question_types):
            if qt not in by_type:
                by_type[qt] = {"contains_any": [], "f1": [], "substring": []}
            by_type[qt]["contains_any"].append(r.contains_any[i])
            by_type[qt]["f1"].append(r.best_f1[i])
            by_type[qt]["substring"].append(r.best_substring[i])

    n = max(len(all_contains), 1)
    metrics: dict[str, Any] = {
        "num_samples": len(results),
        "num_questions": len(all_contains),
        "contains_any": sum(all_contains) / n,
        "f1": sum(all_f1) / n,
        "substring": sum(all_sub) / n,
        "exact_match": sum(all_em) / n,
        "total_ingest_seconds": round(total_ingest, 2),
        "total_search_seconds": round(total_search, 2),
    }

    # Question-type breakdown (if types are present)
    if by_type:
        type_metrics: dict[str, dict[str, float]] = {}
        for qt, scores in sorted(by_type.items()):
            if not qt:
                continue
            nt = max(len(scores["contains_any"]), 1)
            type_metrics[qt] = {
                "count": len(scores["contains_any"]),
                "contains_any": sum(scores["contains_any"]) / nt,
                "f1": sum(scores["f1"]) / nt,
                "substring": sum(scores["substring"]) / nt,
            }
        if type_metrics:
            metrics["by_question_type"] = type_metrics

    label = split_name or "ALL"
    logger.info(
        "%s: contains_any=%.4f  f1=%.4f  substring=%.4f  exact_match=%.4f  "
        "(%d samples, %d questions)",
        label, metrics["contains_any"], metrics["f1"],
        metrics["substring"], metrics["exact_match"],
        metrics["num_samples"], metrics["num_questions"],
    )

    return metrics


# -- Main benchmark runner --------------------------------------------------


async def run_mab_benchmark(
    data: dict[str, Any],
    splits: tuple[str, ...] = ALL_SPLITS,
    top_k: int = DEFAULT_TOP_K,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_samples: int | None = None,
) -> dict[str, Any]:
    """Run all requested MemoryAgentBench evaluations.

    For each split, for each sample: creates a fresh NCMS instance,
    ingests chunked context, searches for each question, and scores
    the retrieved content against ground truth answers.

    Args:
        data: MAB dataset dict with keys like 'ar', 'ttl', 'lru', 'cr'.
        splits: Which splits to evaluate.
        top_k: Number of results to retrieve per question.
        chunk_size: Context chunk size in characters.
        max_samples: Optional limit on samples per split (for testing).

    Returns:
        Nested dict with per-split results and aggregate stats.
    """
    t0 = time.perf_counter()

    logger.info("=" * 60)
    logger.info("MemoryAgentBench Evaluation")
    logger.info("=" * 60)
    logger.info("  Splits: %s", ", ".join(splits))
    logger.info("  Available: %s", ", ".join(data.keys()))
    logger.info("  Top-K: %d, Chunk size: %d chars", top_k, chunk_size)
    if max_samples:
        logger.info("  Max samples per split: %d", max_samples)

    all_sample_results: list[SampleResult] = []
    split_metrics: dict[str, Any] = {}

    for split in splits:
        if split not in data:
            logger.warning("Skipping %s: split not available in dataset", split.upper())
            split_metrics[split] = {"skipped": True, "reason": "split_not_available"}
            continue

        split_data = data[split]
        if not isinstance(split_data, list) or not split_data:
            logger.warning("Skipping %s: empty or invalid data", split.upper())
            split_metrics[split] = {"skipped": True, "reason": "empty_data"}
            continue

        # Apply max_samples limit
        samples = split_data[:max_samples] if max_samples else split_data

        logger.info("")
        logger.info(
            "Evaluating %s: %d samples (%d total in dataset)...",
            split.upper(), len(samples), len(split_data),
        )
        split_start = time.perf_counter()

        split_results: list[SampleResult] = []
        for si, sample in enumerate(samples):
            try:
                sr = await evaluate_sample(
                    sample=sample,
                    split=split,
                    sample_index=si,
                    top_k=top_k,
                    chunk_size=chunk_size,
                )
                split_results.append(sr)
                all_sample_results.append(sr)
            except Exception as exc:
                logger.error(
                    "Failed sample %s-%d: %s", split, si, exc, exc_info=True,
                )

        # Aggregate this split
        sm = aggregate_results(split_results, split_name=split.upper())
        sm["elapsed_seconds"] = round(time.perf_counter() - split_start, 2)
        split_metrics[split] = sm

    # Overall aggregation
    overall = aggregate_results(all_sample_results, split_name="OVERALL")

    total_elapsed = time.perf_counter() - t0
    results: dict[str, Any] = {
        "splits": split_metrics,
        "overall": overall,
        "total_seconds": round(total_elapsed, 1),
        "config": {
            "top_k": top_k,
            "chunk_size": chunk_size,
            "weights": TUNED_WEIGHTS,
        },
    }

    # Summary table
    logger.info("")
    logger.info("=" * 60)
    logger.info("MemoryAgentBench Summary")
    logger.info("=" * 60)
    logger.info(
        "  %-6s  %8s  %8s  %8s  %8s  %8s",
        "Split", "Samples", "Qs", "Contain", "F1", "SubStr",
    )
    logger.info("  " + "-" * 54)
    for split in splits:
        sm = split_metrics.get(split, {})
        if sm.get("skipped"):
            logger.info("  %-6s  SKIPPED (%s)", split.upper(), sm.get("reason"))
        else:
            logger.info(
                "  %-6s  %8d  %8d  %8.4f  %8.4f  %8.4f",
                split.upper(),
                sm.get("num_samples", 0),
                sm.get("num_questions", 0),
                sm.get("contains_any", 0),
                sm.get("f1", 0),
                sm.get("substring", 0),
            )
    logger.info("  " + "-" * 54)
    logger.info(
        "  %-6s  %8d  %8d  %8.4f  %8.4f  %8.4f",
        "TOTAL",
        overall.get("num_samples", 0),
        overall.get("num_questions", 0),
        overall.get("contains_any", 0),
        overall.get("f1", 0),
        overall.get("substring", 0),
    )
    logger.info("  Total time: %.1fs", total_elapsed)

    return results
