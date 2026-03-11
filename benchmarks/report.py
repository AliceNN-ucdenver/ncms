"""Report generation: markdown tables and matplotlib charts.

Produces publishable output from ablation study results for the README.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from benchmarks.configs import ABLATION_CONFIGS

logger = logging.getLogger(__name__)


def generate_summary_table(
    all_results: dict[str, dict[str, dict[str, float]]],
) -> str:
    """Generate a markdown summary table (nDCG@10 per config per dataset).

    Args:
        all_results: {dataset_name: {config_name: {metric: value}}}

    Returns:
        Markdown table string.
    """
    datasets = list(all_results.keys())

    # Header
    header = "| Configuration |"
    separator = "|---------------|"
    for ds in datasets:
        header += f" {ds} nDCG@10 |"
        separator += "------------|"
    header += " Average |"
    separator += "---------|"

    lines = [header, separator]

    # Rows
    for config in ABLATION_CONFIGS:
        if config.name not in next(iter(all_results.values()), {}):
            continue

        row = f"| {config.display_name} |"
        scores: list[float] = []
        for ds in datasets:
            ds_results = all_results.get(ds, {})
            config_results = ds_results.get(config.name, {})
            ndcg = config_results.get("nDCG@10", 0.0)
            scores.append(ndcg)
            row += f" {ndcg:.4f} |"

        avg = sum(scores) / len(scores) if scores else 0.0
        row += f" **{avg:.4f}** |"
        lines.append(row)

    return "\n".join(lines)


def generate_detailed_table(
    all_results: dict[str, dict[str, dict[str, float]]],
) -> str:
    """Generate detailed markdown tables with all metrics per dataset.

    Args:
        all_results: {dataset_name: {config_name: {metric: value}}}

    Returns:
        Markdown string with one table per dataset.
    """
    sections: list[str] = []

    for dataset_name, dataset_results in all_results.items():
        header = "| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 | Time (s) |"
        separator = "|---------------|---------|--------|-----------|------------|----------|"
        lines = [f"### {dataset_name}", "", header, separator]

        for config in ABLATION_CONFIGS:
            metrics = dataset_results.get(config.name, {})
            if not metrics:
                continue

            row = (
                f"| {config.display_name} "
                f"| {metrics.get('nDCG@10', 0):.4f} "
                f"| {metrics.get('MRR@10', 0):.4f} "
                f"| {metrics.get('Recall@10', 0):.4f} "
                f"| {metrics.get('Recall@100', 0):.4f} "
                f"| {metrics.get('elapsed_seconds', 0):.1f} |"
            )
            lines.append(row)

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def generate_chart(
    all_results: dict[str, dict[str, dict[str, float]]],
    output_path: str | Path,
) -> None:
    """Generate a grouped bar chart of nDCG@10 across configs and datasets.

    Includes horizontal reference lines for published baselines so results
    can be visually compared against established systems.

    Args:
        all_results: {dataset_name: {config_name: {metric: value}}}
        output_path: Path to save the PNG chart.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        logger.warning("matplotlib not available, skipping chart generation")
        return

    datasets = list(all_results.keys())
    configs = [c for c in ABLATION_CONFIGS if c.name in next(iter(all_results.values()), {})]

    if not configs or not datasets:
        logger.warning("No results to chart")
        return

    # Published baselines for reference lines (SciFact nDCG@10)
    # Only shown when SciFact is in the results
    baselines: dict[str, tuple[float, str, str]] = {
        "BM25 (published)": (0.671, "#E53935", "dashed"),
        "SPLADE v2 / ColBERT": (0.693, "#7B1FA2", "dashdot"),
        "DPR (dense)": (0.318, "#78909C", "dotted"),
        "ANCE (dense)": (0.507, "#90A4AE", "dotted"),
    }

    # Prepare data
    x = np.arange(len(configs))
    width = 0.8 / len(datasets)
    colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#F44336"]

    fig, ax = plt.subplots(figsize=(14, 7))

    for i, dataset in enumerate(datasets):
        ds_results = all_results[dataset]
        values = [ds_results.get(c.name, {}).get("nDCG@10", 0) for c in configs]
        offset = (i - len(datasets) / 2 + 0.5) * width
        bars = ax.bar(
            x + offset, values, width,
            label=dataset.capitalize(),
            color=colors[i % len(colors)],
            alpha=0.85,
        )
        # Add value labels on bars
        for bar, val in zip(bars, values, strict=True):
            if val > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.005,
                    f"{val:.3f}", ha="center", va="bottom", fontsize=7,
                )

    # Add published baseline reference lines (when SciFact is present)
    if "scifact" in datasets:
        for label, (value, color, style) in baselines.items():
            ax.axhline(
                y=value, color=color, linestyle=style, linewidth=1.2, alpha=0.7,
            )
            ax.text(
                len(configs) - 0.5, value + 0.003, label,
                fontsize=7, color=color, alpha=0.85,
                ha="right", va="bottom", style="italic",
            )

    # Zoom y-axis to the interesting range instead of 0-1
    all_values = []
    for ds in datasets:
        for c in configs:
            v = all_results[ds].get(c.name, {}).get("nDCG@10", 0)
            if v > 0:
                all_values.append(v)

    if all_values:
        y_min = max(0, min(all_values + [0.318]) - 0.05)  # Include DPR baseline
        y_max = min(1.0, max(all_values + [0.693]) + 0.05)  # Include SPLADE baseline
        ax.set_ylim(y_min, y_max)

    ax.set_xlabel("Pipeline Configuration", fontsize=12)
    ax.set_ylabel("nDCG@10", fontsize=12)
    ax.set_title("NCMS Retrieval Pipeline Ablation Study", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([c.display_name for c in configs], rotation=15, ha="right")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info("Chart saved to %s", output_path)


def save_results(
    all_results: dict[str, dict[str, dict[str, float]]],
    output_dir: str | Path,
) -> None:
    """Save all results to disk (JSON, markdown tables, chart).

    Args:
        all_results: {dataset_name: {config_name: {metric: value}}}
        output_dir: Directory to write output files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Raw JSON results
    json_path = output_dir / "ablation_results.json"
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2)
    logger.info("Raw results saved to %s", json_path)

    # Summary table
    summary = generate_summary_table(all_results)
    summary_path = output_dir / "ablation_table.md"
    with open(summary_path, "w") as f:
        f.write("## NCMS Ablation Study Results\n\n")
        f.write(summary)
        f.write("\n\n")
        f.write(generate_detailed_table(all_results))
        f.write("\n")
    logger.info("Markdown tables saved to %s", summary_path)

    # Chart — place in both results dir and docs/assets
    chart_path = output_dir / "ablation-results.png"
    generate_chart(all_results, chart_path)

    # Also copy to docs/assets if it exists
    docs_chart = output_dir.parent.parent / "docs" / "assets" / "ablation-results.png"
    if docs_chart.parent.exists():
        generate_chart(all_results, docs_chart)
