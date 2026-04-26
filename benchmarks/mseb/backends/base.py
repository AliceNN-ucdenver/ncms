"""Backend protocol — what every MSEB-compatible memory system must implement.

Minimal surface area so adding a competitor is straightforward:

- ``setup()``     — load models / connect to vector store
- ``ingest(memories)`` — store a list of CorpusMemory; return ``mid → backend_id`` map
- ``search(query, limit)`` — return ranked :class:`BackendRanking` rows
- ``shutdown()`` — release resources (async if needed)

The harness does not assume an async runtime; each method may be
sync or async — the orchestrator awaits when awaitable.

Design note: backends receive the FeatureSet for optional
feature-level ablation (currently only NCMS honours it; mem0 is
binary on/off per config).  Backends that don't understand a flag
ignore it — the harness records the flag in ``results.json``
regardless so runs stay comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from benchmarks.mseb.schema import CorpusMemory


@dataclass
class BackendRanking:
    """One ranked result from a backend's search() call.

    ``mid`` is the MSEB corpus memory ID (``<domain>-<subject>-m<NN>``);
    backends recover it from whatever metadata / tags they stored at
    ingest time.  ``score`` is the backend's native similarity
    (larger = better by convention; backends that emit distance
    should invert before returning).  ``raw`` carries any
    backend-specific diagnostics the run-logs care about.
    """

    mid: str
    score: float
    raw: dict = field(default_factory=dict)


@runtime_checkable
class MemoryBackend(Protocol):
    """Protocol every MSEB backend implements."""

    name: str
    """Short identifier used in run_id / results filenames (``ncms``, ``mem0``)."""

    async def setup(self) -> None: ...

    async def ingest(
        self,
        memories: list[CorpusMemory],
    ) -> dict[str, str]:
        """Store all memories; return mapping ``corpus_mid → backend_id``."""
        ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[BackendRanking]:
        """Return the backend's top-``limit`` rankings for the query."""
        ...

    def classify_query(self, query: str) -> dict[str, object]:
        """Return per-head SLM outputs for one query string.

        Implemented by backends that ship a classifier (NcmsBackend);
        other backends (mem0) may return ``{}``.  Output format::

            {
              "admission":          str | None,
              "admission_conf":     float | None,
              "state_change":       str | None,
              "state_change_conf":  float | None,
              "topic":              str | None,
              "topic_conf":         float | None,
              "intent":             str,
              "intent_conf":        float,
              "slots":              dict[str, str],
              "shape_intent":       str | None,
              "shape_intent_conf":  float | None,
              "adapter":            str,   # e.g. "clinical/v6"
              "latency_ms":         float,
            }

        Harness populates this into ``predictions.jsonl`` so post-hoc
        forensic tooling can trace WHY a query routed the way it did.
        """
        return {}

    async def shutdown(self) -> None: ...


__all__ = ["BackendRanking", "MemoryBackend"]
