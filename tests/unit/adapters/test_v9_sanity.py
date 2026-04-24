"""Unit tests for the v9 offline corpus sanity check.

Covers every invariant the :mod:`sdg.v9.sanity` module enforces.
Each test constructs a minimal :class:`DomainSpec` + writes a
crafted JSONL, runs :func:`sanity_check`, and asserts the expected
invariant hit (and no unintended hits).

Note: ``intent=null`` is rejected upstream by
:func:`corpus.loader.load_jsonl`, so the "intent is None" case
is not reachable from a sanity-check POV and has no test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncms.application.adapters.domain_loader import (
    DiversityTaxonomy,
    DomainSpec,
)
from ncms.application.adapters.sdg.catalog.primitives import CatalogEntry
from ncms.application.adapters.sdg.v9 import sanity_check
from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec, RoleSpec


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _minimal_spec(tmp_path: Path) -> DomainSpec:
    """Single-archetype spec that exercises the full invariant set."""
    archetype = ArchetypeSpec(
        name="test_arch",
        domain="clinical",
        intent="positive",
        admission="persist",
        state_change="declaration",
        role_spans=(
            RoleSpec(role="primary", slot="medication", count=1),
        ),
        target_min_chars=20,
        target_max_chars=120,
        description="test archetype",
    )
    gazetteer = (
        CatalogEntry(
            canonical="metformin",
            slot="medication",
            topic="medication_mgmt",
        ),
    )
    return DomainSpec(
        name="clinical",
        description="test",
        intended_content="",
        slots=("medication",),
        topics=("medication_mgmt", "other"),
        gazetteer=gazetteer,
        diversity=DiversityTaxonomy(nodes=()),
        archetypes=(archetype,),
        gold_jsonl_path=tmp_path / "gold.jsonl",
        sdg_jsonl_path=tmp_path / "sdg.jsonl",
        adversarial_jsonl_path=tmp_path / "adv.jsonl",
        adapter_output_root=tmp_path / "ckpt",
        deployed_adapter_root=tmp_path / "deployed",
        default_adapter_version="v9",
        source_dir=tmp_path / "spec",
    )


# Reference "valid" row used as the starting point for each test.
# Callers mutate one or two fields via ``**overrides``.
_VALID_ROW: dict = {
    "text": "Started metformin for the newly diagnosed patient.",
    "domain": "clinical",
    "intent": "positive",
    "slots": {"medication": "metformin"},
    "topic": "medication_mgmt",
    "admission": "persist",
    "state_change": "declaration",
    "role_spans": [
        {
            "char_start": 8,
            "char_end": 17,
            "surface": "metformin",
            "canonical": "metformin",
            "slot": "medication",
            "role": "primary",
            "source": "sdg-v9",
        },
    ],
    "split": "sdg",
    "source": "sdg-v9 archetype=test_arch seed=1",
    "note": "",
}


def _write_rows(path: Path, rows: list[dict]) -> None:
    """Write JSON objects directly — bypasses GoldExample so we can
    craft null-label cases the :func:`dump_jsonl` helper cannot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(**overrides) -> dict:
    """Return a deep copy of ``_VALID_ROW`` with ``overrides`` applied.

    ``role_spans`` supports list or None; None → drop the field so
    the loader reads ``role_spans=[]``.
    """
    import copy
    r = copy.deepcopy(_VALID_ROW)
    for k, v in overrides.items():
        if v is None and k in ("admission", "state_change", "topic"):
            # Preserve None as JSON null (loader treats as absent).
            r[k] = None
        elif v is None:
            r.pop(k, None)
        else:
            r[k] = v
    return r


# ---------------------------------------------------------------------------
# Happy path + summary
# ---------------------------------------------------------------------------


class TestSanityCheckHappyPath:
    def test_all_invariants_pass(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "corpus.jsonl"
        _write_rows(path, [_row(), _row()])
        report = sanity_check(path, spec)
        assert report.ok, report.summary()
        assert report.n_rows == 2
        assert report.per_archetype_rows["test_arch"] == 2
        assert report.failure_counts == {}

    def test_report_summary_strings(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "corpus.jsonl"
        _write_rows(path, [_row()])
        report = sanity_check(path, spec)
        assert "sanity OK" in report.summary()
        assert report.ok is True


# ---------------------------------------------------------------------------
# Label presence (I2–I4 — I1 enforced upstream)
# ---------------------------------------------------------------------------


class TestLabelInvariants:
    def test_I2_admission_none_detected(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(admission=None)])
        report = sanity_check(path, spec)
        assert report.failure_counts["I2_admission_none"] == 1

    def test_I3_state_change_none_detected(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(state_change=None)])
        report = sanity_check(path, spec)
        assert report.failure_counts["I3_state_change_none"] == 1

    def test_I4_topic_none_detected(self, tmp_path):
        """Regression guard for the B'.4 bug where every row carried
        topic=None.  Most important single invariant — training cannot
        teach the topic head without labels."""
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(topic=None)])
        report = sanity_check(path, spec)
        assert report.failure_counts["I4_topic_none"] == 1

    def test_I4_topic_unknown_detected(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(topic="not_in_vocab")])
        report = sanity_check(path, spec)
        assert report.failure_counts["I4_topic_unknown"] == 1


