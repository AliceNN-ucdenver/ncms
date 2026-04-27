"""Tests for the production CTLG cue-tagger factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncms.application.adapters.ctlg import CTLGAdapterIntegrityError, CTLGAdapterManifest
from ncms.application.ctlg_cue_tagger import (
    build_default_ctlg_cue_tagger,
    find_ctlg_adapter_dir,
    list_available_ctlg_adapters,
)


def _write_artifact(
    root: Path,
    *,
    domain: str = "software_dev",
    version: str = "ctlg-v1",
) -> Path:
    adapter_dir = root / domain / version
    (adapter_dir / "lora_adapter").mkdir(parents=True)
    (adapter_dir / "lora_adapter" / "adapter_config.json").write_text("{}")
    (adapter_dir / "heads.safetensors").write_bytes(b"not-empty")
    CTLGAdapterManifest(domain=domain, version=version).save(adapter_dir / "manifest.json")
    return adapter_dir


def test_find_ctlg_adapter_dir_prefers_ctlg_versions(tmp_path: Path) -> None:
    _write_artifact(tmp_path, version="ctlg-v1")
    non_ctlg = tmp_path / "software_dev" / "v9"
    non_ctlg.mkdir(parents=True)
    (non_ctlg / "manifest.json").write_text("{}")

    found = find_ctlg_adapter_dir("software_dev", root=tmp_path)

    assert found == tmp_path / "software_dev" / "ctlg-v1"


def test_find_ctlg_adapter_dir_accepts_pinned_version(tmp_path: Path) -> None:
    adapter_dir = _write_artifact(tmp_path, version="ctlg-v2")

    assert find_ctlg_adapter_dir("software_dev", version="ctlg-v2", root=tmp_path) == adapter_dir
    assert find_ctlg_adapter_dir("software_dev", version="ctlg-missing", root=tmp_path) is None


def test_list_available_ctlg_adapters_filters_invalid_layouts(tmp_path: Path) -> None:
    _write_artifact(tmp_path, domain="software_dev", version="ctlg-v1")
    invalid = tmp_path / "clinical" / "ctlg-v1"
    invalid.mkdir(parents=True)

    assert list_available_ctlg_adapters(root=tmp_path) == {"software_dev": ["ctlg-v1"]}


def test_build_default_ctlg_cue_tagger_returns_none_when_optional(tmp_path: Path) -> None:
    assert (
        build_default_ctlg_cue_tagger(domain="software_dev", root=tmp_path, required=False)
        is None
    )


def test_build_default_ctlg_cue_tagger_raises_when_required(tmp_path: Path) -> None:
    with pytest.raises(CTLGAdapterIntegrityError, match="no CTLG cue-tagger"):
        build_default_ctlg_cue_tagger(domain="software_dev", root=tmp_path, required=True)
