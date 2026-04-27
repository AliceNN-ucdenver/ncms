"""Production factory for the dedicated CTLG cue tagger.

CTLG is intentionally a sibling adapter to the five-head content SLM.  This
module mirrors the deployment lookup convention used by ``intent_slot_chain``
while loading only the one-head BIO cue tagger.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ncms.application.adapters.methods.cue_tagger import (
    CTLGAdapterIntegrityError,
    LoraCTLGCueTagger,
    verify_ctlg_adapter_dir,
)
from ncms.domain.protocols import CTLGCueTagger

logger = logging.getLogger(__name__)


def default_ctlg_adapter_root() -> Path:
    """Return the deployed CTLG adapter root.

    The default follows the CTLG design:
    ``~/.ncms/adapters/<domain>/ctlg-vN/``.  Operators can override the
    parent root with ``NCMS_CTLG_ADAPTER_ROOT``; if unset we reuse
    ``NCMS_ADAPTER_ROOT`` and finally the standard NCMS adapter root.
    """
    override = os.environ.get("NCMS_CTLG_ADAPTER_ROOT") or os.environ.get("NCMS_ADAPTER_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ncms" / "adapters"


def find_ctlg_adapter_dir(
    domain: str,
    *,
    version: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Resolve a dedicated CTLG adapter directory for ``domain``.

    Search order:

    1. ``<root>/<domain>/<version>/`` when ``version`` is supplied.
    2. Newest modified version directory beginning with ``ctlg-``.
    3. Newest modified version directory whose manifest validates as CTLG.
    4. Flat ``<root>/<domain>/`` layout when it validates as CTLG.
    """
    root = root or default_ctlg_adapter_root()
    domain_dir = root / domain
    if not domain_dir.is_dir():
        return None

    if version is not None:
        candidate = domain_dir / version
        return candidate if _is_valid_ctlg_dir(candidate) else None

    versions = [p for p in domain_dir.iterdir() if p.is_dir()]
    ctlg_named = [p for p in versions if p.name.startswith("ctlg-")]
    for candidate in sorted(ctlg_named, key=lambda p: p.stat().st_mtime, reverse=True):
        if _is_valid_ctlg_dir(candidate):
            return candidate

    for candidate in sorted(versions, key=lambda p: p.stat().st_mtime, reverse=True):
        if _is_valid_ctlg_dir(candidate):
            return candidate

    return domain_dir if _is_valid_ctlg_dir(domain_dir) else None


def list_available_ctlg_adapters(
    *,
    root: Path | None = None,
) -> dict[str, list[str]]:
    """Enumerate deployed CTLG adapters that pass artifact validation."""
    root = root or default_ctlg_adapter_root()
    if not root.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for domain_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        versions = [
            candidate.name
            for candidate in sorted(p for p in domain_dir.iterdir() if p.is_dir())
            if _is_valid_ctlg_dir(candidate)
        ]
        if versions:
            out[domain_dir.name] = versions
        elif _is_valid_ctlg_dir(domain_dir):
            out[domain_dir.name] = ["."]
    return out


def build_default_ctlg_cue_tagger(
    *,
    domain: str,
    version: str | None = None,
    root: Path | None = None,
    device: str | None = None,
    required: bool = False,
) -> CTLGCueTagger | None:
    """Build a deployed CTLG cue tagger, or return ``None`` when absent."""
    adapter_dir = find_ctlg_adapter_dir(domain, version=version, root=root)
    if adapter_dir is None:
        msg = (
            f"no CTLG cue-tagger adapter found for domain={domain!r} "
            f"at root={root or default_ctlg_adapter_root()}"
        )
        if required:
            raise CTLGAdapterIntegrityError(msg)
        logger.info("%s; CTLG stays in abstain/shadow-only mode", msg)
        return None
    return LoraCTLGCueTagger(adapter_dir, device=device)


def _is_valid_ctlg_dir(path: Path) -> bool:
    try:
        verify_ctlg_adapter_dir(path)
    except CTLGAdapterIntegrityError:
        return False
    return True


__all__ = [
    "build_default_ctlg_cue_tagger",
    "default_ctlg_adapter_root",
    "find_ctlg_adapter_dir",
    "list_available_ctlg_adapters",
]
