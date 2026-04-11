#!/usr/bin/env bash
# ============================================================================
# Phase Comparison — diff benchmark results between two phases
# ============================================================================
# Compares JSON results from two phase directories and prints a summary
# showing regressions (↓) and improvements (↑) for key metrics.
#
# Usage:
#   ./benchmarks/compare_phases.sh phase0_baseline phase4
#   ./benchmarks/compare_phases.sh phase0_baseline phase4 --json
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

UV="${UV:-/Users/shawnmccarthy/.local/bin/uv}"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <baseline-phase> <current-phase> [--json]"
    echo ""
    echo "Example: $0 phase0_baseline phase4"
    exit 1
fi

BASELINE="$1"
CURRENT="$2"
JSON_OUTPUT=false
if [ "${3:-}" = "--json" ]; then
    JSON_OUTPUT=true
fi

BASE_DIR="benchmarks/results/$BASELINE"
CURR_DIR="benchmarks/results/$CURRENT"

if [ ! -d "$BASE_DIR" ]; then
    echo "ERROR: Baseline directory not found: $BASE_DIR"
    exit 1
fi
if [ ! -d "$CURR_DIR" ]; then
    echo "ERROR: Current directory not found: $CURR_DIR"
    exit 1
fi

# ── Python comparison script (inline) ──────────────────────────────────────

$UV run python3 - "$BASE_DIR" "$CURR_DIR" "$JSON_OUTPUT" << 'PYEOF'
import json
import sys
from pathlib import Path

base_dir = Path(sys.argv[1])
curr_dir = Path(sys.argv[2])
json_output = sys.argv[3] == "True"

# Metric extraction rules per benchmark type
# Each rule: (json_file_pattern, metric_path, display_name, higher_is_better)
METRICS = [
    # Hub replay
    ("hub_replay_latest.json", ["duplicate_count"], "Hub: duplicates", False),
    ("hub_replay_latest.json", ["total_entities"], "Hub: entities", True),
    ("hub_replay_latest.json", ["junk_entity_rate"], "Hub: junk entity rate", False),
    ("hub_replay_latest.json", ["ingest_latency_p50"], "Hub: ingest p50 (ms)", False),
    ("hub_replay_latest.json", ["ingest_latency_p95"], "Hub: ingest p95 (ms)", False),
    ("hub_replay_latest.json", ["search_latency_p50"], "Hub: search p50 (ms)", False),
    # BEIR SciFact (best config = bm25_splade_graph)
    ("ablation_results.json", ["scifact", "bm25_splade_graph", "nDCG@10"], "BEIR SciFact: nDCG@10", True),
    ("ablation_results.json", ["scifact", "bm25_splade_graph", "Recall@10"], "BEIR SciFact: Recall@10", True),
    ("ablation_results.json", ["scifact", "bm25_splade_graph", "MRR@10"], "BEIR SciFact: MRR@10", True),
    # LoCoMo
    ("locomo_latest.json", ["overall", "Recall@5"], "LoCoMo: Recall@5", True),
    ("locomo_latest.json", ["overall", "Contains"], "LoCoMo: Contains", True),
    ("locomo_latest.json", ["overall", "F1"], "LoCoMo: F1", True),
    # LoCoMo-Plus
    ("locomo_plus_latest.json", ["overall", "Recall@5"], "LoCoMo+: Recall@5", True),
    ("locomo_plus_latest.json", ["overall", "Contains"], "LoCoMo+: Contains", True),
    # LongMemEval
    ("longmemeval_latest.json", ["overall", "Recall@5"], "LongMemEval: Recall@5", True),
    ("longmemeval_latest.json", ["overall", "Contains"], "LongMemEval: Contains", True),
]


def extract(data: dict, path: list[str]):
    """Walk a nested dict by key path, return None if missing."""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def load_json(directory: Path, pattern: str) -> dict | None:
    f = directory / pattern
    if f.exists():
        return json.loads(f.read_text())
    return None


results = []
for file_pattern, path, name, higher_better in METRICS:
    base_data = load_json(base_dir, file_pattern)
    curr_data = load_json(curr_dir, file_pattern)

    if base_data is None or curr_data is None:
        results.append({"metric": name, "baseline": None, "current": None,
                        "delta": None, "status": "skip"})
        continue

    base_val = extract(base_data, path)
    curr_val = extract(curr_data, path)

    if base_val is None or curr_val is None:
        results.append({"metric": name, "baseline": base_val, "current": curr_val,
                        "delta": None, "status": "skip"})
        continue

    delta = curr_val - base_val
    if base_val != 0:
        pct = (delta / abs(base_val)) * 100
    else:
        pct = 0.0

    # Determine status
    if abs(pct) < 1.0:
        status = "same"
    elif (higher_better and delta > 0) or (not higher_better and delta < 0):
        status = "improved"
    else:
        status = "regressed"

    results.append({
        "metric": name,
        "baseline": round(base_val, 4),
        "current": round(curr_val, 4),
        "delta": round(delta, 4),
        "pct": round(pct, 1),
        "status": status,
    })

if json_output:
    print(json.dumps(results, indent=2))
    sys.exit(0)

# ── Pretty print ───────────────────────────────────────────────────────────

print()
print(f"  Phase Comparison: {base_dir.name} → {curr_dir.name}")
print(f"  {'─' * 72}")
print(f"  {'Metric':<35} {'Baseline':>10} {'Current':>10} {'Delta':>12} {'':>4}")
print(f"  {'─' * 72}")

regressions = 0
improvements = 0

for r in results:
    if r["status"] == "skip":
        print(f"  {r['metric']:<35} {'—':>10} {'—':>10} {'(missing)':>12}")
        continue

    base_str = f"{r['baseline']:.4f}" if isinstance(r['baseline'], float) else str(r['baseline'])
    curr_str = f"{r['current']:.4f}" if isinstance(r['current'], float) else str(r['current'])

    if r["status"] == "improved":
        arrow = "↑"
        improvements += 1
    elif r["status"] == "regressed":
        arrow = "↓ !!!"
        regressions += 1
    else:
        arrow = "="

    delta_str = f"{r['pct']:+.1f}%"
    print(f"  {r['metric']:<35} {base_str:>10} {curr_str:>10} {delta_str:>12} {arrow:>4}")

print(f"  {'─' * 72}")
print(f"  Improvements: {improvements}  |  Regressions: {regressions}")
if regressions > 0:
    print(f"  ⚠️  {regressions} regression(s) detected — review before merging")
print()

sys.exit(1 if regressions > 0 else 0)
PYEOF
