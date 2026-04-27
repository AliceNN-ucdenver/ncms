"""CTLG validation harness modes.

The harness is intentionally separate from live search orchestration.  It lets
us test the CTLG adapter, synthesizer, dispatcher, and composition invariant
without allowing CTLG to mutate production ranking until shadow traces justify
turning it on.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from ncms.application.ctlg import extract_ctlg_cues, payload_to_tagged_tokens
from ncms.domain.tlg.composition import compose
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.grammar import LGTrace
from ncms.domain.tlg.semantic_parser import TLGQuery, synthesize

CTLGHarnessMode = Literal[
    "gold_cues",
    "adapter_only",
    "ctlg_shadow",
    "candidate_grounded_ctlg_shadow",
]
RetrieveLGFn = Callable[..., Awaitable[LGTrace] | LGTrace]
ResolveIdFn = Callable[[str], Awaitable[str | None] | str | None]
CuePayload = Sequence[TaggedToken] | Sequence[Mapping[str, Any]]
CandidateSubjects = Mapping[str, str | None] | Sequence[tuple[str, str | None]]


@dataclass(frozen=True)
class CTLGHarnessResult:
    """Single-query CTLG harness diagnostic result."""

    mode: CTLGHarnessMode
    query: str
    cue_tags: tuple[TaggedToken, ...] = ()
    tlg_query: TLGQuery | None = None
    trace: LGTrace | None = None
    bm25_ranking: tuple[str, ...] = ()
    proposed_ranking: tuple[str, ...] = ()
    would_compose: bool = False
    synthesizer_confidence: float | None = None
    grammar_confidence: str | None = None
    abstention_reason: str = ""
    grounding_subject: str | None = None
    grounding_candidate: str | None = None
    candidate_attempts: tuple[Mapping[str, Any], ...] = ()

    @property
    def ranking_changed(self) -> bool:
        """True when the shadow CTLG ranking differs from the baseline ranking."""
        return self.proposed_ranking != self.bm25_ranking


def serialize_harness_result(
    result: CTLGHarnessResult,
    *,
    gold_ids: Sequence[str] = (),
    id_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a harness result into stable benchmark diagnostics.

    ``id_map`` is useful for benchmark layers that need to translate NCMS
    internal IDs into corpus IDs before writing JSONL.  Unknown IDs are
    preserved so diagnostics do not hide grammar answers that lack a corpus
    mapping.
    """

    def _map_id(value: str) -> str:
        return id_map.get(value, value) if id_map is not None else value

    bm25 = tuple(_map_id(mid) for mid in result.bm25_ranking)
    proposed = tuple(_map_id(mid) for mid in result.proposed_ranking)
    accepted = {_map_id(mid) for mid in gold_ids if mid}

    def _rank(ids: Sequence[str]) -> int | None:
        if not accepted:
            return None
        for idx, mid in enumerate(ids, start=1):
            if mid in accepted:
                return idx
        return None

    tlg_query = result.tlg_query
    trace = result.trace
    return {
        "mode": result.mode,
        "cue_tags": [
            {
                "char_start": tok.char_start,
                "char_end": tok.char_end,
                "surface": tok.surface,
                "cue_label": tok.cue_label,
                "confidence": tok.confidence,
            }
            for tok in result.cue_tags
        ],
        "tlg_query": (
            {
                "axis": tlg_query.axis,
                "relation": tlg_query.relation,
                "referent": tlg_query.referent,
                "subject": tlg_query.subject,
                "scope": tlg_query.scope,
                "depth": tlg_query.depth,
                "scenario": tlg_query.scenario,
                "temporal_anchor": tlg_query.temporal_anchor,
                "confidence": tlg_query.confidence,
                "matched_rule": tlg_query.matched_rule,
            }
            if tlg_query is not None
            else None
        ),
        "synthesizer_rule": tlg_query.matched_rule if tlg_query is not None else None,
        "synthesizer_confidence": result.synthesizer_confidence,
        "grammar_confidence": result.grammar_confidence,
        "grammar_answer": _map_id(trace.grammar_answer) if trace and trace.grammar_answer else None,
        "zone_context": [_map_id(mid) for mid in trace.zone_context] if trace else [],
        "causal_edges_traversed": [
            {
                "source_id": _map_id(str(edge.get("source_id", ""))),
                "target_id": _map_id(str(edge.get("target_id", ""))),
                "edge_type": str(edge.get("edge_type", "")),
            }
            for edge in (trace.causal_edges_traversed if trace else [])
        ],
        "would_compose": result.would_compose,
        "ranking_changed": result.ranking_changed,
        "rank_before": _rank(bm25),
        "rank_after": _rank(proposed),
        "gold_in_candidates": bool(set(bm25) & accepted) if accepted else None,
        "bm25_ranking": list(bm25),
        "proposed_ranking": list(proposed),
        "abstention_reason": result.abstention_reason,
        "grounding_subject": result.grounding_subject,
        "grounding_candidate": (
            _map_id(result.grounding_candidate) if result.grounding_candidate else None
        ),
        "candidate_attempts": [
            {
                "candidate": (
                    _map_id(str(attempt.get("candidate")))
                    if attempt.get("candidate")
                    else None
                ),
                "subject": attempt.get("subject"),
                "confidence": attempt.get("confidence"),
                "grammar_answer": (
                    _map_id(str(attempt.get("grammar_answer")))
                    if attempt.get("grammar_answer")
                    else None
                ),
                "would_compose": bool(attempt.get("would_compose")),
                "answer_in_ranking": bool(attempt.get("answer_in_ranking")),
                "answer_in_allowed_ids": bool(attempt.get("answer_in_allowed_ids")),
                "proof": attempt.get("proof"),
            }
            for attempt in result.candidate_attempts
        ],
    }


