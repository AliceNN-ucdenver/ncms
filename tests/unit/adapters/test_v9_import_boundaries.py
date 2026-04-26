"""Regression guard for the schemas ↔ domain_loader import cycle.

Phase B'.4 caught a silent-failure bug: ``schemas.py`` ran
``_hydrate_from_domain_registry()`` at end-of-module-import,
which imported ``domain_loader``, which transitively imported
the v9 package's ``archetypes`` module — creating a partial-
load cycle that the schemas-side ``except ImportError`` branch
silently swallowed.  Result: ``DOMAIN_MANIFESTS`` retained its
inline LEGACY paths, and v9-generated corpora landed at the
wrong location.

The fix moved hydration to FIRST-ACCESS lazy.  This test pins
that contract: ``get_domain_manifest`` must return the v9 path
regardless of which order callers import ``schemas`` and the
v9 package.

If a future change re-introduces eager hydration, this test
will fail loudly instead of silently shipping legacy paths.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DOMAINS_ROOT = _REPO / "adapters/domains"


def _run_subprocess(snippet: str) -> str:
    """Run ``snippet`` in a fresh Python process, return stdout.

    Each call gets its own Python interpreter so import-order
    state from previous tests doesn't leak in.
    """
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"subprocess failed: returncode={result.returncode}\nstderr: {result.stderr}",
        )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def domains_present():
    if not _DOMAINS_ROOT.is_dir():
        pytest.skip(f"adapters/domains/ not present at {_DOMAINS_ROOT}")


def test_schemas_first_returns_v9_paths(domains_present):
    """Importing schemas before v9 — the easy case."""
    out = _run_subprocess(
        "from ncms.application.adapters.schemas import get_domain_manifest\n"
        "print(get_domain_manifest('clinical').sdg_jsonl)\n",
    )
    assert "/adapters/corpora/v9/clinical/sdg.jsonl" in out, out


def test_v9_first_returns_v9_paths(domains_present):
    """Importing the v9 package before schemas — the case that
    triggered the silent failure pre-fix.

    With eager hydration, this would have returned the LEGACY
    path ``adapters/corpora/sdg_clinical.jsonl`` because the
    inner ImportError got swallowed.  With lazy hydration, the
    cycle never collides at module-import time and the first
    call to ``get_domain_manifest`` hydrates with a fully-
    formed module graph.
    """
    out = _run_subprocess(
        "from ncms.application.adapters.sdg.v9 import SparkBackend\n"
        "from ncms.application.adapters.schemas import get_domain_manifest\n"
        "_ = SparkBackend  # silence unused-import warning\n"
        "print(get_domain_manifest('clinical').sdg_jsonl)\n",
    )
    assert "/adapters/corpora/v9/clinical/sdg.jsonl" in out, out
    # Defensive: explicitly assert NOT the legacy path so a
    # future regression would have a clear failure message.
    assert "adapters/corpora/sdg_clinical.jsonl" not in out, out


def test_cli_import_path_returns_v9_paths(domains_present):
    """Reproduce the exact import order the ``ncms adapters
    generate-sdg`` CLI uses inside ``adapters_generate_sdg``.

    This was the production bug — the CLI imported the v9 helpers
    AND ``get_domain_manifest`` in the order that exposed the
    cycle.  Pinning it here ensures a future CLI refactor can't
    silently regress.
    """
    out = _run_subprocess(
        "from ncms.application.adapters.corpus.loader import dump_jsonl\n"
        "from ncms.application.adapters.domain_loader import load_domain\n"
        "from ncms.application.adapters.schemas import get_domain_manifest\n"
        "from ncms.application.adapters.sdg.v9 import (\n"
        "    SparkBackend, TemplateBackend,\n"
        "    generate_domain, generate_for_archetype,\n"
        ")\n"
        "_ = (dump_jsonl, load_domain, SparkBackend, TemplateBackend,\n"
        "     generate_domain, generate_for_archetype)\n"
        "for name in ('clinical', 'conversational', 'software_dev'):\n"
        "    print(f'{name}: {get_domain_manifest(name).sdg_jsonl}')\n",
    )
    for d in ("clinical", "conversational", "software_dev"):
        assert f"/adapters/corpora/v9/{d}/sdg.jsonl" in out, (
            f"{d} missing v9 path in output:\n{out}"
        )


def test_hydration_is_idempotent():
    """``_ensure_hydrated`` runs at most once per process; subsequent
    calls are no-ops.  Verifying this protects against accidental
    re-hydration loops if a future change adds more entry points."""
    out = _run_subprocess(
        "from ncms.application.adapters import schemas\n"
        "schemas._ensure_hydrated()\n"
        "first = id(schemas.DOMAIN_MANIFESTS['clinical'])\n"
        "schemas._ensure_hydrated()\n"
        "second = id(schemas.DOMAIN_MANIFESTS['clinical'])\n"
        "print('same' if first == second else 'different')\n",
    )
    assert out == "same", out


def test_disabled_via_env_flag(domains_present):
    """``NCMS_V9_DOMAIN_LOADER=0`` keeps the inline legacy manifests
    unchanged — the documented escape hatch for debugging a YAML
    regression."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ncms.application.adapters.schemas import get_domain_manifest\n"
            "print(get_domain_manifest('clinical').sdg_jsonl)\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        env={**__import__("os").environ, "NCMS_V9_DOMAIN_LOADER": "0"},
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    # With hydration disabled, the inline default ``sdg_clinical.jsonl``
    # path stands.
    assert "sdg_clinical.jsonl" in out.stdout, out.stdout
    # Defensive: explicitly NOT the v9 path.
    assert "/v9/clinical/sdg.jsonl" not in out.stdout, out.stdout
