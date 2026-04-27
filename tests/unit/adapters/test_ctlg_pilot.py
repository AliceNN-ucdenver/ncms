"""Unit tests for CTLG pilot generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncms.application.adapters.ctlg import (
    CTLG_PILOT_PRESET_NAMES,
    CTLGGenerationRequest,
    CTLGPilotRequest,
    apply_ctlg_pilot_preset,
    ctlg_pilot_preset_expectation,
    generate_ctlg_pilot,
    load_ctlg_jsonl,
    write_pilot_examples,
)


def _row(text: str, referent: str = "postgres") -> dict:
    return {
        "text": text,
        "tokens": ["What", "did", "we", "use", "before", referent, "?"],
        "cue_tags": ["O", "O", "O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
        "expected_tlg_query": {
            "axis": "temporal",
            "relation": "predecessor",
            "referent": referent.lower(),
        },
    }


@pytest.mark.asyncio
async def test_generate_ctlg_pilot_accumulates_valid_rows_and_diagnostics() -> None:
    calls = 0

    async def fake_call_json(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                _row("What did we use before Postgres?", "Postgres"),
                {
                    "text": "Which database do we use currently?",
                    "tokens": ["Which", "database", "do", "we", "use", "currently", "?"],
                    "cue_tags": ["O", "B-SCOPE", "O", "O", "O", "B-ASK_CURRENT", "O"],
                    "expected_tlg_query": {
                        "axis": "state",
                        "relation": "current",
                        "referent": "database",
                    },
                },
            ]
        return [
            _row("What did we use before Redis?", "Redis"),
            _row("What did we use before Postgres?", "Postgres"),
        ]

    result = await generate_ctlg_pilot(
        CTLGPilotRequest(
            generation=CTLGGenerationRequest(
                domain="software_dev",
                voice="query",
                n_rows=2,
                model="fake/model",
            ),
            target_rows=3,
            batch_size=2,
            max_batches=3,
        ),
        call_json=fake_call_json,
    )

    assert len(result.examples) == 2
    assert result.rows_seen == 6
    assert result.valid_yield == pytest.approx(2 / 6)
    assert not result.hit_target
    assert result.to_json()["by_diagnostic_code"] == {
        "expected_tlg.mismatch": 1,
        "pilot.duplicate_text": 3,
    }
    assert result.to_json()["by_expected_relation"] == {"predecessor": 2}


@pytest.mark.asyncio
async def test_generate_ctlg_pilot_stops_when_target_is_hit(tmp_path: Path) -> None:
    async def fake_call_json(*_args):
        return [
            _row("What did we use before Postgres?", "Postgres"),
            _row("What did we use before Redis?", "Redis"),
        ]

    result = await generate_ctlg_pilot(
        CTLGPilotRequest(
            generation=CTLGGenerationRequest(
                domain="software_dev",
                voice="query",
                n_rows=2,
                model="fake/model",
            ),
            target_rows=2,
            batch_size=2,
            max_batches=5,
        ),
        call_json=fake_call_json,
    )
    out = tmp_path / "pilot.jsonl"
    report = tmp_path / "pilot_report.json"

    write_pilot_examples(result, out)
    report.write_text(json.dumps(result.to_json()), encoding="utf-8")

    assert result.hit_target
    assert len(result.batches) == 1
    assert len(load_ctlg_jsonl(out)) == 2
    assert json.loads(report.read_text(encoding="utf-8"))["n_valid"] == 2


@pytest.mark.asyncio
async def test_generate_ctlg_pilot_rejects_invalid_request() -> None:
    async def fake_call_json(*_args):
        return []

    with pytest.raises(ValueError, match="target_rows"):
        await generate_ctlg_pilot(
            CTLGPilotRequest(
                generation=CTLGGenerationRequest(
                    domain="software_dev",
                    voice="query",
                    n_rows=1,
                    model="fake/model",
                ),
                target_rows=0,
            ),
            call_json=fake_call_json,
        )


def test_apply_ctlg_pilot_preset_merges_focus_and_examples() -> None:
    request = CTLGGenerationRequest(
        domain="software_dev",
        voice="query",
        n_rows=4,
        model="fake/model",
        focus="avoid duplicate surfaces",
        examples=("Custom example?",),
    )

    updated = apply_ctlg_pilot_preset(request, "current")

    assert "current-state query rows" in updated.focus
    assert "Additional focus: avoid duplicate surfaces" in updated.focus
    assert updated.examples[0] == "Currently, which database do we use?"
    assert updated.examples[-1] == "Custom example?"
    assert "current" in CTLG_PILOT_PRESET_NAMES
    assert ctlg_pilot_preset_expectation("current") == ("state", "current")


def test_apply_ctlg_pilot_preset_rejects_unknown_name() -> None:
    request = CTLGGenerationRequest(
        domain="software_dev",
        voice="query",
        n_rows=4,
        model="fake/model",
    )

    with pytest.raises(ValueError, match="unknown CTLG pilot preset"):
        apply_ctlg_pilot_preset(request, "not_real")


@pytest.mark.asyncio
async def test_generate_ctlg_pilot_rejects_off_preset_relation() -> None:
    async def fake_call_json(*_args):
        return [
            {
                "text": "When did Redis become faster than Memcached?",
                "tokens": [
                    "When",
                    "did",
                    "Redis",
                    "become",
                    "faster",
                    "than",
                    "Memcached",
                    "?",
                ],
                "cue_tags": [
                    "O",
                    "O",
                    "B-REFERENT",
                    "O",
                    "B-TEMPORAL_BEFORE",
                    "O",
                    "B-REFERENT",
                    "O",
                ],
                "expected_tlg_query": {
                    "axis": "temporal",
                    "relation": "before_named",
                    "referent": "redis",
                    "secondary": "memcached",
                },
            }
        ]

    result = await generate_ctlg_pilot(
        CTLGPilotRequest(
            generation=CTLGGenerationRequest(
                domain="software_dev",
                voice="query",
                n_rows=1,
                model="fake/model",
            ),
            target_rows=1,
            batch_size=1,
            max_batches=1,
            required_axis="causal",
            required_relation="cause_of",
        ),
        call_json=fake_call_json,
    )

    assert len(result.examples) == 0
    assert result.diagnostics[0].code == "pilot.off_preset_axis"
