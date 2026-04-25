"""Phase G: aggregate ablation results into a side-by-side table.

Walks the Phase F baseline directory + every Phase G ablation
directory, extracts overall + per-class r@1 / mrr / r@5, and prints
a markdown table grouped by domain so the impact of each ablation
is immediately visible against the slm-off baseline and slm-on
full-default-config result.

Usage::

    uv run python scripts/v9/aggregate_phase_g_results.py
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS_ROOT = Path("benchmarks/results/mseb")
PHASE_F = RESULTS_ROOT / "v9_slm_lift_20260425T155229Z"
PHASE_G_ROOT = RESULTS_ROOT / "phase_g_ablations"

DOMAINS = [
    ("softwaredev", "main_softwaredev"),
    ("clinical", "main_clinical"),
    ("convo", "main_convo"),
]


def _load_overall(jsonl_path: Path) -> dict:
    """Read a *.results.json and return the overall metrics block."""
    data = json.loads(jsonl_path.read_text())
    return data.get("overall", data)


def _phase_f_cells() -> dict[str, dict[str, dict]]:
    """Return {domain_short: {'slm-off': metrics, 'slm-on': metrics}}."""
    out: dict[str, dict[str, dict]] = {}
    for short, dom_prefix in DOMAINS:
        out[short] = {}
        for fp in sorted(PHASE_F.glob(f"{dom_prefix}_*.results.json")):
            cfg = "slm-off" if "slm-off" in fp.name else "slm-on"
            out[short][cfg] = _load_overall(fp)
    return out


def _phase_g_cells() -> dict[str, dict[str, dict]]:
    """Return {ablation_label: {domain_short: metrics}}."""
    out: dict[str, dict[str, dict]] = {}
    if not PHASE_G_ROOT.is_dir():
        return out
    for ablation_dir in sorted(PHASE_G_ROOT.iterdir()):
        if not ablation_dir.is_dir():
            continue
        # Strip the timestamp suffix to get the canonical label.
        label = "_".join(ablation_dir.name.split("_")[:-1])
        out.setdefault(label, {})
        for short, dom_prefix in DOMAINS:
            for fp in sorted(ablation_dir.glob(f"{dom_prefix}_*.results.json")):
                out[label][short] = _load_overall(fp)
                break  # one cell per (label, domain)
    return out


def _delta(on: float | None, baseline: float | None) -> str:
    if on is None or baseline is None:
        return "—"
    d = on - baseline
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.4f}"


def main() -> None:
    f = _phase_f_cells()
    g = _phase_g_cells()

    # Sort ablation labels alphabetically so the report is stable.
    ablations = sorted(g.keys())

    print("# Phase G ablation tracking — MSEB v9 SLM signal isolation")
    print()
    print("Each row shows ONE configuration (Phase F baseline cells +")
    print("each Phase G ablation).  Δ columns are vs. slm-off (the no-")
    print("SLM baseline).  Goal: a configuration that recovers slm-off's")
    print("retrieval quality while keeping the SLM's labelling work.")
    print()

    metric = "r@1"
    print(f"## {metric}")
    print()
    header = "| config | softwaredev | Δ | clinical | Δ | convo | Δ |"
    sep = "|---|---:|---:|---:|---:|---:|---:|"
    print(header)
    print(sep)
    for cfg_label, cell_source in (
        ("slm-off (baseline)", lambda d: f.get(d, {}).get("slm-off")),
        ("slm-on full default (Phase F)", lambda d: f.get(d, {}).get("slm-on")),
    ):
        row = [cfg_label]
        for short, _ in DOMAINS:
            cell = cell_source(short)
            v = cell.get(metric) if cell else None
            row.append(f"{v:.4f}" if isinstance(v, (int, float)) else "—")
            base = (f.get(short, {}).get("slm-off") or {}).get(metric)
            row.append(_delta(v, base))
        print("| " + " | ".join(row) + " |")
    for label in ablations:
        row = [f"{label}"]
        for short, _ in DOMAINS:
            cell = g[label].get(short)
            v = cell.get(metric) if cell else None
            row.append(f"{v:.4f}" if isinstance(v, (int, float)) else "—")
            base = (f.get(short, {}).get("slm-off") or {}).get(metric)
            row.append(_delta(v, base))
        print("| " + " | ".join(row) + " |")
    print()


if __name__ == "__main__":
    main()
