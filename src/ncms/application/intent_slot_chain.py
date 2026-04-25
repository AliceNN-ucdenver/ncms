"""Production factory for the per-domain intent-slot chain.

Promotes the benchmark-side helper at ``benchmarks/intent_slot_adapter
.py`` into the application layer so production code paths
(CLI / MCP server / dashboard / NemoClaw hub) can construct an
:class:`IntentSlotExtractor` without importing from the benchmarks
package.  Same path-resolution + same fallback chain; just lives at
the right layer.

Designed to be the single entry point production callers reach for
when they need an SLM-enabled :class:`MemoryService`::

    from ncms.application.intent_slot_chain import (
        build_default_intent_slot_chain,
    )

    chain = build_default_intent_slot_chain(
        domain="conversational",
        confidence_threshold=cfg.slm_confidence_threshold,
    )
    svc = MemoryService(
        store=store, index=index, graph=graph, config=cfg,
        intent_slot=chain,
    )

The architectural question of WHICH adapter to load — one-per-
deployment vs per-memory routing vs a multi-domain model — is left
to the caller.  This factory implements the simplest answer
(one adapter per call) and exposes ``find_adapter_dir`` /
``list_available_adapters`` for callers that want to enumerate
what's deployed and pick.

Phase I.1 — see ``docs/v9-mseb-slm-lift-findings.md`` and the
roadmap in ``docs/completed/p2-plan.md``.  The benchmark helper
remains in place as a thin delegator so existing benchmark
imports keep working unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ncms.domain.protocols import IntentSlotExtractor
from ncms.infrastructure.extraction.intent_slot import (
    build_extractor_chain,
)

if TYPE_CHECKING:
    from ncms.config import NCMSConfig

logger = logging.getLogger(__name__)


def _default_adapter_root() -> Path:
    """User-level adapter directory — matches the NCMS convention.

    Override via ``NCMS_ADAPTER_ROOT`` (set by deployment configs +
    Docker images that bake adapters into the container image).
    """
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
    2. Newest-modified versioned subdirectory under
       ``<root>/<domain>/`` (e.g. ``v9`` > ``v8`` if both exist).
    3. ``<root>/<domain>/`` itself when the domain dir contains
       a flat layout (manifest.json directly).
    4. ``None`` when nothing matches — caller decides whether to
       fall back to a heuristic-only chain or raise.
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

    versions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return versions[0]


def list_available_adapters(
    *, root: Path | None = None,
) -> dict[str, list[str]]:
    """Enumerate adapters on disk.

    Returns ``{domain: [version, ...]}`` for every domain directory
    under ``root`` that has at least one versioned subdirectory.
    Used by ``ncms adapters list`` CLI + production startup logging.
    """
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


def build_default_intent_slot_chain(
    *,
    domain: str,
    version: str | None = None,
    root: Path | None = None,
    confidence_threshold: float = 0.3,
    include_e5_fallback: bool = True,
    device: str | None = None,
) -> IntentSlotExtractor:
    """Build the production intent-slot extractor chain.

    Default configuration mirrors the production preferences (vs
    the benchmark variant which uses ``include_e5_fallback=False``
    for determinism):

      * ``confidence_threshold=0.3`` matches the v9-default
        :class:`~ncms.config.NCMSConfig.slm_confidence_threshold`.
      * ``include_e5_fallback=True`` — production wants the cold-
        start zero-shot fallback for unknown domains; benchmarks
        disable it for deterministic per-cell behaviour.

    When no adapter is found for ``domain``, returns a
    heuristic-only chain with a loud warning.  Production callers
    should treat that as "no classifier active" rather than crash —
    every memory will still ingest cleanly via the heuristic
    fallback (admission=persist, all SLM heads → ``None``).

    Args:
        domain: The deployment's primary content domain
            (``conversational`` / ``software_dev`` / ``clinical``
            today; arbitrary custom names supported).
        version: Pin a specific adapter version; ``None`` picks
            the newest-modified.
        root: Adapter root directory; defaults to
            ``~/.ncms/adapters/`` (or ``$NCMS_ADAPTER_ROOT``).
        confidence_threshold: Per-head confidence floor for the
            chain — below this, the chain falls through to the
            next backend's output for that head.
        include_e5_fallback: Insert the E5 zero-shot extractor in
            the chain.  Production default ``True``; benchmark
            default ``False``.
        device: Torch device override (``"cpu"`` / ``"cuda"`` /
            ``"mps"``); ``None`` lets the chain auto-detect.

    Returns:
        A :class:`ChainedExtractor` ready to pass to
        :class:`MemoryService(intent_slot=...)`.
    """
    adapter_dir = find_adapter_dir(domain, version=version, root=root)
    if adapter_dir is None:
        logger.warning(
            "no intent-slot adapter found for domain=%s at root=%s; "
            "falling back to heuristic-only chain (memories will still "
            "ingest, but admission/state_change/topic/intent/slot heads "
            "will return None)",
            domain, root or _default_adapter_root(),
        )

    return build_extractor_chain(
        checkpoint_dir=adapter_dir,
        confidence_threshold=confidence_threshold,
        include_e5_fallback=include_e5_fallback,
        device=device,
    )


def maybe_build_chain_for_config(
    config: NCMSConfig,
) -> IntentSlotExtractor | None:
    """Build the SLM chain from an :class:`NCMSConfig`, or return ``None``.

    The single entry-point production constructors (CLI / MCP /
    dashboard / NemoClaw hub) use to plumb the SLM through to
    :class:`MemoryService`.  Returns ``None`` when the SLM is
    deliberately off (``default_adapter_domain=None``) — in which
    case :class:`IngestionPipeline` short-circuits the SLM
    extraction and falls through to the heuristic chain.

    The legacy ``slm_enabled`` boolean flag was retired -- the
    chain's presence (or absence) is now the kill-switch: setting
    ``default_adapter_domain=None`` disables the SLM end-to-end.

    Reads from the config:
      * ``default_adapter_domain`` — single-tenant adapter
        selector.  ``None`` → return ``None`` with a one-time
        info log explaining the operator opt-out.
      * ``slm_confidence_threshold`` — passed through.
      * ``slm_e5_fallback_enabled`` — passed through.
      * ``slm_checkpoint_dir`` — when set, pinned via
        ``find_adapter_dir`` lookup; otherwise the newest
        version under ``~/.ncms/adapters/<domain>/`` wins.

    Production callers::

        chain = maybe_build_chain_for_config(config)
        svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
            intent_slot=chain,  # None or IntentSlotExtractor
        )
    """
    if not config.default_adapter_domain:
        logger.info(
            "default_adapter_domain unset — SLM stays dark, ingest "
            "uses heuristic chain.  Set NCMS_DEFAULT_ADAPTER_DOMAIN "
            "to a deployed adapter (e.g. 'conversational') to "
            "activate.",
        )
        return None

    # Pinned checkpoint dir overrides domain-based discovery when
    # set — useful for staging/canary deployments that want a
    # specific adapter version.
    root: Path | None = None
    version: str | None = None
    checkpoint_override = getattr(config, "slm_checkpoint_dir", None)
    if checkpoint_override:
        root = Path(checkpoint_override).expanduser().parent.parent
        version = Path(checkpoint_override).name

    return build_default_intent_slot_chain(
        domain=config.default_adapter_domain,
        version=version,
        root=root,
        confidence_threshold=config.slm_confidence_threshold,
        include_e5_fallback=config.slm_e5_fallback_enabled,
    )


__all__ = [
    "build_default_intent_slot_chain",
    "find_adapter_dir",
    "list_available_adapters",
    "maybe_build_chain_for_config",
]
