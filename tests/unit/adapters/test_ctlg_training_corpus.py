"""Unit tests for CTLG training-corpus preparation."""

from __future__ import annotations

from pathlib import Path

from ncms.application.adapters.ctlg import (
    build_ctlg_training_corpus,
    dump_ctlg_jsonl,
    load_ctlg_jsonl,
    validate_ctlg_row,
)


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


def test_build_ctlg_training_corpus_filters_non_composable_queries(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "train.jsonl"
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
        source,
    )

    result = build_ctlg_training_corpus([source], output_path=output)
    written = load_ctlg_jsonl(output)

    assert result.n_written == 2
    assert result.n_excluded == 1
    assert result.exclusions[0].reason == "query_not_synthesizable"
    assert result.by_exclusion_reason == {"query_not_synthesizable": 1}
    assert [ex.text for ex in written] == [
        "What did we use before Postgres?",
        "Postgres replaced MySQL.",
    ]
    assert result.by_voice == {"query": 1, "memory": 1}


def test_build_ctlg_training_corpus_can_dedupe_text(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "train.jsonl"
    dump_ctlg_jsonl(
        [
            validate_ctlg_row(_row()),
            validate_ctlg_row(_row()),
        ],
        source,
    )

    result = build_ctlg_training_corpus([source], output_path=output)

    assert result.n_written == 1
    assert result.n_excluded == 1
    assert result.exclusions[0].reason == "duplicate_text"


def test_build_ctlg_training_corpus_can_exclude_memory_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "train.jsonl"
    dump_ctlg_jsonl(
        [
            validate_ctlg_row(_row()),
            validate_ctlg_row(
                _row(
                    text="Redis caused a cache outage.",
                    tokens=["Redis", "caused", "a", "cache", "outage", "."],
                    cue_tags=["B-REFERENT", "B-CAUSAL_EXPLICIT", "O", "O", "O", "O"],
                    voice="memory",
                )
            ),
        ],
        source,
    )

    result = build_ctlg_training_corpus([source], output_path=output, include_memory=False)

    assert result.n_written == 1
    assert load_ctlg_jsonl(output)[0].voice == "query"
