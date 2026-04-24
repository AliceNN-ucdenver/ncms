"""Unit tests for the v9 corpus judge.

Covers:

* Archetype extraction from :attr:`GoldExample.source` provenance strings.
* Role-span summary formatting.
* Stratified vs. uniform sampling.
* Verdict aggregation + per-archetype bucketing.
* Failure-list capture.
* ``format_report`` rendering.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ncms.application.adapters.corpus.loader import dump_jsonl
from ncms.application.adapters.schemas import GoldExample, RoleSpan
from ncms.application.adapters.sdg.v9.judge import (
    _archetype_of,
    _role_spans_summary,
    format_report,
    sync_judge_corpus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    def test_preserves_unknown_shape(self):
        ex = _make_row()
        ex.source = "hand-labeled 2026-04"
        assert _archetype_of(ex) == "hand-labeled 2026-04"


class TestRoleSpansSummary:
    def test_empty(self):
        assert _role_spans_summary(_make_row()) == "(none)"

    def test_compact_format(self):
        spans = (
            RoleSpan(
                char_start=0, char_end=9, surface="metformin",
                canonical="metformin", slot="medication", role="primary",
            ),
            RoleSpan(
                char_start=20, char_end=30, surface="once daily",
                canonical="once daily", slot="frequency", role="primary",
            ),
        )
        ex = _make_row(role_spans=spans)
        s = _role_spans_summary(ex)
        assert "primary:medication='metformin'" in s
        assert "primary:frequency='once daily'" in s
        assert s.startswith("[") and s.endswith("]")


# ---------------------------------------------------------------------------
# sync_judge_corpus (mock judge)
# ---------------------------------------------------------------------------


def _mock_verdict(verdict: str = "correct", wrong_heads=()):
    """Factory: sequence-friendly mock verdict dict."""
    return {
        "verdict": verdict,
        "issues": [f"synthetic-{verdict}"] if verdict != "correct" else [],
        "wrong_heads": list(wrong_heads),
    }


class TestSyncJudgeCorpus:
    def test_missing_corpus_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_judge_corpus(
                domain="clinical",
                corpus_path=tmp_path / "nope.jsonl",
                topics=("medication_mgmt",),
                n_samples=5, model="mock", api_base=None,
            )

    def test_all_correct_aggregates_100_pct(self, tmp_path):
        path = tmp_path / "c.jsonl"
        _write_corpus(path, [_make_row() for _ in range(10)])
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            return_value=_mock_verdict("correct"),
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                topics=("medication_mgmt",),
                n_samples=10, model="mock", api_base=None,
            )
        assert result.n_sampled == 10
        assert result.verdicts["correct"] == 10
        assert result.pct_correct == 100.0
        assert result.failures == []

    def test_mixed_verdicts_tracked_per_head(self, tmp_path):
        path = tmp_path / "c.jsonl"
        # Mix archetypes so stratified sampling is exercised.
        rows = [
            _make_row(archetype="a1") for _ in range(5)
        ] + [
            _make_row(archetype="a2") for _ in range(5)
        ]
        _write_corpus(path, rows)
        # Alternate correct / partially-wrong (wrong_heads=intent).
        verdicts = [
            _mock_verdict("correct"),
            _mock_verdict("partially_wrong", ["intent"]),
        ] * 5
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            side_effect=verdicts,
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                topics=("medication_mgmt",),
                n_samples=10, model="mock", api_base=None,
            )
        assert result.verdicts["correct"] == 5
        assert result.verdicts["partially_wrong"] == 5
        assert result.wrong_head_counts.get("intent", 0) == 5
        assert len(result.failures) == 5
        # Stratified: both archetypes appear in per_archetype.
        assert set(result.per_archetype.keys()) == {"a1", "a2"}

    def test_judge_failure_counts_as_severe(self, tmp_path):
        path = tmp_path / "c.jsonl"
        _write_corpus(path, [_make_row() for _ in range(3)])
        with patch(
            "ncms.application.adapters.sdg.v9.judge._judge_one",
            new_callable=AsyncMock,
            return_value=None,  # judge LLM returned None (error)
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                topics=("medication_mgmt",),
                n_samples=3, model="mock", api_base=None,
            )
        assert result.verdicts["severely_wrong"] == 3
        # judge-failed rows surface in the per-archetype "failed" bucket.
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
                _mock_verdict("correct"),
                _mock_verdict("correct"),
                _mock_verdict("partially_wrong", ["topic"]),
                _mock_verdict("severely_wrong", ["intent", "admission"]),
            ],
        ):
            result = sync_judge_corpus(
                domain="clinical",
                corpus_path=path,
                topics=("medication_mgmt",),
                n_samples=4, model="mock", api_base=None,
            )
        report = format_report(result)
        assert "domain=clinical" in report
        assert "pct_correct:" in report
        assert "50.0%" in report or "50.00%" in report
        assert "per-archetype" in report
        assert "arch_x" in report
        # wrong heads histogram shows top offender.
        assert "topic" in report or "intent" in report