"""SWE-bench Django structural analysis.

Pre-experiment validation script that analyzes the Django subset of SWE-bench
to predict whether it has sufficient relational structure for NCMS's cognitive
architecture (entity overlap, graph connectivity, temporal ordering).

Produces a JSON report + markdown summary comparing predicted SWE-bench
metrics against observed BEIR SciFact metrics.

Usage:
    uv run python -m benchmarks.swebench_analysis [--sample-size 100] [--output-dir ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from benchmarks.swebench_loader import (
    SWEInstance,
    load_swebench_django,
    split_train_test,
)
from benchmarks.swebench_qrels import (
    build_ar_qrels,
    build_cr_qrels,
    build_lru_queries,
    build_ttl_labels,
)

logger = logging.getLogger(__name__)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def analyze_file_overlap(instances: list[SWEInstance]) -> dict:
    """Compute pairwise file overlap statistics."""
    n = len(instances)
    jaccards: list[float] = []
    pairs_with_overlap = 0
    total_pairs = 0

    for i in range(n):
        fi = set(instances[i].files_touched)
        if not fi:
            continue
        for j in range(i + 1, n):
            fj = set(instances[j].files_touched)
            if not fj:
                continue
            total_pairs += 1
            jac = _jaccard(fi, fj)
            if jac > 0:
                jaccards.append(jac)
                pairs_with_overlap += 1

    return {
        "total_pairs": total_pairs,
        "pairs_with_overlap": pairs_with_overlap,
        "overlap_fraction": pairs_with_overlap / max(total_pairs, 1),
        "jaccard_mean": statistics.mean(jaccards) if jaccards else 0.0,
        "jaccard_median": statistics.median(jaccards) if jaccards else 0.0,
        "jaccard_max": max(jaccards) if jaccards else 0.0,
        "jaccard_std": statistics.stdev(jaccards) if len(jaccards) > 1 else 0.0,
    }


def analyze_file_distribution(instances: list[SWEInstance]) -> dict:
    """Analyze which files appear across the most issues."""
    file_counts: Counter[str] = Counter()
    for inst in instances:
        for f in inst.files_touched:
            file_counts[f] += 1

    return {
        "unique_files": len(file_counts),
        "files_in_2plus_issues": sum(1 for c in file_counts.values() if c >= 2),
        "files_in_5plus_issues": sum(1 for c in file_counts.values() if c >= 5),
        "files_in_10plus_issues": sum(1 for c in file_counts.values() if c >= 10),
        "top_20_files": file_counts.most_common(20),
    }


def analyze_subsystems(instances: list[SWEInstance]) -> dict:
    """Analyze subsystem distribution."""
    dist: Counter[str] = Counter()
    for inst in instances:
        dist[inst.subsystem] += 1
    return {
        "distribution": dict(dist.most_common()),
        "num_subsystems": len(dist),
    }


def analyze_temporal(instances: list[SWEInstance]) -> dict:
    """Analyze temporal distribution."""
    years: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    for inst in instances:
        years[inst.created_at[:4]] += 1
        versions[inst.version] += 1

    return {
        "year_distribution": dict(sorted(years.items())),
        "version_distribution": dict(versions.most_common()),
        "date_range": {
            "earliest": instances[0].created_at[:10],
            "latest": instances[-1].created_at[:10],
        },
    }


def analyze_entity_overlap_sample(
    instances: list[SWEInstance],
    sample_size: int = 100,
) -> dict:
    """Run GLiNER on a sample of issues and measure entity overlap.

    This is the key validation: do Django issues share enough entities
    to create a connected knowledge graph?
    """
    try:
        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner
    except ImportError:
        logger.warning("GLiNER not available — skipping entity overlap analysis")
        return {"error": "GLiNER not available"}

    # Django-tuned labels
    labels = [
        "class", "method", "function", "module", "field",
        "model", "view", "middleware", "url_pattern", "form",
        "template", "queryset", "manager", "migration", "signal",
        "test_case", "exception", "setting", "command", "mixin",
    ]

    sample = instances[:sample_size]
    logger.info("Running GLiNER entity extraction on %d issues...", len(sample))

    # Extract entities per issue
    issue_entities: list[set[str]] = []
    all_entities: Counter[str] = Counter()
    t0 = time.perf_counter()

    for i, inst in enumerate(sample):
        text = inst.content[:3000]  # Limit to avoid very long issues
        entities = extract_entities_gliner(text, labels=labels)
        ent_names = {e["name"].lower() for e in entities}
        issue_entities.append(ent_names)
        for name in ent_names:
            all_entities[name] += 1

        if (i + 1) % 20 == 0:
            logger.info("  Extracted entities from %d/%d issues", i + 1, len(sample))

    elapsed = time.perf_counter() - t0
    logger.info("Entity extraction: %.1fs (%.1f issues/sec)", elapsed, len(sample) / elapsed)

    # Pairwise entity Jaccard
    n = len(issue_entities)
    jaccards: list[float] = []
    pairs_with_overlap = 0
    total_pairs = 0

    for i in range(n):
        if not issue_entities[i]:
            continue
        for j in range(i + 1, n):
            if not issue_entities[j]:
                continue
            total_pairs += 1
            jac = _jaccard(issue_entities[i], issue_entities[j])
            if jac > 0:
                jaccards.append(jac)
                pairs_with_overlap += 1

    # Predict graph connectivity
    entities_in_2plus = sum(1 for c in all_entities.values() if c >= 2)
    entities_in_5plus = sum(1 for c in all_entities.values() if c >= 5)

    # Estimate entity-memory edges (each entity occurrence = 1 edge)
    total_edges = sum(len(e) for e in issue_entities)

    # Entities per issue
    ents_per_issue = [len(e) for e in issue_entities if e]

    return {
        "sample_size": len(sample),
        "extraction_time_s": round(elapsed, 1),
        "unique_entities": len(all_entities),
        "entities_in_2plus_issues": entities_in_2plus,
        "entities_in_5plus_issues": entities_in_5plus,
        "total_entity_memory_edges": total_edges,
        "entities_per_issue": {
            "mean": statistics.mean(ents_per_issue) if ents_per_issue else 0,
            "median": statistics.median(ents_per_issue) if ents_per_issue else 0,
            "max": max(ents_per_issue) if ents_per_issue else 0,
        },
        "pairwise_overlap": {
            "total_pairs": total_pairs,
            "pairs_with_overlap": pairs_with_overlap,
            "overlap_fraction": pairs_with_overlap / max(total_pairs, 1),
            "jaccard_mean": statistics.mean(jaccards) if jaccards else 0.0,
            "jaccard_median": statistics.median(jaccards) if jaccards else 0.0,
            "jaccard_max": max(jaccards) if jaccards else 0.0,
        },
        "top_20_entities": all_entities.most_common(20),
        # Graph predictions (extrapolated to full 850 issues)
        "predicted_graph": {
            "note": f"Extrapolated from {len(sample)} sample to full corpus",
            "predicted_unique_entities": int(
                len(all_entities) * len(instances) / len(sample) * 0.6
            ),
            "predicted_connected_ratio": pairs_with_overlap / max(total_pairs, 1),
        },
    }


def analyze_qrel_coverage(instances: list[SWEInstance]) -> dict:
    """Validate that all 4 competency splits have sufficient coverage."""
    train, test = split_train_test(instances)

    ar_qrels = build_ar_qrels(train, test)
    ttl_labels = build_ttl_labels(test)
    cr_qrels, cr_queries = build_cr_qrels(instances)
    lru_queries, lru_qrels = build_lru_queries(instances)

    return {
        "ar": {
            "queries_with_judgments": len(ar_qrels),
            "total_test_queries": len(test),
            "coverage": len(ar_qrels) / max(len(test), 1),
            "avg_relevant_per_query": (
                statistics.mean([len(v) for v in ar_qrels.values()])
                if ar_qrels else 0
            ),
        },
        "ttl": {
            "total_labeled": len(ttl_labels),
            "subsystem_distribution": dict(Counter(ttl_labels.values()).most_common()),
        },
        "cr": {
            "file_state_queries": len(cr_qrels),
            "avg_temporal_depth": (
                statistics.mean([len(v) for v in cr_qrels.values()])
                if cr_qrels else 0
            ),
        },
        "lru": {
            "holistic_queries": len(lru_queries),
            "avg_relevant_per_query": (
                statistics.mean([len(v) for v in lru_qrels.values()])
                if lru_qrels else 0
            ),
        },
    }


def generate_comparison_table(report: dict) -> str:
    """Generate markdown comparison: BEIR SciFact vs SWE-bench Django."""
    entity = report.get("entity_overlap", {})
    file_info = report.get("file_distribution", {})

    # SciFact observed values from dream experiment
    scifact = {
        "entities": 51357,
        "edges": 0,
        "components": 51357,
        "density": 0.0,
        "pagerank_max": 0.0,
        "spreading_mean": 0.0558,
        "actr_crossover": "None (best=0.0)",
        "dream_delta": "+0.04%",
    }

    # SWE-bench predictions
    pred = entity.get("predicted_graph", {})
    pred_entities = pred.get("predicted_unique_entities", "?")
    ent_2plus = entity.get("entities_in_2plus_issues", "?")
    overlap_frac = entity.get("pairwise_overlap", {}).get("overlap_fraction", 0)

    lines = [
        "## Structural Comparison: BEIR SciFact vs SWE-bench Django\n",
        "| Metric | SciFact (observed) | SWE-bench Django (predicted) |",
        "|--------|--------------------|------------------------------|",
        f"| Unique entities | {scifact['entities']:,} | ~{pred_entities} |",
        f"| Graph edges | {scifact['edges']} | >> 0 (entities in 2+ issues: {ent_2plus}) |",
        f"| Connected components | {scifact['components']:,} | << {pred_entities} |",
        f"| Entity overlap fraction | ~0 | {overlap_frac:.3f} |",
        f"| Unique files | N/A | {file_info.get('unique_files', '?')} |",
        f"| Files in 5+ issues | N/A | {file_info.get('files_in_5plus_issues', '?')} |",
        f"| ACT-R crossover | {scifact['actr_crossover']} | Expected 0.1-0.2 |",
        f"| Dream cycle delta | {scifact['dream_delta']} | Expected > +1% |",
    ]

    return "\n".join(lines)


def generate_report(report: dict) -> str:
    """Generate full markdown report."""
    lines = ["# SWE-bench Django Structural Analysis\n"]

    # Overview
    lines.append(f"**Total Django issues**: {report['total_instances']}")
    lines.append(f"**Date range**: {report['temporal']['date_range']['earliest']} to "
                 f"{report['temporal']['date_range']['latest']}\n")

    # Subsystems
    lines.append("## Subsystem Distribution\n")
    lines.append("| Subsystem | Count |")
    lines.append("|-----------|-------|")
    for sub, count in report["subsystems"]["distribution"].items():
        lines.append(f"| {sub} | {count} |")
    lines.append("")

    # File overlap
    fo = report["file_overlap"]
    lines.append("## File Overlap (AR Ground Truth Signal)\n")
    lines.append(f"- Total issue pairs: {fo['total_pairs']:,}")
    lines.append(f"- Pairs with file overlap: {fo['pairs_with_overlap']:,} "
                 f"({fo['overlap_fraction']:.1%})")
    lines.append(f"- Jaccard mean: {fo['jaccard_mean']:.4f}")
    lines.append(f"- Jaccard median: {fo['jaccard_median']:.4f}")
    lines.append(f"- Jaccard max: {fo['jaccard_max']:.4f}\n")

    # File distribution
    fd = report["file_distribution"]
    lines.append("## File Distribution\n")
    lines.append(f"- Unique files: {fd['unique_files']}")
    lines.append(f"- Files in 2+ issues: {fd['files_in_2plus_issues']}")
    lines.append(f"- Files in 5+ issues: {fd['files_in_5plus_issues']}")
    lines.append(f"- Files in 10+ issues: {fd['files_in_10plus_issues']}\n")
    lines.append("### Top 20 Most-Modified Files\n")
    lines.append("| File | Issues |")
    lines.append("|------|--------|")
    for filepath, count in fd["top_20_files"]:
        lines.append(f"| `{filepath}` | {count} |")
    lines.append("")

    # Entity overlap
    if "error" not in report.get("entity_overlap", {}):
        eo = report["entity_overlap"]
        po = eo["pairwise_overlap"]
        lines.append("## Entity Overlap (Graph Connectivity Signal)\n")
        lines.append(f"- Sample size: {eo['sample_size']} issues")
        lines.append(f"- Unique entities: {eo['unique_entities']}")
        lines.append(f"- Entities in 2+ issues: {eo['entities_in_2plus_issues']}")
        lines.append(f"- Entities in 5+ issues: {eo['entities_in_5plus_issues']}")
        epi = eo["entities_per_issue"]
        lines.append(f"- Entities per issue: mean={epi['mean']:.1f}, "
                     f"median={epi['median']:.0f}, max={epi['max']}")
        lines.append(f"- Pairwise overlap fraction: {po['overlap_fraction']:.3f}")
        lines.append(f"- Jaccard mean: {po['jaccard_mean']:.4f}")
        lines.append(f"- Jaccard max: {po['jaccard_max']:.4f}\n")
        lines.append("### Top 20 Entities\n")
        lines.append("| Entity | Count |")
        lines.append("|--------|-------|")
        for ent, count in eo["top_20_entities"]:
            lines.append(f"| {ent} | {count} |")
        lines.append("")

    # Qrel coverage
    qc = report["qrel_coverage"]
    lines.append("## Competency Split Coverage\n")
    lines.append("| Split | Queries | Coverage | Notes |")
    lines.append("|-------|---------|----------|-------|")
    ar = qc["ar"]
    lines.append(f"| AR (Accurate Retrieval) | {ar['queries_with_judgments']} | "
                 f"{ar['coverage']:.0%} | avg {ar['avg_relevant_per_query']:.1f} relevant/query |")
    ttl = qc["ttl"]
    lines.append(f"| TTL (Test-Time Learning) | {ttl['total_labeled']} | 100% | "
                 f"{len(ttl['subsystem_distribution'])} subsystems |")
    cr = qc["cr"]
    lines.append(f"| CR (Conflict Resolution) | {cr['file_state_queries']} | N/A | "
                 f"avg depth {cr['avg_temporal_depth']:.1f} |")
    lru = qc["lru"]
    lines.append(f"| LRU (Long-Range Understanding) | {lru['holistic_queries']} | N/A | "
                 f"avg {lru['avg_relevant_per_query']:.0f} relevant/query |")
    lines.append("")

    # Comparison table
    lines.append(generate_comparison_table(report))

    # Temporal
    lines.append("\n## Temporal Distribution\n")
    lines.append("| Year | Count |")
    lines.append("|------|-------|")
    for year, count in report["temporal"]["year_distribution"].items():
        lines.append(f"| {year} | {count} |")

    # Validation verdict
    lines.append("\n## Validation Verdict\n")

    entity_ok = True
    if "error" not in report.get("entity_overlap", {}):
        eo = report["entity_overlap"]
        entity_jac = eo["pairwise_overlap"]["jaccard_mean"]
        if entity_jac < 0.05:
            lines.append(f"- **WARN**: Entity overlap Jaccard mean ({entity_jac:.4f}) < 0.05")
            entity_ok = False

    file_ok = fo["overlap_fraction"] > 0.01
    ar_ok = ar["queries_with_judgments"] >= 20
    cr_ok = cr["file_state_queries"] >= 10

    if entity_ok and file_ok and ar_ok and cr_ok:
        lines.append("**PASS** — Dataset has sufficient relational structure for NCMS evaluation.")
    else:
        issues = []
        if not entity_ok:
            issues.append("low entity overlap")
        if not file_ok:
            issues.append("low file overlap")
        if not ar_ok:
            issues.append("insufficient AR queries")
        if not cr_ok:
            issues.append("insufficient CR queries")
        lines.append(f"**FAIL** — Issues: {', '.join(issues)}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench Django structural analysis")
    parser.add_argument(
        "--sample-size", type=int, default=100,
        help="Number of issues for GLiNER entity extraction sample (default: 100)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="benchmarks/results/swebench/analysis",
        help="Output directory for reports",
    )
    parser.add_argument(
        "--skip-entities", action="store_true",
        help="Skip GLiNER entity extraction (faster, no GPU needed)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    t0 = time.perf_counter()

    # Load dataset
    instances = load_swebench_django()

    report: dict = {
        "total_instances": len(instances),
    }

    # Structural analyses
    logger.info("Analyzing file overlap...")
    report["file_overlap"] = analyze_file_overlap(instances)

    logger.info("Analyzing file distribution...")
    report["file_distribution"] = analyze_file_distribution(instances)

    logger.info("Analyzing subsystems...")
    report["subsystems"] = analyze_subsystems(instances)

    logger.info("Analyzing temporal distribution...")
    report["temporal"] = analyze_temporal(instances)

    # Entity extraction (optional, slower)
    if not args.skip_entities:
        logger.info("Analyzing entity overlap (GLiNER on %d issues)...", args.sample_size)
        report["entity_overlap"] = analyze_entity_overlap_sample(
            instances, sample_size=args.sample_size,
        )
    else:
        report["entity_overlap"] = {"error": "Skipped (--skip-entities)"}

    # Qrel coverage
    logger.info("Analyzing qrel coverage...")
    report["qrel_coverage"] = analyze_qrel_coverage(instances)

    elapsed = time.perf_counter() - t0
    report["analysis_time_s"] = round(elapsed, 1)

    # Output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "analysis_report.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("JSON report: %s", json_path)

    md_path = out_dir / "analysis_report.md"
    md_content = generate_report(report)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown report: %s", md_path)

    # Print summary to console
    print(f"\n{'=' * 60}")
    print("SWE-bench Django Analysis Complete")
    print(f"{'=' * 60}")
    print(f"  Issues: {len(instances)}")
    fo = report["file_overlap"]
    print(f"  File overlap pairs: {fo['pairs_with_overlap']:,} / {fo['total_pairs']:,} "
          f"({fo['overlap_fraction']:.1%})")
    if "error" not in report["entity_overlap"]:
        eo = report["entity_overlap"]
        print(f"  Entity overlap: {eo['pairwise_overlap']['overlap_fraction']:.1%} "
              f"(Jaccard mean: {eo['pairwise_overlap']['jaccard_mean']:.4f})")
    qc = report["qrel_coverage"]
    print(f"  AR queries: {qc['ar']['queries_with_judgments']}")
    print(f"  CR queries: {qc['cr']['file_state_queries']}")
    print(f"  LRU queries: {qc['lru']['holistic_queries']}")
    print(f"  TTL subsystems: {len(qc['ttl']['subsystem_distribution'])}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"  Reports: {out_dir}/")

    # Exit code based on validation
    if fo["overlap_fraction"] < 0.01:
        print("\nFAIL: Insufficient file overlap for AR evaluation")
        sys.exit(1)


if __name__ == "__main__":
    main()
