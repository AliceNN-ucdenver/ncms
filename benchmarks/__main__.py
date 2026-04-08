"""Unified benchmark CLI dispatcher.

Usage:
    python -m benchmarks beir           # BEIR retrieval ablation
    python -m benchmarks dream          # Dream cycle consolidation
    python -m benchmarks swebench       # SWE-bench memory competencies
    python -m benchmarks locomo         # LoCoMo conversational reasoning
    python -m benchmarks longmemeval    # LongMemEval conversation memory
    python -m benchmarks mab            # MemoryAgentBench 4-competency
    python -m benchmarks hub            # Hub workload replay
    python -m benchmarks smoke          # Quick validation smoke test
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        print("\nAvailable suites: beir, dream, swebench, locomo, longmemeval, mab, hub, smoke")
        sys.exit(1)

    suite = sys.argv[1]
    # Remove the suite name so sub-runners see clean argv
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if suite == "beir":
        from benchmarks.beir.run_ablation import main as run
    elif suite == "dream":
        from benchmarks.dream.run_dream import main as run
    elif suite == "swebench":
        from benchmarks.swebench.run_swebench import main as run
    elif suite == "locomo":
        from benchmarks.locomo.run_locomo import main as run
    elif suite == "longmemeval":
        from benchmarks.longmemeval.run_longmemeval import main as run
    elif suite == "mab":
        from benchmarks.memoryagentbench.run_mab import main as run
    elif suite == "hub":
        from benchmarks.hub_replay.run_hub_replay import main as run
    elif suite == "smoke":
        from benchmarks.smoke_test import main as run
    else:
        print(f"Unknown suite: {suite}")
        print("Available: beir, dream, swebench, locomo, longmemeval, mab, hub, smoke")
        sys.exit(1)

    run()


if __name__ == "__main__":
    main()
