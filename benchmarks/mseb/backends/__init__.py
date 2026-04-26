"""MSEB backend registry — selects between memory systems at runtime.

The harness accepts ``--backend ncms|mem0`` and routes to one of the
concrete implementations here.  Every backend implements the
:class:`MemoryBackend` protocol so the ingest + search + grade
loop in ``harness.py`` stays identical across systems.

Adding a new competitor is one file:

1. Implement ``MemoryBackend`` in ``benchmarks/mseb/backends/foo_backend.py``.
2. Register it in ``BACKENDS`` below.
3. ``uv run python -m benchmarks.mseb.harness --backend foo ...``.
"""

from __future__ import annotations

from benchmarks.mseb.backends.base import BackendRanking, MemoryBackend

# Lazy registry — heavy imports (mem0, ncms) land only when selected.


def _make_ncms_backend(**kwargs) -> MemoryBackend:
    from benchmarks.mseb.backends.ncms_backend import NcmsBackend

    return NcmsBackend(**kwargs)


def _make_mem0_backend(**kwargs) -> MemoryBackend:
    from benchmarks.mseb.backends.mem0_backend import Mem0Backend

    return Mem0Backend(**kwargs)


BACKENDS: dict[str, object] = {
    "ncms": _make_ncms_backend,
    "mem0": _make_mem0_backend,
}


def make_backend(name: str, **kwargs) -> MemoryBackend:
    """Construct a backend by name.  Raises for unknown backends."""
    if name not in BACKENDS:
        raise ValueError(
            f"unknown backend {name!r}; valid: {sorted(BACKENDS)}",
        )
    return BACKENDS[name](**kwargs)  # type: ignore[misc]


__all__ = ["BACKENDS", "BackendRanking", "MemoryBackend", "make_backend"]
