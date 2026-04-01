#!/usr/bin/env bash
# Run the structured prompt experiment for all agents and topics.
#
# Usage:
#   ./experiments/structured-prompts/run_experiment.sh                    # All agents, default topic
#   ./experiments/structured-prompts/run_experiment.sh --topic "Rate limiting"  # Custom topic
#   ./experiments/structured-prompts/run_experiment.sh --agent researcher       # Single agent
#   ./experiments/structured-prompts/run_experiment.sh --judge                  # Judge latest pair
#
# Environment:
#   LLM_MODEL        — override LLM (default: Nemotron Nano on Spark)
#   LLM_API_BASE     — override API base (default: spark-ee7d.local:8000)
#   JUDGE_MODEL      — override judge model (default: same as LLM_MODEL)
#   NCMS_HUB_URL     — override hub URL (default: localhost:9080)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Defaults
TOPIC="Authentication patterns for identity services"
AGENT=""
JUDGE_ONLY=false
HUB_URL="${NCMS_HUB_URL:-http://localhost:9080}"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --topic)    TOPIC="$2"; shift 2 ;;
    --agent)    AGENT="$2"; shift 2 ;;
    --judge)    JUDGE_ONLY=true; shift ;;
    --hub-url)  HUB_URL="$2"; shift 2 ;;
    --help|-h)
      sed -n '2,/^$/s/^# //p' "$0"
      exit 0 ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

cd "$PROJECT_ROOT"

# Load .env
if [ -f .env ]; then
  set -a; source .env; set +a
fi

if $JUDGE_ONLY; then
  echo "=== Judging latest experiment pair ==="
  uv run python experiments/structured-prompts/judge.py --latest
  exit 0
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Structured Prompt Experiment                            ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Topic:    $TOPIC"
echo "  Hub:      $HUB_URL"
echo "  LLM:      ${LLM_MODEL:-Nemotron Nano (default)}"
echo ""

# Check hub health
if ! curl -sf "$HUB_URL/api/v1/health" > /dev/null 2>&1; then
  echo "ERROR: Hub not reachable at $HUB_URL"
  exit 1
fi
echo "  ✓ Hub healthy"
echo ""

# Run experiments
AGENTS=("researcher" "prd")
if [ -n "$AGENT" ]; then
  AGENTS=("$AGENT")
fi

for agent in "${AGENTS[@]}"; do
  echo "━━━ Running $agent experiment ━━━"
  uv run python experiments/structured-prompts/harness.py \
    --topic "$TOPIC" \
    --agent "$agent" \
    --hub-url "$HUB_URL"
  echo ""
done

echo "━━━ Experiments complete ━━━"
echo ""
echo "Results in: experiments/structured-prompts/results/"
echo ""
echo "To judge the results:"
echo "  ./experiments/structured-prompts/run_experiment.sh --judge"
echo ""
echo "Or evaluate a specific pair:"
echo "  uv run python experiments/structured-prompts/judge.py \\"
echo "    --standard results/<file>_standard.md \\"
echo "    --semiformal results/<file>_semiformal.md"
