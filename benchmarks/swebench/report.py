"""SWE-bench experiment report generation.

Generates markdown tables and JSON output for the multi-split
dream cycle experiment results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_progression_table(results: dict[str, Any]) -> str:
    """Generate multi-split progression table (all 4 competencies per stage)."""
    lines = [
        "#### Multi-Split Retrieval Progression\n",
        "| Stage | AR nDCG@10 | AR Δ% | TTL Acc | CR tMRR | LRU nDCG@10 | Time |",
        "|-------|-----------|-------|---------|---------|-------------|------|",
    ]

    baseline_ar = None
    for _stage_name, stage in results.get("stages", {}).items():
        ar_ndcg = stage["ar"]["nDCG@10"]
        ttl_acc = stage["ttl"]["accuracy"]
        cr_mrr = stage["cr"]["temporal_mrr"]
        lru_ndcg = stage["lru"]["nDCG@10"]
        elapsed = stage["elapsed_seconds"]

        if baseline_ar is None:
            baseline_ar = ar_ndcg
            delta = "—"
        else:
            delta_pct = (ar_ndcg - baseline_ar) / baseline_ar * 100 if baseline_ar > 0 else 0
            delta = f"+{delta_pct:.2f}%" if delta_pct >= 0 else f"{delta_pct:.2f}%"

        lines.append(
            f"| {stage['display_name']} | {ar_ndcg:.4f} | {delta} | "
            f"{ttl_acc:.4f} | {cr_mrr:.4f} | {lru_ndcg:.4f} | {elapsed:.0f}s |"
        )

    return "\n".join(lines)


def generate_graph_table(results: dict[str, Any]) -> str:
    """Generate graph connectivity progression table."""
    lines = [
        "#### Graph Connectivity Progression\n",
        "| Stage | Entities | Edges | Density | Components | Largest | Degree mean | PR max |",
        "|-------|----------|-------|---------|------------|---------|-------------|--------|",
    ]

    for stage in results.get("stages", {}).values():
        g = stage.get("graph_diagnostics", {})
        lines.append(
            f"| {stage['display_name']} | {g.get('entity_count', 0):,} | "
            f"{g.get('edge_count', 0):,} | {g.get('density', 0):.4f} | "
            f"{g.get('components', 0):,} | {g.get('largest_component', 0):,} | "
            f"{g.get('degree_mean', 0):.2f} | {g.get('pagerank_max', 0):.6f} |"
        )

    return "\n".join(lines)


def generate_actr_table(results: dict[str, Any]) -> str:
    """Generate ACT-R crossover sweep table."""
    lines = [
        "#### ACT-R Crossover Sweep (AR Split)\n",
        "| Stage | actr_0.0 | actr_0.1 | actr_0.2 | actr_0.3 | actr_0.4 | Best |",
        "|-------|---------|---------|---------|---------|---------|------|",
    ]

    for stage in results.get("stages", {}).values():
        actr = stage.get("actr_crossover", {})
        if not actr:
            continue

        best_weight = 0.0
        best_ndcg = 0.0
        values = []
        for w in [0.0, 0.1, 0.2, 0.3, 0.4]:
            key = f"actr_{w}"
            ndcg = actr.get(key, {}).get("nDCG@10", 0)
            values.append(f"{ndcg:.4f}")
            if ndcg > best_ndcg:
                best_ndcg = ndcg
                best_weight = w

        lines.append(f"| {stage['display_name']} | {' | '.join(values)} | {best_weight} |")

    return "\n".join(lines)


def generate_comparison_table(results: dict[str, Any]) -> str:
    """Generate SciFact vs SWE-bench comparison table."""
    # SciFact observed values
    scifact = {
        "entities": 51357,
        "edges": 0,
        "components": 51357,
        "density": 0.0,
        "actr_best": 0.0,
        "dream_delta": "+0.04%",
    }

    # Get SWE-bench final stage graph diagnostics
    stages = results.get("stages", {})
    last_stage = list(stages.values())[-1] if stages else {}
    g = last_stage.get("graph_diagnostics", {})

    # Get baseline AR
    baseline = list(stages.values())[0] if stages else {}
    baseline_ar = baseline.get("ar", {}).get("nDCG@10", 0)
    final_ar = last_stage.get("ar", {}).get("nDCG@10", 0)
    delta = f"+{(final_ar - baseline_ar) / baseline_ar * 100:.2f}%" if baseline_ar > 0 else "N/A"

    lines = [
        "#### SciFact vs SWE-bench Django Comparison\n",
        "| Metric | SciFact (BEIR) | SWE-bench Django |",
        "|--------|---------------|------------------|",
        f"| Entities | {scifact['entities']:,} | {g.get('entity_count', '?'):,} |",
        f"| Edges | {scifact['edges']} | {g.get('edge_count', '?'):,} |",
        f"| Components | {scifact['components']:,} | {g.get('components', '?'):,} |",
        f"| Density | {scifact['density']:.4f} | {g.get('density', '?')} |",
        f"| Dream cycle AR Δ | {scifact['dream_delta']} | {delta} |",
    ]

    return "\n".join(lines)


def generate_full_report(results: dict[str, Any]) -> str:
    """Generate the complete markdown report."""
    ing = results.get("ingestion", {})
    lines = [
        "## NCMS SWE-bench Dream Cycle Experiment Results\n",
        f"- **Documents**: {ing.get('docs_ingested', 0)}",
        f"- **Episodes created**: {ing.get('episodes_created', 0)}",
        f"- **Ingestion time**: {ing.get('ingestion_seconds', 0):.1f}s",
        f"- **LLM model**: `{ing.get('llm_model', 'N/A')}`",
        f"- **Total time**: {results.get('total_seconds', 0):.1f}s\n",
    ]

    lines.append(generate_progression_table(results))
    lines.append("")
    lines.append(generate_graph_table(results))
    lines.append("")
    lines.append(generate_actr_table(results))
    lines.append("")
    lines.append(generate_comparison_table(results))

    return "\n".join(lines)


def save_swebench_results(
    results: dict[str, Any],
    output_dir: str | Path,
) -> None:
    """Save results as JSON + markdown to output directory."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = out / "swebench_results.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("JSON results: %s", json_path)

    # Markdown
    md_path = out / "swebench_table.md"
    md_content = generate_full_report(results)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown report: %s", md_path)
