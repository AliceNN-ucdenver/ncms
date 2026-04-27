"""Tests for CTLG validation harness modes."""

from __future__ import annotations

import pytest

from ncms.application.adapters.ctlg.harness import (
    CTLGHarnessResult,
    run_adapter_only,
    run_candidate_grounded_ctlg_shadow,
    run_ctlg_shadow,
    run_gold_cues,
    serialize_harness_result,
)
from ncms.domain.tlg.confidence import Confidence
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.grammar import LGIntent, LGTrace
from ncms.domain.tlg.semantic_parser import TLGQuery


def _tokens(pairs: list[tuple[str, str]]) -> list[TaggedToken]:
    tokens: list[TaggedToken] = []
    pos = 0
    for surface, label in pairs:
        tokens.append(
            TaggedToken(
                char_start=pos,
                char_end=pos + len(surface),
                surface=surface,
                cue_label=label,  # type: ignore[arg-type]
                confidence=0.9,
            )
        )
        pos += len(surface) + 1
    return tokens


@pytest.mark.asyncio
async def test_gold_cues_synthesizes_dispatches_and_proposes_composition() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        assert query == "What is our current database?"
        assert tlg_query is not None
        assert tlg_query.axis == "state"
        assert tlg_query.relation == "current"
        return LGTrace(
            query=query,
            intent=LGIntent(kind="current", entity=tlg_query.scope),
            grammar_answer="m2",
            zone_context=["m1"],
            confidence=Confidence.HIGH,
        )

    result = await run_gold_cues(
        "What is our current database?",
        _tokens(
            [
                ("What", "O"),
                ("is", "O"),
                ("our", "O"),
                ("current", "B-ASK_CURRENT"),
                ("database", "B-SCOPE"),
                ("?", "O"),
            ]
        ),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("m0", "m2", "m3"),
    )

    assert result.mode == "gold_cues"
    assert result.tlg_query is not None
    assert result.tlg_query.matched_rule == "state_current"
    assert result.would_compose is True
    assert result.ranking_changed is True
    assert result.proposed_ranking == ("m2", "m1", "m0", "m3")
    assert result.grammar_confidence == "high"


@pytest.mark.asyncio
async def test_adapter_only_runs_cue_tagger_without_dispatch() -> None:
    class StubCueTagger:
        def extract_cues(self, text: str, *, domain: str) -> list[TaggedToken]:
            assert text == "latest database"
            assert domain == "software_dev"
            return _tokens([("latest", "B-ORDINAL_LAST"), ("database", "B-SCOPE")])

    result = await run_adapter_only(
        "latest database",
        StubCueTagger(),
        domain="software_dev",
    )

    assert result.mode == "adapter_only"
    assert result.trace is None
    assert result.tlg_query is not None
    assert result.tlg_query.relation == "last"
    assert result.proposed_ranking == ()
    assert result.abstention_reason == ""


@pytest.mark.asyncio
async def test_shadow_abstains_without_synthesized_query() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        raise AssertionError("shadow mode should not dispatch without a TLGQuery")

    result = await run_ctlg_shadow(
        "ordinary lexical query",
        cue_tags=_tokens([("ordinary", "O"), ("lexical", "O"), ("query", "O")]),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("m0", "m1"),
    )

    assert result.mode == "ctlg_shadow"
    assert result.tlg_query is None
    assert result.trace is None
    assert result.proposed_ranking == ("m0", "m1")
    assert result.would_compose is False
    assert result.abstention_reason == "synthesizer_no_match"


@pytest.mark.asyncio
async def test_shadow_low_confidence_does_not_change_ranking() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        assert tlg_query is not None
        return LGTrace(
            query=query,
            intent=LGIntent(kind="current"),
            grammar_answer="m9",
            confidence=Confidence.LOW,
        )

    result = await run_ctlg_shadow(
        "current database",
        cue_tags=[
            {
                "char_start": 0,
                "char_end": 7,
                "surface": "current",
                "cue_label": "B-ASK_CURRENT",
                "confidence": 0.9,
            },
            {
                "char_start": 8,
                "char_end": 16,
                "surface": "database",
                "cue_label": "B-SCOPE",
                "confidence": 0.9,
            },
        ],
        retrieve_lg_fn=dispatch,
        bm25_ranking=("m0", "m1"),
    )

    assert result.trace is not None
    assert result.grammar_confidence == "low"
    assert result.proposed_ranking == ("m0", "m1")
    assert result.would_compose is False
    assert result.ranking_changed is False


@pytest.mark.asyncio
async def test_candidate_grounded_shadow_tries_ranked_subjects_until_confident() -> None:
    seen_subjects: list[str | None] = []

    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        assert query == "What was first in this decision?"
        assert tlg_query is not None
        seen_subjects.append(tlg_query.subject)
        if tlg_query.subject == "wrong-subject":
            return LGTrace(
                query=query,
                intent=LGIntent(kind="origin", subject=tlg_query.subject),
                confidence=Confidence.ABSTAIN,
                proof="no zone",
            )
        return LGTrace(
            query=query,
            intent=LGIntent(kind="origin", subject=tlg_query.subject),
            grammar_answer="node-hit",
            zone_context=["node-context"],
            confidence=Confidence.HIGH,
            proof=f"origin(subject={tlg_query.subject})",
        )

    result = await run_candidate_grounded_ctlg_shadow(
        "What was first in this decision?",
        cue_tags=_tokens([("first", "B-ORDINAL_FIRST")]),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("cand0", "cand1", "cand2"),
        candidate_subjects={
            "cand0": "wrong-subject",
            "cand1": "right-subject",
            "cand2": "right-subject",
        },
        resolve_id_fn={"node-hit": "mem-hit", "node-context": "mem-context"}.get,
    )

    assert result.mode == "candidate_grounded_ctlg_shadow"
    assert seen_subjects == ["wrong-subject", "right-subject"]
    assert result.grounding_subject == "right-subject"
    assert result.grounding_candidate == "cand1"
    assert result.would_compose is True
    assert result.proposed_ranking == ("mem-hit", "mem-context", "cand0", "cand1", "cand2")
    assert len(result.candidate_attempts) == 2
    assert result.candidate_attempts[0]["would_compose"] is False
    assert result.candidate_attempts[1]["would_compose"] is True


