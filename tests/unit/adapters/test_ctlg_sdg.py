"""Tests for deterministic CTLG SDG generation."""

from __future__ import annotations

from collections import Counter

import pytest

from ncms.application.adapters.ctlg import (
    CTLGSDGRequest,
    dump_ctlg_jsonl,
    generate_ctlg_sdg_examples,
    load_ctlg_jsonl,
)
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import synthesize


def _families(rows) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for tag in row.cue_tags:
            if tag == "O":
                continue
            counts[tag.split("-", 1)[1]] += 1
    return counts


def _tagged_tokens(row) -> list[TaggedToken]:
    return [
        TaggedToken(
            char_start=start,
            char_end=end,
            surface=surface,
            cue_label=label,
            confidence=0.99,
        )
        for surface, label, (start, end) in zip(
            row.tokens,
            row.cue_tags,
            row.char_offsets,
            strict=True,
        )
    ]


def test_generate_ctlg_sdg_examples_returns_valid_requested_count(tmp_path) -> None:
    rows = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="mixed", n_rows=40, seed=7),
    )

    assert len(rows) == 40
    assert {row.domain for row in rows} == {"software_dev"}
    assert {row.split for row in rows} == {"sdg"}
    assert {row.source for row in rows} == {"sdg_ctlg_template"}

    path = tmp_path / "ctlg_sdg.jsonl"
    dump_ctlg_jsonl(rows, path)
    assert len(load_ctlg_jsonl(path)) == 40


def test_generate_ctlg_sdg_query_rows_include_expected_tlg_contract() -> None:
    rows = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="query", n_rows=32, seed=11),
    )

    assert rows
    assert all(row.expected_tlg_query is not None for row in rows)
    for row in rows:
        expected = row.expected_tlg_query
        assert expected is not None
        actual = synthesize(_tagged_tokens(row))
        assert actual is not None, row.text
        assert actual.axis == expected.axis
        assert actual.relation == expected.relation
        assert actual.referent == expected.referent
        assert actual.secondary == expected.secondary
        assert actual.subject == expected.subject
        assert actual.scope == expected.scope
        assert actual.temporal_anchor == expected.temporal_anchor
        assert actual.scenario == expected.scenario


def test_generate_ctlg_sdg_targeted_mseb_rows_compose_to_contract() -> None:
    rows = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="mseb_targeted", n_rows=24, seed=17),
    )

    assert {row.voice for row in rows} == {"query"}
    assert {row.note for row in rows} >= {
        "mseb_current_adopted",
        "mseb_current_latest_approach",
        "mseb_ordinal_last_section",
        "mseb_predecessor_preceded",
        "mseb_retirement_alternatives",
        "mseb_sequence_context",
    }
    for row in rows:
        expected = row.expected_tlg_query
        assert expected is not None
        actual = synthesize(_tagged_tokens(row))
        assert actual is not None, row.text
        assert (actual.axis, actual.relation) == (expected.axis, expected.relation)


def test_generate_ctlg_sdg_examples_balances_core_cue_families() -> None:
    rows = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="mixed", n_rows=80, seed=3),
    )

    families = _families(rows)
    for expected in (
        "CAUSAL_EXPLICIT",
        "CAUSAL_ALTLEX",
        "TEMPORAL_BEFORE",
        "TEMPORAL_AFTER",
        "TEMPORAL_DURING",
        "TEMPORAL_SINCE",
        "TEMPORAL_ANCHOR",
        "ORDINAL_FIRST",
        "ORDINAL_LAST",
        "ORDINAL_NTH",
        "MODAL_HYPOTHETICAL",
        "ASK_CHANGE",
        "ASK_CURRENT",
        "REFERENT",
        "SUBJECT",
        "SCOPE",
    ):
        assert families[expected] > 0


def test_generate_ctlg_sdg_counterfactual_rows_are_query_voice() -> None:
    rows = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="counterfactual", n_rows=12),
    )

    assert {row.voice for row in rows} == {"query"}
    assert _families(rows)["MODAL_HYPOTHETICAL"] > 0


def test_generate_ctlg_sdg_examples_is_seed_deterministic() -> None:
    first = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="query", n_rows=12, seed=99),
    )
    second = generate_ctlg_sdg_examples(
        CTLGSDGRequest(domain="software_dev", voice="query", n_rows=12, seed=99),
    )

    assert [row.text for row in first] == [row.text for row in second]


def test_generate_ctlg_sdg_examples_rejects_bad_request() -> None:
    with pytest.raises(ValueError, match="n_rows"):
        generate_ctlg_sdg_examples(
            CTLGSDGRequest(domain="software_dev", voice="query", n_rows=0),
        )
