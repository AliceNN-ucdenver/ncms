"""Unit tests for the v9 corpus judge (content-fidelity edition).

Covers:

* Archetype name extraction from :attr:`GoldExample.source`.
* Required-entities prompt block formatting.
* Stratified vs. uniform sampling in :func:`sync_judge_corpus`.
* Verdict aggregation + per-archetype bucketing using the new
  ``faithful / partial / unfaithful`` vocabulary.
* Failed-checks histogram for the four content-fidelity dimensions.
* ``format_report`` rendering.

The judge LLM call is patched out via :class:`AsyncMock` — no
live spend in CI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ncms.application.adapters.corpus.loader import dump_jsonl
from ncms.application.adapters.schemas import GoldExample, RoleSpan
from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec, RoleSpec
from ncms.application.adapters.sdg.v9.judge import (
    _archetype_of,
    _required_entities_block,
    format_report,
    sync_judge_corpus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _archetype(name: str = "positive_medication_start") -> ArchetypeSpec:
    """Minimal archetype matching the row factory below."""
    return ArchetypeSpec(
        name=name,
        domain="clinical",
        intent="positive",
        admission="persist",
        state_change="declaration",
        role_spans=(RoleSpec(role="primary", slot="medication", count=1),),
        description="Clinician starts a patient on a new medication.",
        example_utterances=("Started metformin 500mg BID.",),
    )


def _archetype_lookup(*archetypes: ArchetypeSpec) -> dict:
    return {a.name: a for a in archetypes}


def _make_row(
    *,
    text: str = "Started metformin once daily.",
    archetype: str = "positive_medication_start",
    seed: int = 17,
    intent: str = "positive",
    admission: str = "persist",
    state_change: str = "declaration",
    topic: str | None = "medication_mgmt",
    role_spans: tuple[RoleSpan, ...] = (),
) -> GoldExample:
    return GoldExample(
        text=text,
        domain="clinical",
        intent=intent,  # type: ignore[arg-type]
        slots={"medication": "metformin"},
        topic=topic,
        admission=admission,  # type: ignore[arg-type]
        state_change=state_change,  # type: ignore[arg-type]
        role_spans=list(role_spans),
        split="sdg",
        source=f"sdg-v9 archetype={archetype} seed={seed}",
    )


def _write_corpus(path: Path, rows: list[GoldExample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_jsonl(rows, path)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestArchetypeExtraction:
    def test_extracts_archetype_name(self):
        ex = _make_row(archetype="habitual_medication_regimen", seed=42)
        assert _archetype_of(ex) == "habitual_medication_regimen"

    def test_falls_back_when_source_missing(self):
        ex = _make_row()
        ex.source = ""
        assert _archetype_of(ex) == "unknown"


class TestRequiredEntitiesBlock:
    def test_no_role_spans_renders_skip_message(self):
        ex = _make_row()
        block = _required_entities_block(ex)
        assert "no required entities" in block

    def test_role_spans_render_per_line(self):
        ex = _make_row(
            role_spans=(
                RoleSpan(
                    char_start=0,
                    char_end=9,
                    surface="metformin",
                    canonical="metformin",
                    slot="medication",
                    role="primary",
                ),
                RoleSpan(
                    char_start=10,
                    char_end=20,
                    surface="once daily",
                    canonical="once daily",
                    slot="frequency",
                    role="primary",
                ),
            )
        )
        block = _required_entities_block(ex)
        assert "'metformin'" in block
        assert "role=primary" in block
        assert "slot=medication" in block
        assert "'once daily'" in block
        assert "slot=frequency" in block

    def test_not_relevant_spans_excluded(self):
        ex = _make_row(
            role_spans=(
                RoleSpan(
                    char_start=0,
                    char_end=9,
                    surface="metformin",
                    canonical="metformin",
                    slot="medication",
                    role="primary",
                ),
                RoleSpan(
                    char_start=10,
                    char_end=20,
                    surface="lisinopril",
                    canonical="lisinopril",
                    slot="medication",
                    role="not_relevant",
                ),
            )
        )
        block = _required_entities_block(ex)
        assert "metformin" in block
        # not_relevant span is dropped from the required-entity list.
        assert "lisinopril" not in block


# ---------------------------------------------------------------------------
# sync_judge_corpus (mock judge)
# ---------------------------------------------------------------------------


def _verdict(verdict: str = "faithful", failed_checks=()):
    return {
        "verdict": verdict,
        "issues": [f"synthetic-{verdict}"] if verdict != "faithful" else [],
        "failed_checks": list(failed_checks),
    }


class TestSyncJudgeCorpus:
    def test_missing_corpus_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_judge_corpus(
                domain="clinical",
                corpus_path=tmp_path / "nope.jsonl",
                archetype_lookup=_archetype_lookup(_archetype()),
                n_samples=5,
                model="mock",
                api_base=None,
            )

    def test_all_faithful_aggregates_100_pct(self, tmp_path):
        path = tmp_path / "c.jsonl"
        _write_corpus(path, [_make_row() for _ in range(10)])
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            return_value=_verdict("faithful"),
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                archetype_lookup=_archetype_lookup(_archetype()),
                n_samples=10,
                model="mock",
                api_base=None,
            )
        assert result.n_sampled == 10
        assert result.verdicts["faithful"] == 10
        assert result.pct_faithful == 100.0
        # pct_correct alias still works for legacy callers.
        assert result.pct_correct == 100.0
        assert result.failures == []

    def test_mixed_verdicts_track_failed_checks(self, tmp_path):
        path = tmp_path / "c.jsonl"
        rows = [_make_row(archetype="a1") for _ in range(5)] + [
            _make_row(archetype="a2") for _ in range(5)
        ]
        _write_corpus(path, rows)
        verdicts = [
            _verdict("faithful"),
            _verdict("partial", ["scenario_fidelity"]),
        ] * 5
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            side_effect=verdicts,
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                archetype_lookup=_archetype_lookup(
                    _archetype("a1"),
                    _archetype("a2"),
                ),
                n_samples=10,
                model="mock",
                api_base=None,
            )
        assert result.verdicts["faithful"] == 5
        assert result.verdicts["partial"] == 5
        assert result.failed_check_counts.get("scenario_fidelity", 0) == 5
        assert len(result.failures) == 5
        assert set(result.per_archetype.keys()) == {"a1", "a2"}

    def test_judge_failure_counts_as_unfaithful(self, tmp_path):
        path = tmp_path / "c.jsonl"
        _write_corpus(path, [_make_row() for _ in range(3)])
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            return_value=None,  # judge LLM error
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                archetype_lookup=_archetype_lookup(_archetype()),
                n_samples=3,
                model="mock",
                api_base=None,
            )
        assert result.verdicts["unfaithful"] == 3
        for bucket in result.per_archetype.values():
            assert bucket.get("failed", 0) > 0


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_report_contains_summary_fields(self, tmp_path):
        path = tmp_path / "c.jsonl"
        _write_corpus(path, [_make_row(archetype="arch_x") for _ in range(4)])
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            side_effect=[
                _verdict("faithful"),
                _verdict("faithful"),
                _verdict("partial", ["coherence"]),
                _verdict("unfaithful", ["entity_presence", "scenario_fidelity"]),
            ],
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                archetype_lookup=_archetype_lookup(_archetype("arch_x")),
                n_samples=4,
                model="mock",
                api_base=None,
            )
        report = format_report(result)
        assert "domain=clinical" in report
        assert "pct_faithful:" in report
        assert "50.0%" in report
        assert "per-archetype" in report
        assert "arch_x" in report
        # failed checks histogram surfaces top offenders.
        assert "entity_presence" in report or "scenario_fidelity" in report or "coherence" in report