def _coerce_cue_tags(cue_tags: CuePayload | None) -> tuple[TaggedToken, ...]:
    if not cue_tags:
        return ()
    items = list(cue_tags)
    if all(isinstance(item, TaggedToken) for item in items):
        return tuple(item for item in items if isinstance(item, TaggedToken))
    return tuple(payload_to_tagged_tokens(items))


async def _dispatch(
    retrieve_lg_fn: RetrieveLGFn,
    query: str,
    tlg_query: TLGQuery,
) -> LGTrace:
    result = retrieve_lg_fn(query, tlg_query=tlg_query)
    if inspect.isawaitable(result):
        return await result
    return result


async def _resolve_trace_ids(
    trace: LGTrace,
    resolve_id_fn: ResolveIdFn | None,
) -> LGTrace:
    """Resolve grammar node IDs to memory IDs for benchmark composition."""
    if resolve_id_fn is None:
        return trace

    async def _resolve(value: str | None) -> str | None:
        if value is None:
            return None
        resolved = resolve_id_fn(value)
        if inspect.isawaitable(resolved):
            return await resolved
        return resolved

    grammar_answer = await _resolve(trace.grammar_answer)
    zone_context = [
        resolved
        for resolved in [await _resolve(node_id) for node_id in trace.zone_context]
        if resolved is not None
    ]
    return replace(
        trace,
        grammar_answer=grammar_answer,
        zone_context=zone_context,
    )


def _result_from_parts(
    *,
    mode: CTLGHarnessMode,
    query: str,
    cue_tags: tuple[TaggedToken, ...],
    tlg_query: TLGQuery | None,
    trace: LGTrace | None = None,
    bm25_ranking: Sequence[str] = (),
    abstention_reason: str = "",
) -> CTLGHarnessResult:
    baseline = tuple(bm25_ranking)
    proposed = tuple(compose(baseline, trace)) if trace is not None else baseline
    would_compose = trace.has_confident_answer() if trace is not None else False
    return CTLGHarnessResult(
        mode=mode,
        query=query,
        cue_tags=cue_tags,
        tlg_query=tlg_query,
        trace=trace,
        bm25_ranking=baseline,
        proposed_ranking=proposed,
        would_compose=would_compose,
        synthesizer_confidence=tlg_query.confidence if tlg_query is not None else None,
        grammar_confidence=str(trace.confidence) if trace is not None else None,
        abstention_reason=abstention_reason,
    )