# ---------------------------------------------------------------------------
# Role-span invariants (R1–R3)
# ---------------------------------------------------------------------------


class TestRoleSpanInvariants:
    def test_R1_role_spans_empty(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(role_spans=[])])
        report = sanity_check(path, spec)
        assert report.failure_counts["R1_role_spans_empty"] == 1

    def test_R2_role_span_mismatch_wrong_count(self, tmp_path):
        """Archetype asks for 1 primary medication, row has 2."""
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        span = {
            "char_start": 0, "char_end": 9, "surface": "metformin",
            "canonical": "metformin", "slot": "medication",
            "role": "primary", "source": "sdg-v9",
        }
        _write_rows(path, [_row(role_spans=[span, dict(span, char_start=10, char_end=22)])])
        report = sanity_check(path, spec)
        assert report.failure_counts["R2_role_span_mismatch"] == 1

    def test_R2_role_span_mismatch_wrong_role(self, tmp_path):
        """Archetype asks for primary, row has alternative."""
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(role_spans=[{
            "char_start": 8, "char_end": 17, "surface": "metformin",
            "canonical": "metformin", "slot": "medication",
            "role": "alternative", "source": "sdg-v9",
        }])])
        report = sanity_check(path, spec)
        assert report.failure_counts["R2_role_span_mismatch"] == 1

    def test_R2_not_relevant_spans_ignored(self, tmp_path):
        """not_relevant spans don't count toward the archetype target."""
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(role_spans=[
            {
                "char_start": 8, "char_end": 17, "surface": "metformin",
                "canonical": "metformin", "slot": "medication",
                "role": "primary", "source": "sdg-v9",
            },
            {
                "char_start": 20, "char_end": 29, "surface": "lisinopril",
                "canonical": "lisinopril", "slot": "medication",
                "role": "not_relevant", "source": "sdg-v9",
            },
        ])])
        report = sanity_check(path, spec)
        assert "R2_role_span_mismatch" not in report.failure_counts

    def test_R3_surface_not_in_text(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(
            text="Patient reports moderate back pain this morning.",
            role_spans=[{
                "char_start": 0, "char_end": 9, "surface": "metformin",
                "canonical": "metformin", "slot": "medication",
                "role": "primary", "source": "sdg-v9",
            }],
            slots={"medication": "metformin"},
        )])
        report = sanity_check(path, spec)
        assert report.failure_counts["R3_surface_missing_from_text"] == 1


# ---------------------------------------------------------------------------
# Text invariants (T1–T3)
# ---------------------------------------------------------------------------


class TestTextInvariants:
    def test_T1_empty_text(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(text="   ")])
        report = sanity_check(path, spec)
        assert report.failure_counts["T1_text_empty"] == 1

    def test_T2_placeholder_leak(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(text="Started metformin for {condition}.")])
        report = sanity_check(path, spec)
        assert report.failure_counts["T2_placeholder_leak"] == 1

    def test_T3_too_short(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(text="metformin.")])  # 10 chars < 20
        report = sanity_check(path, spec)
        assert report.failure_counts["T3_too_short"] == 1

    def test_T3_too_long(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(text="Started metformin. " * 20)])
        report = sanity_check(path, spec)
        assert report.failure_counts["T3_too_long"] == 1


# ---------------------------------------------------------------------------
# Unknown archetype
# ---------------------------------------------------------------------------


class TestUnknownArchetype:
    def test_P0_unknown_archetype_flagged(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(
            source="sdg-v9 archetype=definitely_not_a_real_arch seed=1",
        )])
        report = sanity_check(path, spec)
        assert report.failure_counts["P0_unknown_archetype"] == 1


# ---------------------------------------------------------------------------
# Failure-list sample cap
# ---------------------------------------------------------------------------


class TestSampleCap:
    def test_failures_cap_per_invariant(self, tmp_path):
        """Even with 20 bad rows, failures list is capped at sample_cap."""
        spec = _minimal_spec(tmp_path)
        path = tmp_path / "c.jsonl"
        _write_rows(path, [_row(topic=None) for _ in range(20)])
        report = sanity_check(path, spec, sample_cap=3)
        # Count is the full 20, but failures list caps at 3.
        assert report.failure_counts["I4_topic_none"] == 20
        sampled = [f for f in report.failures if f.invariant == "I4_topic_none"]
        assert len(sampled) == 3


# ---------------------------------------------------------------------------
# Missing corpus file
# ---------------------------------------------------------------------------


class TestMissingCorpus:
    def test_missing_file_raises(self, tmp_path):
        spec = _minimal_spec(tmp_path)
        with pytest.raises(FileNotFoundError):
            sanity_check(tmp_path / "does_not_exist.jsonl", spec)
