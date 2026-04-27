"""Unit tests for CTLG grammar corpus audit."""

from __future__ import annotations

from pathlib import Path

from ncms.application.adapters.ctlg import audit_ctlg_files, dump_ctlg_jsonl, validate_ctlg_row


def _row(**overrides):
    row = {
        "text": "What did we use before Postgres?",
        "tokens": ["What", "did", "we", "use", "before", "Postgres", "?"],
        "cue_tags": ["O", "O", "O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
        "domain": "software_dev",
        "voice": "query",
        "split": "llm",
    }
    row.update(overrides)
    return row


def test_audit_ctlg_files_reports_query_synthesis_rate(tmp_path: Path) -> None:
    path = tmp_path / "ctlg.jsonl"
    dump_ctlg_jsonl(
        [
            validate_ctlg_row(_row()),
            validate_ctlg_row(
                _row(
                    text="Which commit introduced the bug?",
                    tokens=["Which", "commit", "introduced", "the", "bug", "?"],
                    cue_tags=["B-REFERENT", "O", "O", "O", "O", "O"],
                )
            ),
            validate_ctlg_row(
                _row(
                    text="Postgres replaced MySQL.",
                    tokens=["Postgres", "replaced", "MySQL", "."],
                    cue_tags=["B-REFERENT", "B-CAUSAL_EXPLICIT", "B-REFERENT", "O"],
                    voice="memory",
                )
            ),
        ],
        path,
    )

    report = audit_ctlg_files([path])
    payload = report.to_json()

    assert report.n_query == 2
    assert report.n_query_synthesized == 1
    assert report.query_synthesis_rate == 0.5
    assert not report.ok(min_query_synthesis_rate=0.75)
    assert payload["files"][0]["misses"][0]["text"] == "Which commit introduced the bug?"


def test_audit_ctlg_files_tracks_expected_tlg_coverage(tmp_path: Path) -> None:
    path = tmp_path / "ctlg_expected.jsonl"
    dump_ctlg_jsonl(
        [
            validate_ctlg_row(
                _row(
                    expected_tlg_query={
                        "axis": "temporal",
                        "relation": "predecessor",
                        "referent": "postgres",
                    }
                )
            )
        ],
        path,
    )

    report = audit_ctlg_files([path])

    assert report.expected_tlg_coverage == 1.0
    assert report.ok(min_query_synthesis_rate=1.0)
