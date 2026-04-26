"""Benchmark harness helper: per-domain adapter selection.

Phase I.1 — this module is now a thin delegator around the
production factory at
:mod:`ncms.application.intent_slot_chain`.  Existing benchmark
imports keep working unchanged; the actual path-resolution +
chain-building logic lives at the right architectural layer
(application, not benchmarks) so production code paths
(CLI / MCP server / dashboard / NemoClaw hub) can reach for the
same factory.

Benchmark calling convention is unchanged::

    from benchmarks.intent_slot_adapter import get_intent_slot_chain

    chain = get_intent_slot_chain(domain="conversational")
    svc = MemoryService(
        store=store, index=index, graph=graph, config=cfg,
        intent_slot=chain,
    )

The only intentional behavioral difference between this benchmark
helper and :func:`ncms.application.intent_slot_chain.
build_default_intent_slot_chain` is the default for
``include_e5_fallback`` — benchmarks default ``False`` for
deterministic per-cell behaviour, production defaults ``True``
so cold-start unknown-domain requests still get an intent label.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ncms.application.intent_slot_chain import (
    _default_adapter_root,
    find_adapter_dir,
    list_available_adapters,
)
from ncms.domain.protocols import IntentSlotExtractor
from ncms.infrastructure.extraction.intent_slot import (
    build_extractor_chain,
)

logger = logging.getLogger(__name__)


def get_intent_slot_chain(
    *,
    domain: str,
    version: str | None = None,
    root: Path | None = None,
    confidence_threshold: float = 0.7,
    include_e5_fallback: bool = False,
    device: str | None = None,
) -> IntentSlotExtractor:
    """Build a domain-specific extractor chain for a benchmark run.

    ``include_e5_fallback`` defaults to ``False`` because benchmarks
    typically want deterministic behaviour — adding the zero-shot
    fallback makes results depend on E5 model initialization.
    Flip to ``True`` when benchmarking cold-start behaviour.

    When no adapter is found for ``domain``, returns a heuristic-
    only chain with a loud warning — benchmarks should treat that
    as "no classifier active" rather than crash.
    """
    adapter_dir = find_adapter_dir(domain, version=version, root=root)
    if adapter_dir is None:
        logger.warning(
            "[bench] no adapter found for domain=%s at root=%s; "
            "falling back to heuristic-only chain",
            domain,
            root or _default_adapter_root(),
        )

    return build_extractor_chain(
        checkpoint_dir=adapter_dir,
        confidence_threshold=confidence_threshold,
        include_e5_fallback=include_e5_fallback,
        device=device,
    )


__all__ = [
    "find_adapter_dir",
    "get_intent_slot_chain",
    "list_available_adapters",
]
