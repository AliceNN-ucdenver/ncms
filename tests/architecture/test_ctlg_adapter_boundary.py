"""Fitness functions for the CTLG / five-head SLM boundary."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ncms"
JOINT_ADAPTER = SRC_ROOT / "application" / "adapters" / "methods" / "joint_bert_lora.py"

FORBIDDEN_RUNTIME_NAMES = {
    "cue_head",
    "shape_cue_head",
    "shape_intent_head",
    "cue_labels",
    "shape_cue_labels",
    "shape_intent_labels",
}


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple | ast.List):
        out: set[str] = set()
        for elt in node.elts:
            out.update(_target_names(elt))
        return out
    return set()


def test_v9_joint_adapter_has_no_runtime_ctlg_head_or_manifest_fields() -> None:
    """CTLG must stay in a sibling adapter, not the five-head SLM."""
    tree = ast.parse(JOINT_ADAPTER.read_text())
    violations: list[str] = []

    for node in ast.walk(tree):
        targets: set[str] = set()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                targets.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            targets.update(_target_names(node.target))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            targets.add(node.name)

        for name in sorted(targets & FORBIDDEN_RUNTIME_NAMES):
            violations.append(f"{name} at line {getattr(node, 'lineno', '?')}")

    assert not violations, (
        "CTLG cue tagging must not be reintroduced into the v9 five-head "
        "joint adapter. Put cue labels and cue heads in the dedicated "
        "CTLG adapter instead:\n  "
        + "\n  ".join(violations)
    )
