#!/usr/bin/env bash
# SWE-bench dream cycle experiment runner.
#
# Usage:
#   ./benchmarks/run_swebench.sh                      # Full experiment (DGX Spark)
#   ./benchmarks/run_swebench.sh --analysis-only       # Analysis only (no LLM)
#   LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_swebench.sh
#
# Monitor: tail -f benchmarks/results/swebench/swebench_latest.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Load .env if present (HF_TOKEN, etc.)
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
    echo "  ✓ .env loaded (HF_TOKEN=${HF_TOKEN:+set}${HF_TOKEN:-unset})"
else
    echo "  ⚠ No .env file found"
fi

# Defaults (overridable via env vars)
LLM_MODEL="${LLM_MODEL:-openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16}"
LLM_API_BASE="${LLM_API_BASE:-http://spark-ee7d.local:8000/v1}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results/swebench}"
VERBOSE="${VERBOSE:-}"

echo "============================================================"
echo "NCMS SWE-bench Dream Cycle Experiment"
echo "============================================================"
echo "  LLM model : $LLM_MODEL"
echo "  API base  : $LLM_API_BASE"
echo "  Output    : $OUTPUT_DIR"
echo "  Git SHA   : $(git rev-parse --short HEAD 2>/dev/null || echo 'N/A')"
echo "  Date      : $(date)"
echo "============================================================"

# Pre-flight checks
echo ""
echo "Pre-flight checks..."

# Check uv
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo "  ✓ uv found"

# Check Python imports
if ! uv run python -c "from benchmarks.swebench_loader import load_swebench_django" 2>/dev/null; then
    echo "ERROR: benchmark imports failed. Run: uv sync --group bench"
    exit 1
fi
echo "  ✓ Python imports OK"

# Check LLM connectivity (skip for analysis-only)
if [[ "${1:-}" != "--analysis-only" ]]; then
    if [[ -n "$LLM_API_BASE" ]]; then
        if ! curl -s --connect-timeout 10 --max-time 30 "$LLM_API_BASE/models" >/dev/null 2>&1; then
            echo "WARNING: Cannot reach LLM at $LLM_API_BASE"
            echo "  Consolidation stages may fail. Continue? (Ctrl+C to abort)"
            sleep 3
        else
            echo "  ✓ LLM endpoint reachable"
        fi
    fi
fi

echo ""
echo "Starting experiment..."
echo "Monitor: tail -f $OUTPUT_DIR/swebench_latest.log"
echo ""

# Build command
CMD="uv run python -m benchmarks.run_swebench"
CMD="$CMD --llm-model $LLM_MODEL"
CMD="$CMD --llm-api-base $LLM_API_BASE"
CMD="$CMD --output-dir $OUTPUT_DIR"

if [[ -n "$VERBOSE" ]]; then
    CMD="$CMD --verbose"
fi

# Pass through extra args (like --analysis-only)
if [[ $# -gt 0 ]]; then
    CMD="$CMD $*"
fi

eval $CMD

echo ""
echo "============================================================"
echo "Experiment complete. Results: $OUTPUT_DIR/"
echo "============================================================"
