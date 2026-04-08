"""Dream cycle / consolidation experiment configuration.

Defines the stages to evaluate. Each stage runs a consolidation sub-phase
and measures retrieval quality against the same query set.

Unlike the retrieval ablation (which swaps scoring weights on a fixed index),
the dream experiment measures how LLM-generated abstract memories (episode
summaries, state trajectories, recurring patterns) affect retrieval quality.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DreamStage:
    """A consolidation stage to execute and measure."""

    name: str
    display_name: str
    # Which consolidation sub-phases to run at this stage
    episode_consolidation: bool = False    # Phase 5A
    trajectory_consolidation: bool = False  # Phase 5B
    pattern_consolidation: bool = False     # Phase 5C
    dream_cycle: bool = False              # Phase 8
    cycles: int = 1  # How many times to run consolidation at this stage


# Stages execute in order.  Each builds on previous results (cumulative).
# Stage 0 (baseline) just measures retrieval before any consolidation.
DREAM_STAGES: tuple[DreamStage, ...] = (
    DreamStage(
        name="baseline",
        display_name="Baseline (Phases 1-3)",
    ),
    DreamStage(
        name="episode_summaries",
        display_name="+ Episode Summaries (5A)",
        episode_consolidation=True,
    ),
    DreamStage(
        name="trajectories",
        display_name="+ State Trajectories (5B)",
        trajectory_consolidation=True,
    ),
    DreamStage(
        name="patterns",
        display_name="+ Pattern Detection (5C)",
        pattern_consolidation=True,
    ),
    DreamStage(
        name="dream_1x",
        display_name="+ Dream Cycle (1\u00d7)",
        dream_cycle=True,
    ),
    DreamStage(
        name="dream_3x",
        display_name="+ Dream Cycle (3\u00d7)",
        dream_cycle=True,
        cycles=3,
    ),
)