def _candidate_subject_items(
    candidate_subjects: CandidateSubjects,
    ranking: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Return candidate subjects in ranking order, de-duped by subject."""
    if isinstance(candidate_subjects, Mapping):
        raw = [(mid, candidate_subjects.get(mid)) for mid in ranking]
    else:
        raw = list(candidate_subjects)

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for candidate, subject in raw:
        if not candidate or not subject:
            continue
        norm_subject = str(subject).strip()
        if not norm_subject or norm_subject in seen:
            continue
        seen.add(norm_subject)
        ordered.append((str(candidate), norm_subject))
    return tuple(ordered)


async def run_gold_cues(
    query: str,
    cue_tags: CuePayload,
    *,
    retrieve_lg_fn: RetrieveLGFn | None = None,
    resolve_id_fn: ResolveIdFn | None = None,
    bm25_ranking: Sequence[str] = (),
) -> CTLGHarnessResult:
    """Bypass the adapter and validate synthesizer plus optional dispatcher."""
    tags = _coerce_cue_tags(cue_tags)
    tlg_query = synthesize(tags)
    if tlg_query is None:
        return _result_from_parts(
            mode="gold_cues",
            query=query,
            cue_tags=tags,
            tlg_query=None,
            bm25_ranking=bm25_ranking,
            abstention_reason="synthesizer_no_match",
        )
    if retrieve_lg_fn is None:
        return _result_from_parts(
            mode="gold_cues",
            query=query,
            cue_tags=tags,
            tlg_query=tlg_query,
            bm25_ranking=bm25_ranking,
        )
    trace = await _resolve_trace_ids(
        await _dispatch(retrieve_lg_fn, query, tlg_query),
        resolve_id_fn,
    )
    return _result_from_parts(
        mode="gold_cues",
        query=query,
        cue_tags=tags,
        tlg_query=tlg_query,
        trace=trace,
        bm25_ranking=bm25_ranking,
    )


async def run_adapter_only(
    query: str,
    cue_tagger: object | None,
    *,
    domain: str,
) -> CTLGHarnessResult:
    """Run only the CTLG adapter and synthesizer; retrieval is not touched."""
    extraction = await extract_ctlg_cues(cue_tagger, query, domain=domain)
    tags = tuple(extraction.tokens)
    tlg_query = synthesize(tags)
    return _result_from_parts(
        mode="adapter_only",
        query=query,
        cue_tags=tags,
        tlg_query=tlg_query,
        abstention_reason="" if tlg_query is not None else "synthesizer_no_match",
    )


async def run_ctlg_shadow(
    query: str,
    *,
    retrieve_lg_fn: RetrieveLGFn,
    bm25_ranking: Sequence[str],
    domain: str = "",
    cue_tagger: object | None = None,
    cue_tags: CuePayload | None = None,
    tlg_query: TLGQuery | None = None,
    resolve_id_fn: ResolveIdFn | None = None,
) -> CTLGHarnessResult:
    """Run CTLG end to end and report the proposed composition without mutation."""
    tags = _coerce_cue_tags(cue_tags)
    derived_query = tlg_query

    if derived_query is None:
        if not tags:
            extraction = await extract_ctlg_cues(cue_tagger, query, domain=domain)
            tags = tuple(extraction.tokens)
        derived_query = synthesize(tags)

    if derived_query is None:
        return _result_from_parts(
            mode="ctlg_shadow",
            query=query,
            cue_tags=tags,
            tlg_query=None,
            bm25_ranking=bm25_ranking,
            abstention_reason="synthesizer_no_match",
        )

    trace = await _resolve_trace_ids(
        await _dispatch(retrieve_lg_fn, query, derived_query),
        resolve_id_fn,
    )
    return _result_from_parts(
        mode="ctlg_shadow",
        query=query,
        cue_tags=tags,
        tlg_query=derived_query,
        trace=trace,
        bm25_ranking=bm25_ranking,
    )


async def run_candidate_grounded_ctlg_shadow(
    query: str,
    *,
    retrieve_lg_fn: RetrieveLGFn,
    bm25_ranking: Sequence[str],
    candidate_subjects: CandidateSubjects,
    domain: str = "",
    cue_tagger: object | None = None,
    cue_tags: CuePayload | None = None,
    tlg_query: TLGQuery | None = None,
    resolve_id_fn: ResolveIdFn | None = None,
    override_existing_subject: bool = False,
    require_answer_in_ranking: bool = False,
    allowed_answer_ids: Sequence[str] | None = None,
) -> CTLGHarnessResult:
    """Run CTLG shadow by grounding the TLGQuery against ranked candidates.

    The regular CTLG shadow asks the grammar to resolve a query globally.  That
    fails for deictic queries such as "this decision" because the query has no
    standalone subject.  Candidate-grounded shadow keeps the same cue-derived
    relation, then tries candidate subjects in baseline ranking order and
    chooses the first confident grammar trace.  It is still shadow-only: the
    returned ``proposed_ranking`` is diagnostic and never mutates live search.
    """
    tags = _coerce_cue_tags(cue_tags)
    derived_query = tlg_query

    if derived_query is None:
        if not tags:
            extraction = await extract_ctlg_cues(cue_tagger, query, domain=domain)
            tags = tuple(extraction.tokens)
        derived_query = synthesize(tags)

    baseline = tuple(bm25_ranking)
    if derived_query is None:
        return _result_from_parts(
            mode="candidate_grounded_ctlg_shadow",
            query=query,
            cue_tags=tags,
            tlg_query=None,
            bm25_ranking=baseline,
            abstention_reason="synthesizer_no_match",
        )

    candidates = _candidate_subject_items(candidate_subjects, baseline)
    if not candidates:
        return _result_from_parts(
            mode="candidate_grounded_ctlg_shadow",
            query=query,
            cue_tags=tags,
            tlg_query=derived_query,
            bm25_ranking=baseline,
            abstention_reason="no_candidate_subjects",
        )

    selected_trace: LGTrace | None = None
    selected_query: TLGQuery | None = None
    selected_candidate: str | None = None
    selected_subject: str | None = None
    first_trace: LGTrace | None = None
    attempts: list[Mapping[str, Any]] = []
    baseline_set = set(baseline)
    allowed_answer_set = set(allowed_answer_ids) if allowed_answer_ids is not None else baseline_set

    for candidate_id, subject in candidates:
        grounded_query = (
            replace(derived_query, subject=subject)
            if override_existing_subject or derived_query.subject is None
            else derived_query
        )
        trace = await _resolve_trace_ids(
            await _dispatch(retrieve_lg_fn, query, grounded_query),
            resolve_id_fn,
        )
        if first_trace is None:
            first_trace = trace
        would_compose = trace.has_confident_answer()
        answer_in_ranking = bool(trace.grammar_answer and trace.grammar_answer in baseline_set)
        answer_in_allowed_ids = bool(
            trace.grammar_answer and trace.grammar_answer in allowed_answer_set
        )
        attempts.append(
            {
                "candidate": candidate_id,
                "subject": subject,
                "confidence": str(trace.confidence),
                "grammar_answer": trace.grammar_answer,
                "would_compose": would_compose,
                "answer_in_ranking": answer_in_ranking,
                "answer_in_allowed_ids": answer_in_allowed_ids,
                "proof": trace.proof,
            }
        )
        if would_compose and (answer_in_allowed_ids or not require_answer_in_ranking):
            selected_trace = trace
            selected_query = grounded_query
            selected_candidate = candidate_id
            selected_subject = subject
            break

    result = _result_from_parts(
        mode="candidate_grounded_ctlg_shadow",
        query=query,
        cue_tags=tags,
        tlg_query=selected_query or derived_query,
        trace=selected_trace or (None if require_answer_in_ranking else first_trace),
        bm25_ranking=baseline,
        abstention_reason="" if selected_trace is not None else "candidate_dispatch_abstained",
    )
    return replace(
        result,
        grounding_subject=selected_subject,
        grounding_candidate=selected_candidate,
        candidate_attempts=tuple(attempts),
    )


__all__ = [
    "CTLGHarnessMode",
    "CTLGHarnessResult",
    "CuePayload",
    "ResolveIdFn",
    "RetrieveLGFn",
    "run_adapter_only",
    "run_candidate_grounded_ctlg_shadow",
    "run_ctlg_shadow",
    "run_gold_cues",
    "serialize_harness_result",
]
