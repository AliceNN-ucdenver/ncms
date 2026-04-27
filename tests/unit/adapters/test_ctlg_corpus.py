"""Unit tests for the dedicated CTLG cue-tag corpus loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncms.application.adapters.ctlg import (
    CTLGCorpusError,
    CTLGExample,
    CTLGExpectedQuery,
    dump_ctlg_jsonl,
    load_ctlg_jsonl,
    validate_ctlg_jsonl,
    validate_ctlg_row,
)


def _row(**overrides) -> dict:
    row = {
        "text": "Why did Postgres replace MySQL?",
        "tokens": ["Why", "did", "Postgres", "replace", "MySQL", "?"],
        "cue_tags": ["B-CAUSAL_EXPLICIT", "O", "B-REFERENT", "O", "B-REFERENT", "O"],
        "domain": "software_dev",
        "voice": "query",
        "split": "train",
        "source": "unit",
        "note": "",
    }
    row.update(overrides)
    return row


def _write(path: Path, rows: list[dict] | list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            if isinstance(row, str):
                fh.write(row + "\n")
            else:
                fh.write(json.dumps(row) + "\n")


class TestCTLGRowValidation:
    def test_valid_row_derives_offsets(self) -> None:
        ex = validate_ctlg_row(_row())

        assert ex.voice == "query"
        assert ex.domain == "software_dev"
        assert ex.char_offsets[0] == (0, 3)
        assert ex.char_offsets[2] == (8, 16)

    def test_valid_row_accepts_explicit_offsets(self) -> None:
        ex = validate_ctlg_row(
            _row(
                char_offsets=[
                    {"char_start": 0, "char_end": 3},
                    {"char_start": 4, "char_end": 7},
                    {"char_start": 8, "char_end": 16},
                    {"char_start": 17, "char_end": 24},
                    {"char_start": 25, "char_end": 30},
                    {"char_start": 30, "char_end": 31},
                ],
            ),
        )

        assert ex.char_offsets[-1] == (30, 31)

    def test_valid_row_accepts_expected_tlg_query(self) -> None:
        ex = validate_ctlg_row(
            _row(
                expected_tlg_query={
                    "axis": "causal",
                    "relation": "cause_of",
                    "referent": "postgres",
                    "secondary": "mysql",
                    "depth": 1,
                }
            )
        )

        assert ex.expected_tlg_query == CTLGExpectedQuery(
            axis="causal",
            relation="cause_of",
            referent="postgres",
            secondary="mysql",
        )

    def test_expected_tlg_query_rejects_unknown_relation(self) -> None:
        with pytest.raises(CTLGCorpusError, match="expected_tlg.relation"):
            validate_ctlg_row(
                _row(expected_tlg_query={"axis": "causal", "relation": "not_real"})
            )

    def test_missing_required_field_rejected(self) -> None:
        row = _row()
        row.pop("voice")

        with pytest.raises(CTLGCorpusError, match="schema.missing"):
            validate_ctlg_row(row)

    def test_unknown_label_rejected(self) -> None:
        with pytest.raises(CTLGCorpusError, match="cue_tags.label"):
            validate_ctlg_row(_row(cue_tags=["B-NOT_REAL", "O", "O", "O", "O", "O"]))

    def test_tag_length_mismatch_rejected(self) -> None:
        with pytest.raises(CTLGCorpusError, match="cue_tags.length"):
            validate_ctlg_row(_row(cue_tags=["O"]))

    def test_illegal_i_tag_rejected(self) -> None:
        with pytest.raises(CTLGCorpusError, match="bio.illegal_i"):
            validate_ctlg_row(
                _row(cue_tags=["I-CAUSAL_EXPLICIT", "O", "O", "O", "O", "O"]),
            )

    def test_offset_slice_mismatch_rejected(self) -> None:
        with pytest.raises(CTLGCorpusError, match="offsets.slice"):
            validate_ctlg_row(
                _row(
                    char_offsets=[
                        {"char_start": 0, "char_end": 4},
                        {"char_start": 4, "char_end": 7},
                        {"char_start": 8, "char_end": 16},
                        {"char_start": 17, "char_end": 24},
                        {"char_start": 25, "char_end": 30},
                        {"char_start": 30, "char_end": 31},
                    ],
                ),
            )

    def test_unalignable_tokens_rejected(self) -> None:
        with pytest.raises(CTLGCorpusError, match="offsets.derive"):
            validate_ctlg_row(_row(tokens=["why", "missing"], cue_tags=["O", "O"]))

    def test_legacy_tagged_tokens_are_normalized(self) -> None:
        ex = validate_ctlg_row(
            {
                "text": "What before Kubernetes: Kubernetes?",
                "tokens": [
                    {
                        "char_start": 0,
                        "char_end": 4,
                        "surface": "What",
                        "cue_label": "O",
                    },
                    {
                        "char_start": 5,
                        "char_end": 11,
                        "surface": "before",
                        "cue_label": "B-TEMPORAL_BEFORE",
                    },
                    {
                        "char_start": 12,
                        "char_end": 23,
                        "surface": "Kubernetes:",
                        "cue_label": "B-REFERENT",
                    },
                    {
                        "char_start": 12,
                        "char_end": 22,
                        "surface": "Kubernetes",
                        "cue_label": "I-REFERENT",
                    },
                ],
                "domain": "software_dev",
                "voice": "query",
                "split": "gold",
            },
        )

        assert ex.tokens == ("What", "before", "Kubernetes", ":", "Kubernetes", "?")
        assert ex.cue_tags == (
            "O",
            "B-TEMPORAL_BEFORE",
            "B-REFERENT",
            "B-REFERENT",
            "O",
            "O",
        )


class TestCTLGFileValidation:
    def test_validate_jsonl_collects_diagnostics_and_stats(self, tmp_path: Path) -> None:
        path = tmp_path / "ctlg.jsonl"
        _write(
            path,
            [
                _row(),
                _row(voice="memory", split="dev", cue_tags=["O", "O", "O", "O", "O", "O"]),
                _row(cue_tags=["I-CAUSAL_EXPLICIT", "O", "O", "O", "O", "O"]),
                "{bad json",
            ],
        )

        report = validate_ctlg_jsonl(path)

        assert not report.ok
        assert report.rows_seen == 4
        assert len(report.examples) == 2
        assert len(report.diagnostics) == 2
        assert report.by_voice == {"query": 1, "memory": 1}
        assert report.by_split == {"train": 1, "dev": 1}
        assert report.by_cue_family["CAUSAL_EXPLICIT"] == 1
        assert report.by_cue_family["REFERENT"] == 2
        assert report.by_cue_family["O"] == 9

    def test_load_jsonl_raises_on_diagnostics(self, tmp_path: Path) -> None:
        path = tmp_path / "ctlg.jsonl"
        _write(path, [_row(cue_tags=["I-CAUSAL_EXPLICIT", "O", "O", "O", "O", "O"])])

        with pytest.raises(CTLGCorpusError, match="bio.illegal_i"):
            load_ctlg_jsonl(path)

    def test_dump_round_trips(self, tmp_path: Path) -> None:
        original = validate_ctlg_row(
            _row(expected_tlg_query={"axis": "causal", "relation": "cause_of"})
        )
        path = tmp_path / "roundtrip.jsonl"

        dump_ctlg_jsonl([original], path)
        loaded = load_ctlg_jsonl(path)

        assert loaded == [original]

    def test_example_type_is_hashable_enough_for_tuple_fields(self) -> None:
        ex = validate_ctlg_row(_row())

        assert isinstance(ex, CTLGExample)
        assert isinstance(ex.tokens, tuple)
        assert isinstance(ex.cue_tags, tuple)
        assert isinstance(ex.char_offsets, tuple)
