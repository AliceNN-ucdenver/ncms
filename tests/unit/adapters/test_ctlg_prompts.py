"""Unit tests for CTLG prompt builders."""

from __future__ import annotations

import pytest

from ncms.application.adapters.ctlg import (
    CTLGPromptSpec,
    build_generation_prompt,
    build_judge_prompt,
)


def test_generation_prompt_query_voice_contains_schema_and_labels() -> None:
    prompt = build_generation_prompt(
        CTLGPromptSpec(
            domain="software_dev",
            voice="query",
            n_rows=3,
            focus="before/after technology replacement questions",
            examples=("What did we use before Postgres?",),
        ),
    )

    assert "Generate exactly 3 CTLG cue-tagging rows" in prompt
    assert "software_dev" in prompt
    assert "B-CAUSAL_EXPLICIT" in prompt
    assert "B-TEMPORAL_BEFORE" in prompt
    assert "tokens and cue_tags must have the same length" in prompt
    assert "expected_tlg_query" in prompt
    assert "Use this exact object shape" in prompt
    assert "before one anchor -> relation=predecessor" in prompt
    assert "Cue-to-query rule map" in prompt
    assert "What did we use before Postgres?" in prompt


def test_generation_prompt_memory_and_counterfactual_are_distinct() -> None:
    memory_prompt = build_generation_prompt(
        CTLGPromptSpec(domain="clinical", voice="memory", n_rows=1),
    )
    counterfactual_prompt = build_generation_prompt(
        CTLGPromptSpec(domain="software_dev", voice="counterfactual", n_rows=1),
    )

    assert "stored memory statements" in memory_prompt
    assert "counterfactual framing" in counterfactual_prompt
    assert "B-MODAL_HYPOTHETICAL" in counterfactual_prompt


def test_generation_prompt_rejects_empty_batch() -> None:
    with pytest.raises(ValueError, match="n_rows"):
        build_generation_prompt(CTLGPromptSpec(domain="software_dev", voice="query", n_rows=0))


def test_judge_prompt_contains_row_and_output_contract() -> None:
    prompt = build_judge_prompt(
        {
            "text": "Why did Postgres replace MySQL?",
            "tokens": ["Why", "did", "Postgres"],
            "cue_tags": ["B-CAUSAL_EXPLICIT", "O", "B-REFERENT"],
            "domain": "software_dev",
            "voice": "query",
        },
    )

    assert "Judge whether this CTLG cue-tagged row is valid training data" in prompt
    assert "0: 'Why' -> 'B-CAUSAL_EXPLICIT'" in prompt
    assert "6. query rows include expected_tlg_query" in prompt
    assert '"verdict":"valid"|"fixable"|"invalid"' in prompt
