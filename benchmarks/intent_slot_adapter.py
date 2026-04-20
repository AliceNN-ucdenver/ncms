"""Benchmark harness helper: per-domain adapter selection.

Given a benchmark domain string (``"conversational"``,
``"software_dev"``, ``"clinical"``, or an arbitrary custom name),
returns an :class:`IntentSlotExtractor` loaded from the matching
adapter at ``~/.ncms/adapters/<domain>/<version>/`` (falling back
to whatever's available).  Benchmarks call this once per run and
hand the returned chain to ``MemoryService(intent_slot=...)``.

This keeps benchmark code simple::

    from benchmarks.intent_slot_adapter import get_intent_slot_chain

    chain = get_intent_slot_chain(domain="conversational")
    svc = MemoryService(
        store=store, index=index, graph=graph, config=cfg,
        intent_slot=chain,
    )

The helper is deliberately separate from
``ncms.infrastructure.extraction.intent_slot.build_extractor_chain``
so benchmarks can express "pick the adapter for this domain"
without re-implementing path-resolution logic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ncms.domain.protocols import IntentSlotExtractor
from ncms.infrastructure.extraction.intent_slot import (
    build_extractor_chain,
)

logger = logging.getLogger(__name__)


def _default_adapter_root() -> Path:
    """User-level adapter directory — matches the NCMS convention."""
    override = os.environ.get("NCMS_ADAPTER_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ncms" / "adapters"


def find_adapter_dir(
    domain: str,
    *,
    version: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Resolve an adapter path for ``domain``.

    Search order:

    1. ``<root>/<domain>/<version>/`` if ``version`` is given.
    2. Newest versioned subdirectory under ``<root>/<domain>/``.
    3. ``<root>/<domain>/`` itself (for flat layouts).
    4. ``None`` when nothing matches.

    Callers can then pass the path to
    :func:`ncms.infrastructure.extraction.intent_slot.
    build_extractor_chain`.
    """
    root = root or _default_adapter_root()
    domain_dir = root / domain
    if not domain_dir.is_dir():
        return None

    if version is not None:
        candidate = domain_dir / version
        return candidate if candidate.is_dir() else None

    versions = [p for p in domain_dir.iterdir() if p.is_dir()]
    if not versions:
        # Flat layout — check for the artifact files directly under domain_dir.
        if (domain_dir / "manifest.json").is_file():
            return domain_dir
        return None

    # Pick the newest-modified version directory (usually ``v4`` > ``v3``…).
    versions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return versions[0]


def list_available_adapters(
    *, root: Path | None = None,
) -> dict[str, list[str]]:
    """Enumerate adapters on disk — for benchmark logging."""
    root = root or _default_adapter_root()
    if not root.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for domain_dir in sorted(root.iterdir()):
        if not domain_dir.is_dir():
            continue
        versions = sorted(
            p.name for p in domain_dir.iterdir() if p.is_dir()
        )
        if versions:
            out[domain_dir.name] = versions
    return out


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
    Flip to True when benchmarking cold-start behaviour.

    When no adapter is found for ``domain``, returns a
    heuristic-only chain with a loud warning — benchmarks should
    treat that as "no classifier active" rather than crash.
    """
    adapter_dir = find_adapter_dir(domain, version=version, root=root)
    if adapter_dir is None:
        logger.warning(
            "[bench] no adapter found for domain=%s at root=%s; "
            "falling back to heuristic-only chain",
            domain, root or _default_adapter_root(),
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
