"""Unit tests for CTLG corpus generation helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncms.application.adapters.ctlg import (
    CTLGGenerationRequest,
    generate_ctlg_examples,
    load_ctlg_jsonl,
    write_generation_result,
)


async def _fake_call_json(
    prompt: str,
    model: str,
    api_base: str | None,
    max_tokens: int,
    temperature: float,
):
    assert "Generate exactly 2 CTLG cue-tagging rows" in prompt
    assert "expected_tlg_query" in prompt
    assert model == "fake/model"
    assert api_base is None
    assert max_tokens == 4000
    assert temperature == 0.4
    return [
        {
            "text": "What happened before Postgres?",
            "tokens": ["What", "happened", "before", "Postgres", "?"],
            "cue_tags": ["O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
            "voice": "wrong",
            "domain": "wrong",
            "split": "wrong",
            "source": "wrong",
            "expected_tlg_query": {
                "axis": "temporal",
                "relation": "predecessor",
                "referent": "postgres",
            },
        },
        {
            "text": "If we had kept MySQL, what would be current?",
            "tokens": [
                "If",
                "we",
                "had",
                "kept",
                "MySQL",
                ",",
                "what",
                "would",
                "be",
                "current",
                "?",
            ],
            "cue_tags": [
                "B-MODAL_HYPOTHETICAL",
                "O",
                "O",
                "O",
                "B-REFERENT",
                "O",
                "O",
                "O",
                "O",
                "B-ASK_CURRENT",
                "O",
            ],
            "expected_tlg_query": {
                "axis": "modal",
                "relation": "would_be_current_if",
                "referent": "mysql",
                "scenario": "preserve_mysql",
            },
        },
    ]


@pytest.mark.asyncio
async def test_generate_ctlg_examples_coerces_metadata_and_validates() -> None:
    result = await generate_ctlg_examples(
        CTLGGenerationRequest(
            domain="software_dev",
            voice="counterfactual",
            n_rows=2,
            model="fake/model",
            focus="modal query examples",
        ),
        call_json=_fake_call_json,
    )

    assert result.is_valid
    assert result.raw_rows_seen == 2
    assert [ex.voice for ex in result.examples] == ["query", "query"]
    assert {ex.domain for ex in result.examples} == {"software_dev"}
    assert {ex.split for ex in result.examples} == {"llm"}
    assert {ex.source for ex in result.examples} == {"llm_generated"}


@pytest.mark.asyncio
async def test_generate_ctlg_examples_reports_invalid_rows() -> None:
    async def bad_call_json(*_args):
        return [{"text": "bad", "tokens": ["bad"]}]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=bad_call_json,
    )

    assert not result.is_valid
    assert len(result.examples) == 0
    assert len(result.diagnostics) == 1


@pytest.mark.asyncio
async def test_generate_ctlg_examples_normalizes_llm_tokenization() -> None:
    async def punctuation_call_json(*_args):
        return [
            {
                "text": "What happened before Postgres?",
                "tokens": ["What", "happened", "before", "Postgres?"],
                "cue_tags": ["O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "predecessor",
                    "referent": "postgres",
                },
            },
            {
                "text": "What changed after Redis?",
                "tokens": ["What", "changed", "after", "Redis", "?"],
                "cue_tags": [
                    "O",
                    "O",
                    "B-TEMPORAL_AFTER",
                    "B-REFERENT",
                    "O",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "after_named",
                    "referent": "redis",
                },
            },
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=2, model="fake/model"),
        call_json=punctuation_call_json,
    )

    assert result.is_valid
    assert result.examples[0].tokens == ("What", "happened", "before", "Postgres", "?")
    assert result.examples[0].cue_tags == (
        "O",
        "O",
        "B-TEMPORAL_BEFORE",
        "B-REFERENT",
        "O",
    )
    assert result.examples[1].tokens == ("What", "changed", "after", "Redis", "?")
    assert result.examples[1].cue_tags == (
        "O",
        "O",
        "B-TEMPORAL_AFTER",
        "B-REFERENT",
        "O",
    )


@pytest.mark.asyncio
async def test_generate_ctlg_examples_normalizes_label_aliases_and_trims() -> None:
    async def alias_call_json(*_args):
        return [
            {
                "text": "Why did we move to Postgres?",
                "tokens": ["Why", "did", "we", "move", "to", "Postgres", "?"],
                "cue_tags": ["B-ASK_CAUSE", "O", "O", "O", "O", "B-REFERENT", "O"],
                "expected_tlg_query": {
                    "axis": "causal",
                    "relation": "cause_of",
                    "referent": "postgres",
                },
            },
            {
                "text": "What is current?",
                "tokens": ["What", "is", "current", "?"],
                "cue_tags": ["O", "O", "B-ASK_CURRENT", "O"],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                },
            },
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=alias_call_json,
    )

    assert result.is_valid
    assert result.raw_rows_seen == 2
    assert len(result.examples) == 1
    assert result.examples[0].cue_tags[0] == "B-CAUSAL_EXPLICIT"


@pytest.mark.asyncio
async def test_generate_ctlg_examples_falls_back_to_positional_projection() -> None:
    async def unaligned_call_json(*_args):
        return [
            {
                "text": "If Kafka stayed, what would be current?",
                "tokens": ["If", "Apache Kafka", "stayed", "what", "would", "be", "current", "?"],
                "cue_tags": [
                    "B-MODAL_HYPOTHETICAL",
                    "B-REFERENT",
                    "O",
                    "O",
                    "O",
                    "O",
                    "B-ASK_CURRENT",
                    "O",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "modal",
                    "relation": "would_be_current_if",
                    "referent": "kafka",
                    "scenario": "preserve_kafka",
                },
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(
            domain="software_dev",
            voice="counterfactual",
            n_rows=1,
            model="fake/model",
        ),
        call_json=unaligned_call_json,
    )

    assert result.is_valid
    assert result.examples[0].tokens == (
        "If",
        "Kafka",
        "stayed",
        ",",
        "what",
        "would",
        "be",
        "current",
        "?",
    )
    assert len(result.examples[0].cue_tags) == len(result.examples[0].tokens)
    assert result.examples[0].cue_tags[0] == "B-MODAL_HYPOTHETICAL"


@pytest.mark.asyncio
async def test_generate_ctlg_examples_accepts_short_valid_batches() -> None:
    async def short_call_json(*_args):
        return [
            {
                "text": "What is current?",
                "tokens": ["What", "is", "current", "?"],
                "cue_tags": ["O", "O", "B-ASK_CURRENT", "O"],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                },
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=2, model="fake/model"),
        call_json=short_call_json,
    )

    assert result.is_valid
    assert result.raw_rows_seen == 1
    assert len(result.examples) == 1


def test_write_generation_result_rejects_diagnostics(tmp_path: Path) -> None:
    result = type(
        "FakeResult",
        (),
        {"diagnostics": (object(),), "examples": ()},
    )()

    with pytest.raises(ValueError, match="diagnostics"):
        write_generation_result(result, tmp_path / "out.jsonl")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_generation_result_round_trips(tmp_path: Path) -> None:
    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=2, model="fake/model"),
        call_json=_fake_call_json,
    )
    path = tmp_path / "ctlg.jsonl"

    write_generation_result(result, path)

    loaded = load_ctlg_jsonl(path)
    assert len(loaded) == 2
    assert loaded[0].cue_tags[2] == "B-TEMPORAL_BEFORE"
    assert loaded[0].expected_tlg_query is not None
    assert loaded[0].expected_tlg_query.relation == "predecessor"


@pytest.mark.asyncio
async def test_generate_ctlg_examples_requires_expected_tlg_for_query_rows() -> None:
    async def missing_expected_call_json(*_args):
        return [
            {
                "text": "What happened before Postgres?",
                "tokens": ["What", "happened", "before", "Postgres", "?"],
                "cue_tags": ["O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=missing_expected_call_json,
    )

    assert not result.is_valid
    assert result.diagnostics[0].code == "expected_tlg.missing"


@pytest.mark.asyncio
async def test_generate_ctlg_examples_rejects_expected_tlg_mismatch() -> None:
    async def mismatch_call_json(*_args):
        return [
            {
                "text": "What happened before Postgres?",
                "tokens": ["What", "happened", "before", "Postgres", "?"],
                "cue_tags": ["O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "before_named",
                    "referent": "postgres",
                },
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=mismatch_call_json,
    )

    assert not result.is_valid
    assert result.diagnostics[0].code == "expected_tlg.mismatch"
    assert "actual=" in result.diagnostics[0].message
    assert "What happened before Postgres?" in result.diagnostics[0].message


@pytest.mark.asyncio
async def test_generate_ctlg_examples_fills_expected_tlg_omissions_from_grammar() -> None:
    async def omitted_expected_call_json(*_args):
        return [
            {
                "text": "What did we use before Postgres?",
                "tokens": ["What", "did", "we", "use", "before", "Postgres", "?"],
                "cue_tags": ["O", "O", "O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "predecessor",
                },
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=omitted_expected_call_json,
    )

    assert result.is_valid
    assert result.examples[0].expected_tlg_query is not None
    assert result.examples[0].expected_tlg_query.referent == "postgres"


@pytest.mark.asyncio
async def test_generate_ctlg_examples_strips_ask_current_from_question_word() -> None:
    async def noisy_call_json(*_args):
        return [
            {
                "text": "What is the current database?",
                "tokens": ["What", "is", "the", "current", "database", "?"],
                "cue_tags": ["B-ASK_CURRENT", "O", "O", "B-ASK_CURRENT", "B-SCOPE", "O"],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                    "scope": "database",
                },
            }
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=1, model="fake/model"),
        call_json=noisy_call_json,
    )

    assert result.is_valid
    assert result.examples[0].cue_tags == (
        "O",
        "O",
        "O",
        "B-ASK_CURRENT",
        "B-SCOPE",
        "O",
    )


@pytest.mark.asyncio
async def test_generate_ctlg_examples_cleans_common_llm_cue_noise() -> None:
    async def noisy_call_json(*_args):
        return [
            {
                "text": "Why did Kafka replace RabbitMQ?",
                "tokens": ["Why", "did", "Kafka", "replace", "RabbitMQ", "?"],
                "cue_tags": [
                    "B-ASK_CHANGE",
                    "O",
                    "B-REFERENT",
                    "B-CAUSAL_EXPLICIT",
                    "B-REFERENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "causal",
                    "relation": "cause_of",
                    "referent": "kafka",
                    "secondary": "rabbitmq",
                },
            },
            {
                "text": "Which version of Kubernetes changed after the rollout?",
                "tokens": [
                    "Which",
                    "version",
                    "of",
                    "Kubernetes",
                    "changed",
                    "after",
                    "the",
                    "rollout",
                    "?",
                ],
                "cue_tags": [
                    "O",
                    "B-REFERENT",
                    "O",
                    "B-REFERENT",
                    "O",
                    "B-TEMPORAL_AFTER",
                    "O",
                    "O",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "after_named",
                    "referent": "kubernetes",
                    "scope": "version",
                },
            },
            {
                "text": "What would have been the state of OAuth if we had used JWT?",
                "tokens": [
                    "What",
                    "would",
                    "have",
                    "been",
                    "the",
                    "state",
                    "of",
                    "OAuth",
                    "if",
                    "we",
                    "had",
                    "used",
                    "JWT",
                    "?",
                ],
                "cue_tags": [
                    "O",
                    "B-MODAL_HYPOTHETICAL",
                    "I-MODAL_HYPOTHETICAL",
                    "I-MODAL_HYPOTHETICAL",
                    "O",
                    "B-REFERENT",
                    "O",
                    "B-REFERENT",
                    "O",
                    "O",
                    "O",
                    "B-REFERENT",
                    "B-REFERENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "modal",
                    "relation": "would_be_current_if",
                    "referent": "oauth",
                    "scope": "state",
                    "scenario": "preserve_oauth",
                },
            },
            {
                "text": "What database are we using today?",
                "tokens": ["What", "database", "are", "we", "using", "today", "?"],
                "cue_tags": [
                    "O",
                    "B-ASK_CURRENT",
                    "O",
                    "O",
                    "O",
                    "B-ASK_CURRENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                    "scope": "database",
                },
            },
            {
                "text": "Which database is currently used?",
                "tokens": ["Which", "database", "is", "currently", "used", "?"],
                "cue_tags": ["B-REFERENT", "B-SCOPE", "O", "B-ASK_CURRENT", "O", "O"],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                    "scope": "database",
                },
            },
            {
                "text": "What database version are we using today?",
                "tokens": ["What", "database", "version", "are", "we", "using", "today", "?"],
                "cue_tags": [
                    "O",
                    "B-SCOPE",
                    "B-SCOPE",
                    "O",
                    "O",
                    "O",
                    "B-ASK_CURRENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "state",
                    "relation": "current",
                    "scope": "database",
                },
            },
            {
                "text": "Which cache preceded Redis?",
                "tokens": ["Which", "cache", "preceded", "Redis", "?"],
                "cue_tags": [
                    "B-ORDINAL_NTH",
                    "B-SCOPE",
                    "B-TEMPORAL_BEFORE",
                    "B-REFERENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "predecessor",
                    "referent": "redis",
                    "scope": "cache",
                },
            },
            {
                "text": "Why did Postgres replace MySQL?",
                "tokens": ["Why", "did", "Postgres", "replace", "MySQL", "?"],
                "cue_tags": ["O", "O", "B-REFERENT", "O", "B-REFERENT", "O"],
                "expected_tlg_query": {
                    "axis": "causal",
                    "relation": "cause_of",
                    "referent": "postgres",
                    "secondary": "mysql",
                },
            },
            {
                "text": "Why did Kubernetes replace Docker Swarm?",
                "tokens": [
                    "Why",
                    "did",
                    "Kubernetes",
                    "replace",
                    "Docker",
                    "Swarm",
                    "?",
                ],
                "cue_tags": [
                    "O",
                    "O",
                    "B-REFERENT",
                    "B-CAUSAL_EXPLICIT",
                    "B-REFERENT",
                    "B-REFERENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "causal",
                    "relation": "cause_of",
                    "referent": "kubernetes",
                    "secondary": "docker swarm",
                },
            },
        ]

    result = await generate_ctlg_examples(
        CTLGGenerationRequest(domain="software_dev", voice="query", n_rows=9, model="fake/model"),
        call_json=noisy_call_json,
    )

    assert result.is_valid
    assert result.examples[0].cue_tags[0] == "O"
    assert result.examples[1].cue_tags[1] == "B-SCOPE"
    assert result.examples[2].cue_tags[5] == "B-SCOPE"
    assert result.examples[2].cue_tags[11] == "O"
    assert result.examples[3].cue_tags[1] == "B-SCOPE"
    assert result.examples[3].cue_tags[5] == "B-ASK_CURRENT"
    assert result.examples[4].cue_tags[0] == "O"
    assert result.examples[5].cue_tags[1:3] == ("B-SCOPE", "O")
    assert result.examples[6].cue_tags[0] == "O"
    assert result.examples[7].cue_tags[3] == "B-CAUSAL_EXPLICIT"
    assert result.examples[8].cue_tags[4:6] == ("B-REFERENT", "I-REFERENT")
