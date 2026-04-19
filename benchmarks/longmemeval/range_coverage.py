"""Phase-A instrumentation: range-extraction coverage + label ablation.

Measures whether GLiNER (with temporal labels) + the normalizer produce
usable calendar intervals on real LongMemEval inputs, AND compares
label configurations to find the fastest-with-acceptable-coverage combo.

Zero NCMS pipeline, zero SQLite, zero SPLADE — just the extraction
stack in isolation.  Runs in ~1 minute for 30 questions × 4 variants.

## Ablation matrix

Two orthogonal dimensions:

1. **Label set.**
   * `full` = 10 universal entity labels + 7 temporal labels (17 total).
   * `slim` = 4 domain-focused labels for life/event questions like
     LongMemEval: ``event``, ``location``, ``person``, ``temporal relative``.

2. **Call strategy.**
   * `combined` = one GLiNER call with entity + temporal labels merged.
   * `split` = two serial GLiNER calls — entity labels first, temporal
     labels second.  Tests whether GLiNER's per-call scaling with label
     count is worse than the overhead of a second inference pass.

Four cells: (full, combined), (full, split), (slim, combined),
(slim, split).  For each, measure:

* Query extraction rate (% of questions where the normalizer resolves
  at least one interval).
* Memory extraction rate on a sampled subset.
* Per-call p50/p95 latency and total wall time per question.

Usage::

    uv run python -m benchmarks.longmemeval.range_coverage
    uv run python -m benchmarks.longmemeval.range_coverage --limit 30
    uv run python -m benchmarks.longmemeval.range_coverage \\
        --limit 30 --memory-sample 100

Output goes to
``benchmarks/results/temporal_diagnostic/range_coverage_<ts>.{json,md,log}``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.core.runner import log_run_header, run_async, setup_logging
from benchmarks.longmemeval.harness import _parse_lme_date
from benchmarks.longmemeval.loader import load_longmemeval_dataset
from benchmarks.longmemeval.temporal_diagnostic import classify_pattern

logger = logging.getLogger(__name__)


# ── Label presets (§15.1 of the design) ───────────────────────────────

# Current default — universal entities merged with temporal labels.
# Imported lazily from ncms so we get the source of truth.
def _label_presets() -> dict[str, tuple[list[str], list[str]]]:
    """Return {preset_name: (entity_labels, temporal_labels)}.

    Each tuple lets the split variant call GLiNER twice; the combined
    variant uses their union.  Kept here (not in ncms.domain) because
    these are experiment presets, not production defaults.
    """
    from ncms.domain.entity_extraction import (
        TEMPORAL_LABELS,
        UNIVERSAL_LABELS,
    )
    return {
        "full": (list(UNIVERSAL_LABELS), list(TEMPORAL_LABELS)),
        "slim": (
            ["event", "location", "person"],
            ["temporal relative"],
        ),
    }


@dataclass
class VariantResult:
    label_preset: str        # 'full' | 'slim'
    call_strategy: str       # 'combined' | 'split'
    label_count: int         # total labels issued to GLiNER per question
    # Query-side
    query_samples: int = 0
    query_with_range: int = 0
    query_total_ms: float = 0.0  # sum of per-call times
    query_p50_ms: float = 0.0
    query_p95_ms: float = 0.0
    # Memory-side (optional — run once per variant on a sample)
    memory_samples: int = 0
    memory_with_range: int = 0

    @property
    def query_rate(self) -> float:
        return (
            self.query_with_range / self.query_samples
            if self.query_samples else 0.0
        )

    @property
    def memory_rate(self) -> float:
        return (
            self.memory_with_range / self.memory_samples
            if self.memory_samples else 0.0
        )


@dataclass
class QueryDetail:
    """One question measured across all variants — for per-question
    drill-down when the aggregate hides a pattern-specific miss."""

    question_id: str
    pattern: str
    question: str
    per_variant_had_range: dict[str, bool] = field(default_factory=dict)
    per_variant_ms: dict[str, float] = field(default_factory=dict)


def _run_variant_on_query(
    question_text: str,
    reference_time: datetime,
    entity_labels: list[str],
    temporal_labels: list[str],
    strategy: str,
    extract_fn,
    split_fn,
    resolve_fn,
) -> tuple[bool, float]:
    """Return (had_range, total_ms) for one question under one variant."""
    t = time.perf_counter()
    if strategy == "combined":
        # Deduplicate if entity and temporal sets overlap.
        merged_labels = list(dict.fromkeys(entity_labels + temporal_labels))
        mixed = extract_fn(question_text, merged_labels)
    elif strategy == "split":
        # Two serial calls: entities, then temporal.  We union the
        # outputs and feed to the splitter just like combined.
        entity_out = extract_fn(question_text, entity_labels)
        temporal_out = extract_fn(question_text, temporal_labels)
        mixed = list(entity_out) + list(temporal_out)
    else:
        raise ValueError(f"unknown strategy {strategy!r}")
    _ent, spans = split_fn(mixed)
    query_range = resolve_fn(spans, reference_time) if spans else None
    return (query_range is not None), (time.perf_counter() - t) * 1000


def _run_variant_on_memory(
    content: str,
    reference_time: datetime,
    entity_labels: list[str],
    temporal_labels: list[str],
    strategy: str,
    extract_fn,
    split_fn,
    normalize_fn,
) -> bool:
    """Return True if this memory's content produced a resolvable interval."""
    if strategy == "combined":
        merged_labels = list(dict.fromkeys(entity_labels + temporal_labels))
        mixed = extract_fn(content, merged_labels)
    else:
        mixed = list(extract_fn(content, entity_labels)) + list(
            extract_fn(content, temporal_labels)
        )
    _ent, spans = split_fn(mixed)
    if not spans:
        return False
    intervals = normalize_fn(spans, reference_time)
    return bool(intervals)


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(p * len(s))))
    return s[idx]