@pytest.mark.asyncio
async def test_candidate_grounded_shadow_reports_missing_candidate_subjects() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        raise AssertionError("no subjects means no dispatch")

    result = await run_candidate_grounded_ctlg_shadow(
        "latest decision",
        cue_tags=_tokens([("latest", "B-ORDINAL_LAST")]),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("cand0",),
        candidate_subjects={},
    )

    assert result.mode == "candidate_grounded_ctlg_shadow"
    assert result.abstention_reason == "no_candidate_subjects"
    assert result.proposed_ranking == ("cand0",)


@pytest.mark.asyncio
async def test_candidate_grounded_shadow_can_require_answer_in_ranking() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        assert tlg_query is not None
        return LGTrace(
            query=query,
            intent=LGIntent(kind="origin", subject=tlg_query.subject),
            grammar_answer="outside-candidate-set",
            confidence=Confidence.HIGH,
        )

    result = await run_candidate_grounded_ctlg_shadow(
        "first decision",
        cue_tags=_tokens([("first", "B-ORDINAL_FIRST")]),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("cand0", "cand1"),
        candidate_subjects={"cand0": "subject-a"},
        require_answer_in_ranking=True,
    )

    assert result.would_compose is False
    assert result.ranking_changed is False
    assert result.abstention_reason == "candidate_dispatch_abstained"
    assert result.candidate_attempts[0]["answer_in_ranking"] is False
    assert result.candidate_attempts[0]["answer_in_allowed_ids"] is False


@pytest.mark.asyncio
async def test_candidate_grounded_shadow_can_accept_answer_from_allowed_pool() -> None:
    async def dispatch(query: str, *, tlg_query: TLGQuery | None = None) -> LGTrace:
        assert tlg_query is not None
        return LGTrace(
            query=query,
            intent=LGIntent(kind="origin", subject=tlg_query.subject),
            grammar_answer="scored-but-not-returned",
            confidence=Confidence.HIGH,
        )

    result = await run_candidate_grounded_ctlg_shadow(
        "first decision",
        cue_tags=_tokens([("first", "B-ORDINAL_FIRST")]),
        retrieve_lg_fn=dispatch,
        bm25_ranking=("cand0", "cand1"),
        candidate_subjects={"cand0": "subject-a"},
        require_answer_in_ranking=True,
        allowed_answer_ids=("cand0", "cand1", "scored-but-not-returned"),
    )

    assert result.would_compose is True
    assert result.ranking_changed is True
    assert result.proposed_ranking == ("scored-but-not-returned", "cand0", "cand1")
    assert result.candidate_attempts[0]["answer_in_ranking"] is False
    assert result.candidate_attempts[0]["answer_in_allowed_ids"] is True


def test_serialize_harness_result_maps_ids_and_reports_gold_ranks() -> None:
    tlg_query = TLGQuery(axis="state", relation="current", scope="database", confidence=0.9)
    trace = LGTrace(
        query="current database",
        intent=LGIntent(kind="current"),
        grammar_answer="ncms2",
        zone_context=["ncms1"],
        causal_edges_traversed=[
            {"source_id": "ncms2", "target_id": "ncms1", "edge_type": "caused_by"}
        ],
        confidence=Confidence.HIGH,
    )
    result = CTLGHarnessResult(
        mode="ctlg_shadow",
        query="current database",
        cue_tags=tuple(_tokens([("current", "B-ASK_CURRENT"), ("database", "B-SCOPE")])),
        tlg_query=tlg_query,
        trace=trace,
        bm25_ranking=("ncms0", "ncms2"),
        proposed_ranking=("ncms2", "ncms1", "ncms0"),
        would_compose=True,
        synthesizer_confidence=0.9,
        grammar_confidence="high",
    )

    payload = serialize_harness_result(
        result,
        gold_ids=("mid2",),
        id_map={"ncms0": "mid0", "ncms1": "mid1", "ncms2": "mid2"},
    )

    assert payload["mode"] == "ctlg_shadow"
    assert payload["tlg_query"]["relation"] == "current"
    assert payload["synthesizer_rule"] == ""
    assert payload["grammar_answer"] == "mid2"
    assert payload["zone_context"] == ["mid1"]
    assert payload["causal_edges_traversed"] == [
        {"source_id": "mid2", "target_id": "mid1", "edge_type": "caused_by"}
    ]
    assert payload["rank_before"] == 2
    assert payload["rank_after"] == 1
    assert payload["gold_in_candidates"] is True
    assert payload["bm25_ranking"] == ["mid0", "mid2"]
    assert payload["proposed_ranking"] == ["mid2", "mid1", "mid0"]
