from __future__ import annotations

import json
from pathlib import Path

from ncms.application.intent_slot_chain import find_adapter_dir


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(payload))


def test_find_adapter_dir_skips_ctlg_sibling(tmp_path: Path) -> None:
    domain_dir = tmp_path / "software_dev"
    intent_dir = domain_dir / "v9"
    ctlg_dir = domain_dir / "ctlg-v1"
    _write_manifest(
        intent_dir,
        {
            "domain": "software_dev",
            "version": "v9",
            "intent_labels": ["none"],
        },
    )
    _write_manifest(
        ctlg_dir,
        {
            "domain": "software_dev",
            "version": "ctlg-v1",
            "cue_labels": ["O", "B-CAUSAL_EXPLICIT"],
        },
    )

    # Make the CTLG directory newer, matching the deploy sequence that
    # previously caused the five-head SLM loader to pick the wrong adapter.
    ctlg_dir.touch()

    assert find_adapter_dir("software_dev", root=tmp_path) == intent_dir


def test_find_adapter_dir_rejects_pinned_ctlg_version(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "software_dev" / "ctlg-v1",
        {
            "domain": "software_dev",
            "version": "ctlg-v1",
            "cue_labels": ["O", "B-CAUSAL_EXPLICIT"],
        },
    )

    assert find_adapter_dir("software_dev", version="ctlg-v1", root=tmp_path) is None