def _format_markdown(
    variants: dict[tuple[str, str], VariantResult],
    details: list[QueryDetail],
    total_questions: int,
    elapsed_s: float,
) -> str:
    lines: list[str] = []
    lines.append("# P1-Temporal-Experiment — Phase A Range Coverage")
    lines.append("")
    lines.append(
        f"**Questions analyzed:** {total_questions} "
        "(LongMemEval temporal-reasoning subset)"
    )
    lines.append(f"**Elapsed:** {elapsed_s:.1f} s")
    lines.append("")

    # Aggregate table
    lines.append("## Variant comparison")
    lines.append("")
    lines.append(
        "| Label preset | Strategy | Labels/call | "
        "Query R | Memory R | p50 ms | p95 ms |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for key in sorted(variants):
        v = variants[key]
        lines.append(
            f"| {v.label_preset} | {v.call_strategy} "
            f"| {v.label_count} "
            f"| {v.query_rate * 100:.1f}% "
            f"| {v.memory_rate * 100:.1f}% "
            f"| {v.query_p50_ms:.0f} "
            f"| {v.query_p95_ms:.0f} |"
        )
    lines.append("")
    lines.append(
        "**Reading:** *Query R* and *Memory R* are the share of inputs "
        "where the normalizer resolved ≥1 calendar interval.  Latency "
        "columns are per-question (sum of sub-call times for `split`)."
    )
    lines.append("")

    # Per-pattern breakdown for the baseline variant (full, combined)
    baseline_key = ("full", "combined")
    lines.append("## Query rate by pattern (baseline: full · combined)")
    lines.append("")
    lines.append("| Pattern | # Qs | Had range | Rate |")
    lines.append("|---|---:|---:|---:|")
    pattern_buckets: dict[str, list[QueryDetail]] = {}
    for d in details:
        pattern_buckets.setdefault(d.pattern, []).append(d)
    key_str = f"{baseline_key[0]}-{baseline_key[1]}"
    for p, items in sorted(
        pattern_buckets.items(), key=lambda kv: -len(kv[1]),
    ):
        n = len(items)
        hit = sum(
            1 for d in items
            if d.per_variant_had_range.get(key_str, False)
        )
        rate = hit / n if n else 0.0
        lines.append(f"| {p} | {n} | {hit} | {rate * 100:.1f}% |")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Phase B gate: ≥ 80% query extraction rate.  Below that, "
        "the filter can't meaningfully help — revisit labels or "
        "fall back to the subject-scoped / metadata-anchored path "
        "in §14 of the design."
    )
    lines.append(
        "- If `slim` holds query coverage within ~5% of `full` but "
        "cuts latency meaningfully, ship slim as the default."
    )
    lines.append(
        "- If `split` latency is ≥ 1.5× `combined` without a coverage "
        "gain, keep the combined single call."
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> None:
    from ncms.config import NCMSConfig
    from ncms.domain.temporal_normalizer import (
        merge_intervals,
        normalize_spans,
    )
    from ncms.infrastructure.extraction.gliner_extractor import (
        extract_entities_gliner,
    )
    from ncms.application.retrieval.pipeline import RetrievalPipeline

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading LongMemEval dataset...")
    sessions_by_q, questions = load_longmemeval_dataset(
        cache_dir=cache_dir, dataset_file=args.dataset,
    )
    temporal_qs = [
        q for q in questions if q.category == "temporal-reasoning"
    ]
    logger.info(
        "Loaded %d total, %d temporal-reasoning",
        len(questions), len(temporal_qs),
    )
    if args.limit:
        temporal_qs = temporal_qs[:args.limit]

    cfg = NCMSConfig(db_path=":memory:")
    presets = _label_presets()

    def extract(text: str, labels: list[str]):
        return extract_entities_gliner(
            text, labels=labels,
            model_name=cfg.gliner_model,
            threshold=cfg.gliner_threshold,
            cache_dir=cfg.model_cache_dir,
        )

    split = RetrievalPipeline.split_entity_and_temporal_spans
    resolve = RetrievalPipeline.resolve_temporal_range

    logger.info("Warming up GLiNER...")
    _ = extract("warmup June 5, 2024", ["person", "date"])

    # Build variant records.
    variants: dict[tuple[str, str], VariantResult] = {}
    latencies: dict[tuple[str, str], list[float]] = {}
    for preset_name, (e_labels, t_labels) in presets.items():
        for strategy in ("combined", "split"):
            if strategy == "combined":
                label_count = len(
                    list(dict.fromkeys(e_labels + t_labels)),
                )
            else:
                label_count = len(e_labels) + len(t_labels)
            variants[(preset_name, strategy)] = VariantResult(
                label_preset=preset_name,
                call_strategy=strategy,
                label_count=label_count,
            )
            latencies[(preset_name, strategy)] = []

    # 1) Query-side: each question × each variant.
    details: list[QueryDetail] = []
    t0 = time.perf_counter()
    for qi, q in enumerate(temporal_qs):
        ref_time = _parse_lme_date(q.question_date) or datetime.now(UTC)
        d = QueryDetail(
            question_id=q.question_id,
            pattern=classify_pattern(q.question),
            question=q.question[:120],
        )
        for (preset_name, strategy), vr in variants.items():
            e_labels, t_labels = presets[preset_name]
            had_range, ms = _run_variant_on_query(
                q.question, ref_time,
                e_labels, t_labels, strategy,
                extract, split, resolve,
            )
            vr.query_samples += 1
            if had_range:
                vr.query_with_range += 1
            vr.query_total_ms += ms
            latencies[(preset_name, strategy)].append(ms)
            key_str = f"{preset_name}-{strategy}"
            d.per_variant_had_range[key_str] = had_range
            d.per_variant_ms[key_str] = ms
        details.append(d)
        if (qi + 1) % 10 == 0 or qi == 0:
            logger.info(
                "Q %d/%d — full·combined hit rate: %.1f%%  slim·combined: %.1f%%",
                qi + 1, len(temporal_qs),
                variants[("full", "combined")].query_rate * 100,
                variants[("slim", "combined")].query_rate * 100,
            )

    for key, vr in variants.items():
        lats = latencies[key]
        vr.query_p50_ms = _pctile(lats, 0.5)
        vr.query_p95_ms = _pctile(lats, 0.95)

    # 2) Memory-side: reservoir sample, each variant processes the
    # same content so the comparison is fair.
    if args.memory_sample > 0:
        logger.info(
            "Sampling %d haystack memories for content-range coverage",
            args.memory_sample,
        )
        all_turns: list[tuple[str, datetime | None]] = []
        for q in temporal_qs:
            for sess in sessions_by_q.get(q.question_id, []):
                session_date = None
                if getattr(sess, "timestamp", None):
                    session_date = _parse_lme_date(sess.timestamp)
                for turn in sess.turns:
                    all_turns.append((turn.content, session_date))
        rng = random.Random(42)
        if len(all_turns) > args.memory_sample:
            all_turns = rng.sample(all_turns, args.memory_sample)
        for content, ref in all_turns:
            if not content or len(content) < 10:
                continue
            anchor = ref or datetime.now(UTC)
            for (preset_name, strategy), vr in variants.items():
                e_labels, t_labels = presets[preset_name]
                had = _run_variant_on_memory(
                    content, anchor,
                    e_labels, t_labels, strategy,
                    extract, split, normalize_spans,
                )
                vr.memory_samples += 1
                if had:
                    vr.memory_with_range += 1

    elapsed_s = time.perf_counter() - t0

    # Write outputs
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"range_coverage_{ts}.json"
    md_path = output_dir / f"range_coverage_{ts}.md"
    json_path.write_text(json.dumps(
        {
            "total_questions": len(temporal_qs),
            "elapsed_s": elapsed_s,
            "variants": [
                {
                    "label_preset": v.label_preset,
                    "call_strategy": v.call_strategy,
                    "label_count": v.label_count,
                    "query_samples": v.query_samples,
                    "query_with_range": v.query_with_range,
                    "query_rate": v.query_rate,
                    "query_p50_ms": v.query_p50_ms,
                    "query_p95_ms": v.query_p95_ms,
                    "memory_samples": v.memory_samples,
                    "memory_with_range": v.memory_with_range,
                    "memory_rate": v.memory_rate,
                }
                for v in variants.values()
            ],
            "questions": [asdict(d) for d in details],
        },
        indent=2,
    ))
    md_path.write_text(_format_markdown(
        variants, details, len(temporal_qs), elapsed_s,
    ))
    for suffix in ("json", "md"):
        latest = output_dir / f"range_coverage_latest.{suffix}"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(f"range_coverage_{ts}.{suffix}")
    logger.info("Wrote %s", md_path)
    for key, vr in sorted(variants.items()):
        logger.info(
            "%s · %s: Q=%.1f%% M=%.1f%% p50=%.0fms p95=%.0fms",
            vr.label_preset, vr.call_strategy,
            vr.query_rate * 100, vr.memory_rate * 100,
            vr.query_p50_ms, vr.query_p95_ms,
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default="longmemeval_s")
    p.add_argument("--cache-dir", default=None)
    p.add_argument(
        "--output-dir",
        default="benchmarks/results/temporal_diagnostic",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Run only first N questions (debug)",
    )
    p.add_argument(
        "--memory-sample", type=int, default=200,
        help="Size of haystack memory sample (0 = skip)",
    )
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = setup_logging("range_coverage", output_dir)
    log_run_header(
        "P1-temporal-experiment Phase A — Range Coverage + Label Ablation",
        logger,
    )
    logger.info("Log file: %s", log_file)

    run_async(_run(args), "P1-temporal Phase A range coverage")


if __name__ == "__main__":
    main()
