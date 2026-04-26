"""Hardware device resolution — GPU / MPS / CPU.

Single source of truth for ``which device should this model load
on``.  Every place in NCMS that touches a PyTorch / sentence-
transformers / HuggingFace model picks its device here instead of
duplicating a ``cuda.is_available()`` check.

Priority (default order):

1. CUDA (NVIDIA GPU) — fastest everywhere it's available
2. MPS (Apple Metal Performance Shaders) — Apple Silicon
3. CPU — always available

Overrides via environment variable (per-component):

* ``NCMS_GLINER_DEVICE``
* ``NCMS_SPLADE_DEVICE``
* ``NCMS_RERANKER_DEVICE``

or the global ``NCMS_DEVICE`` which every component honours when
its specific override isn't set.  Valid values: ``auto`` / ``cpu`` /
``mps`` / ``cuda``.

We deliberately do NOT add ROCm / XPU / other accelerators yet —
consistent with NCMS's "works on a laptop" posture.  Add them here
(and only here) when we need them.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

Device = Literal["cuda", "mps", "cpu"]

_VALID = {"auto", "cpu", "mps", "cuda"}


def _auto() -> Device:
    """Detect the best-available device without an override."""
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_device(component_env: str | None = None) -> Device:
    """Return the PyTorch device string NCMS should load a model on.

    ``component_env`` is an optional per-component environment
    variable name (e.g. ``"NCMS_GLINER_DEVICE"``).  Falls back to
    ``NCMS_DEVICE`` and finally to :func:`_auto`.

    Invalid override values log a warning and fall through to the
    auto-detect path — we never crash the caller because of a typo
    in an env var.
    """
    candidates: list[str] = []
    if component_env:
        candidates.append(component_env)
    candidates.append("NCMS_DEVICE")

    for key in candidates:
        override = (os.environ.get(key) or "").strip().lower()
        if not override:
            continue
        if override not in _VALID:
            logger.warning(
                "[hardware] ignoring invalid %s=%r (use one of %s)",
                key,
                override,
                sorted(_VALID),
            )
            continue
        if override == "auto":
            continue
        return override  # type: ignore[return-value]

    return _auto()


def summary() -> dict[str, object]:
    """Describe what's available.  Useful for ``ncms info`` output."""
    try:
        import torch
    except ImportError:
        return {
            "torch": False,
            "cuda_available": False,
            "mps_available": False,
            "selected": "cpu",
        }
    return {
        "torch": True,
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device_count": (torch.cuda.device_count() if torch.cuda.is_available() else 0),
        "mps_available": (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()),
        "selected": _auto(),
    }
