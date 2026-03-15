"""Dream cycle experiment report generation.

Produces markdown tables and JSON results from dream experiment output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from benchmarks.dream_configs import DREAM_STAGES

logger = logging.getLogger(__name__)


def generate_dream_table(results: dict[str, Any]) -> str:
    """Generate a markdown table showing retrieval progression across stages.

    Args:
        results: Output from run_dream_experiment().

    Returns:
        Markdown table string.
    """
    stages = results.get("stages", {})
    if not stages:
        return "No stage results."

    header = (
        "| Stage | nDCG@10 | \u0394% | MRR@10 | Recall@100 "
        "| Insights | Memories | Time |"
    )
    separator = (
        "|-------|---------|------|--------|------------"
        "|----------|----------|------|"
    )
    lines = [header, separator]

    for stage in DREAM_STAGES:
        sr = stages.get(stage.name)
        if not sr:
            continue

        rm = sr.get("retrieval_metrics", {})
        ndcg = rm.get("nDCG@10", 0.0)
        mrr = rm.get("MRR@10", 0.0)
        recall = rm.get("Recall@100", 0.0)
        delta = sr.get("delta_pct", 0.0)
        insights = sr.get("insight_count", 0)
        total = sr.get("total_memories", 0)
        elapsed = sr.get("elapsed_seconds", 0.0)

        delta_str = f"+{delta:.2f}%" if delta > 0 else (f"{delta:.2f}%" if delta < 0 else "\u2014")

        lines.append(
            f"| {stage.display_name} "
            f"| {ndcg:.4f} "
            f"| {delta_str} "
            f"| {mrr:.4f} "
            f"| {recall:.4f} "
            f"| {insights} "
            f"| {total} "
            f"| {elapsed:.1f}s |"
        )

    return "\n".join(lines)


def generate_dream_summary(
    all_results: dict[str, dict[str, Any]],
) -> str:
    """Generate a cross-dataset summary table (nDCG@10 per stage per dataset).

    Args:
        all_results: {dataset_name: experiment_results}

    Returns:
        Markdown table string.
    """
    datasets = list(all_results.keys())

    header = "| Stage |"
    separator = "|-------|"
    for ds in datasets:
        header += f" {ds} |"
        separator += "--------|"

    lines = [header, separator]

    for stage in DREAM_STAGES:
        row = f"| {stage.display_name} |"
        for ds in datasets:
            sr = all_results[ds].get("stages", {}).get(stage.name, {})
            rm = sr.get("retrieval_metrics", {})
            ndcg = rm.get("nDCG@10", 0.0)
            delta = sr.get("delta_pct", 0.0)
            delta_str = f" ({delta:+.1f}%)" if delta != 0 else ""
            row += f" {ndcg:.4f}{delta_str} |"
        lines.append(row)

    return "\n".join(lines)


def generate_diagnostics_structural_table(results: dict[str, Any]) -> str:
    """Generate a structural diagnostics table (graph, associations, abstracts).

    Args:
        results: Output from run_dream_experiment().

    Returns:
        Markdown table string.
    """
    stages = results.get("stages", {})
    if not stages:
        return "No diagnostics."

    header = (
        "| Stage | Entities | Edges | Density | Components "
        "| PR max | Assoc Pairs | Abstracts |"
    )
    separator = (
        "|-------|----------|-------|---------|------------"
        "|--------|-------------|-----------|"
    )
    lines = [header, separator]

    for stage in DREAM_STAGES:
        sr = stages.get(stage.name)
        if not sr:
            continue
        diag = sr.get("diagnostics", {})
        if not diag:
            continue

        g = diag.get("graph", {})
        a = diag.get("associations", {})
        ab = diag.get("abstracts", {})
        total_abstracts = sum(ab.values()) if ab else 0

        lines.append(
            f"| {stage.display_name} "
            f"| {g.get('entity_count', 0)} "
            f"| {g.get('relationship_count', 0)} "
            f"| {g.get('density', 0):.4f} "
            f"| {g.get('weakly_connected_components', 0)} "
            f"| {g.get('pagerank_max', 0):.4f} "
            f"| {a.get('pair_count', 0)} "
            f"| {total_abstracts} |"
        )

    return "\n".join(lines)


def generate_diagnostics_cognitive_table(results: dict[str, Any]) -> str:
    """Generate a cognitive diagnostics table (ACT-R, importance, spreading, insights).

    Args:
        results: Output from run_dream_experiment().

    Returns:
        Markdown table string.
    """
    stages = results.get("stages", {})
    if not stages:
        return "No diagnostics."

    header = (
        "| Stage | ACT-R mean | Above thr | Import mean "
        "| Spread mean | Insight top-10 | Improved | Degraded |"
    )
    separator = (
        "|-------|------------|-----------|-------------"
        "|-------------|----------------|----------|----------|"
    )
    lines = [header, separator]

    for stage in DREAM_STAGES:
        sr = stages.get(stage.name)
        if not sr:
            continue
        diag = sr.get("diagnostics", {})
        if not diag:
            continue

        actr = diag.get("actr", {})
        imp = diag.get("importance", {})
        spread = diag.get("spreading", {})
        insight = diag.get("insight_contribution", {})
        pqd = diag.get("per_query_deltas")

        improved_str = str(pqd["improved"]) if pqd else "\u2014"
        degraded_str = str(pqd["degraded"]) if pqd else "\u2014"

        lines.append(
            f"| {stage.display_name} "
            f"| {actr.get('mean', 0):.3f} "
            f"| {actr.get('count_above_threshold', 0)} "
            f"| {imp.get('mean', 0):.2f} "
            f"| {spread.get('mean', 0):.4f} "
            f"| {insight.get('in_top10', 0)} "
            f"| {improved_str} "
            f"| {degraded_str} |"
        )

    return "\n".join(lines)


def generate_actr_crossover_table(results: dict[str, Any]) -> str:
    """Generate an ACT-R crossover table showing optimal weight per stage.

    Shows nDCG@10 at each ACT-R weight for each stage. This reveals
    whether dream cycles make ACT-R beneficial (design spec conditions A-D).

    Args:
        results: Output from run_dream_experiment().

    Returns:
        Markdown table string.
    """
    stages = results.get("stages", {})
    if not stages:
        return "No crossover results."

    # Collect all ACT-R weights tested
    actr_weights: list[str] = []
    for sr in stages.values():
        crossover = sr.get("actr_crossover", {})
        if crossover:
            actr_weights = sorted(crossover.keys())
            break

    if not actr_weights:
        return "No ACT-R crossover data."

    # Header
    header = "| Stage |"
    separator = "|-------|"
    for w in actr_weights:
        header += f" {w} |"
        separator += "--------|"
    header += " Best |"
    separator += "------|"
    lines = [header, separator]

    for stage in DREAM_STAGES:
        sr = stages.get(stage.name)
        if not sr:
            continue
        crossover = sr.get("actr_crossover", {})
        if not crossover:
            continue

        row = f"| {stage.display_name} |"
        best_w = ""
        best_ndcg = -1.0
        for w in actr_weights:
            ndcg = crossover.get(w, {}).get("nDCG@10", 0.0)
            row += f" {ndcg:.4f} |"
            if ndcg > best_ndcg:
                best_ndcg = ndcg
                best_w = w.split("_")[1]
        row += f" {best_w} |"
        lines.append(row)

    return "\n".join(lines)


def save_dream_results(
    all_results: dict[str, dict[str, Any]],
    output_dir: str | Path,
) -> None:
    """Save dream experiment results to disk (JSON + markdown).

    Args:
        all_results: {dataset_name: experiment_results}
        output_dir: Directory to write output files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON results
    json_path = output_dir / "dream_results.json"
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2)
    logger.info("Raw results saved to %s", json_path)

    # Markdown tables
    md_path = output_dir / "dream_table.md"
    with open(md_path, "w") as f:
        f.write("## NCMS Dream Cycle Experiment Results\n\n")

        if len(all_results) > 1:
            f.write("### Cross-Dataset Summary (nDCG@10)\n\n")
            f.write(generate_dream_summary(all_results))
            f.write("\n\n")

        for dataset_name, results in all_results.items():
            f.write(f"### {dataset_name}\n\n")

            ingestion = results.get("ingestion", {})
            f.write(f"- **Documents**: {ingestion.get('docs_ingested', 0)}\n")
            f.write(
                f"- **Episodes created**: {ingestion.get('episodes_created', 0)}\n"
            )
            f.write(
                f"- **Ingestion time**: "
                f"{ingestion.get('ingestion_seconds', 0):.1f}s\n"
            )
            f.write(f"- **LLM model**: `{ingestion.get('llm_model', 'N/A')}`\n")
            f.write(
                f"- **Total time**: "
                f"{results.get('total_elapsed_seconds', 0):.1f}s\n"
            )
            f.write("\n")

            # Retrieval progression table
            f.write("#### Retrieval Progression\n\n")
            f.write(generate_dream_table(results))
            f.write("\n\n")

            # Diagnostics tables
            f.write("#### Structural Diagnostics\n\n")
            f.write(generate_diagnostics_structural_table(results))
            f.write("\n\n")

            f.write("#### Cognitive Diagnostics\n\n")
            f.write(generate_diagnostics_cognitive_table(results))
            f.write("\n\n")

            # ACT-R crossover table
            crossover_table = generate_actr_crossover_table(results)
            if "No" not in crossover_table:
                f.write("#### ACT-R Crossover Sweep\n\n")
                f.write(crossover_table)
                f.write("\n\n")

    logger.info("Markdown tables saved to %s", md_path)
