"""Deterministic CTLG synthetic data generation.

The LLM generator is useful for natural phrasing, but CTLG needs a
controlled source of BIO-legal cue coverage.  This module emits
template-backed query and memory rows with local tokenization and
validation-compatible character offsets.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from ncms.application.adapters.ctlg.corpus import (
    CTLGExample,
    CTLGExpectedQuery,
    CTLGSplit,
    CTLGVoice,
)
from ncms.application.adapters.schemas import DOMAINS, Domain
from ncms.domain.tlg.cue_taxonomy import CueLabel

CTLGSDGVoice = Literal["query", "memory", "counterfactual", "mixed", "mseb_targeted"]
CueType = Literal[
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
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._/#:+-][A-Za-z0-9]+)*|[^\w\s]")
_PUNCT = frozenset({".", ",", "?", ":", ";", ")"})


@dataclass(frozen=True)
class CTLGSDGRequest:
    """Configuration for deterministic CTLG SDG generation."""

    domain: Domain
    n_rows: int
    voice: CTLGSDGVoice = "mixed"
    split: CTLGSplit = "sdg"
    source: str = "sdg_ctlg_template"
    seed: int = 13


@dataclass(frozen=True)
class Segment:
    """One labeled phrase segment before tokenization."""

    text: str
    cue_type: CueType | None = None


@dataclass(frozen=True)
class _RenderedSegment:
    start: int
    end: int
    cue_type: CueType | None


@dataclass(frozen=True)
class _TemplateContext:
    subject: str
    referent: str
    alternative: str
    cause: str
    event: str
    anchor: str
    scope: str
    ordinal: str


TemplateFn = Callable[[_TemplateContext], tuple[CTLGVoice, tuple[Segment, ...], str]]

_SUBJECTS: tuple[str, ...] = (
    "checkout service",
    "auth service",
    "search index",
    "billing worker",
    "ingestion pipeline",
    "memory service",
    "API gateway",
    "graph traversal",
    "replay job",
    "maintenance scheduler",
)
_REFERENTS: tuple[str, ...] = (
    "Postgres",
    "MySQL",
    "Redis",
    "Kafka",
    "SPLADE",
    "BM25",
    "GLiNER",
    "SQLite",
    "Tantivy",
    "NetworkX",
    "temporal index",
    "role head",
)
_CAUSES: tuple[str, ...] = (
    "latency regression",
    "cache outage",
    "schema migration",
    "failed rollout",
    "incident review",
    "adapter retrain",
    "index corruption",
    "rollback",
    "memory leak",
    "feature flag launch",
)
_ANCHORS: tuple[str, ...] = (
    "Q1",
    "the March incident",
    "last release",
    "sprint 18",
    "the migration window",
    "the postmortem",
)
_SCOPES: tuple[str, ...] = (
    "retrieval stack",
    "datastore",
    "classifier",
    "ingestion path",
    "ranking pipeline",
    "adapter registry",
)
_ORDINALS: tuple[str, ...] = ("first", "second", "third")


def _query_after(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What came"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.event, "REFERENT"),
            Segment("for"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("?"),
        ),
        "query_after_change",
    )


def _query_before_current(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What came"),
            Segment("before", "TEMPORAL_BEFORE"),
            Segment(ctx.event, "REFERENT"),
            Segment("in"),
            Segment(ctx.scope, "SCOPE"),
            Segment("?"),
        ),
        "query_before_current",
    )


def _query_since(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What has changed", "ASK_CHANGE"),
            Segment("since", "TEMPORAL_SINCE"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment("for"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("?"),
        ),
        "query_since_change",
    )


def _query_during(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("During", "TEMPORAL_DURING"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment(","),
            Segment("what was happening"),
            Segment("about"),
            Segment(ctx.referent, "REFERENT"),
            Segment("?"),
        ),
        "query_during_change",
    )


def _query_ordinal_first(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What was the"),
            Segment("first", "ORDINAL_FIRST"),
            Segment(ctx.scope, "SCOPE"),
            Segment("choice"),
            Segment("before", "TEMPORAL_BEFORE"),
            Segment(ctx.referent, "REFERENT"),
            Segment("?"),
        ),
        "query_ordinal_first",
    )


def _query_ordinal_last(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What was the"),
            Segment("last", "ORDINAL_LAST"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("state"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.event, "REFERENT"),
            Segment("?"),
        ),
        "query_ordinal_last",
    )


def _query_ordinal_nth(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What was the"),
            Segment(ctx.ordinal, "ORDINAL_NTH"),
            Segment("decision"),
            Segment("during", "TEMPORAL_DURING"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment("?"),
        ),
        "query_ordinal_nth",
    )


def _query_cause(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Why did", "CAUSAL_EXPLICIT"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("move to"),
            Segment(ctx.referent, "REFERENT"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.cause, "REFERENT"),
            Segment("?"),
        ),
        "query_causal_after",
    )


def _query_mseb_current_adopted(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What"),
            Segment("decision", "SCOPE"),
            Segment("was"),
            Segment("adopted", "ASK_CURRENT"),
            Segment("in"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("?"),
        ),
        "mseb_current_adopted",
    )


def _query_mseb_latest_approach(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What"),
            Segment("is"),
            Segment("the"),
            Segment("latest chosen", "ASK_CURRENT"),
            Segment(ctx.scope, "SCOPE"),
            Segment("per this record"),
            Segment("?"),
        ),
        "mseb_current_latest_approach",
    )


def _query_mseb_ordinal_last_section(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Which"),
            Segment("section", "SCOPE"),
            Segment("closed the decision record"),
            Segment("most recently", "ORDINAL_LAST"),
            Segment("?"),
        ),
        "mseb_ordinal_last_section",
    )


def _query_mseb_final_conclusion(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What was the"),
            Segment("final", "ORDINAL_LAST"),
            Segment("conclusion", "SCOPE"),
            Segment("of"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("?"),
        ),
        "mseb_ordinal_last_conclusion",
    )


def _query_mseb_preceded_adopted(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What alternatives"),
            Segment("preceded", "TEMPORAL_BEFORE"),
            Segment("the"),
            Segment("adopted decision", "REFERENT"),
            Segment("?"),
        ),
        "mseb_predecessor_preceded",
    )


def _query_mseb_considered_before_choice(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("What was considered"),
            Segment("before", "TEMPORAL_BEFORE"),
            Segment("the"),
            Segment("current choice", "REFERENT"),
            Segment("was made"),
            Segment("?"),
        ),
        "mseb_predecessor_current_choice",
    )


def _query_mseb_retired_alternatives(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Which"),
            Segment("alternatives", "SCOPE"),
            Segment("were retired", "ASK_CHANGE"),
            Segment("by the"),
            Segment("adopted decision", "REFERENT"),
            Segment("?"),
        ),
        "mseb_retirement_alternatives",
    )


def _query_mseb_trace_context(
    ctx: _TemplateContext,
) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Trace the decision record"),
            Segment("from"),
            Segment("context", "REFERENT"),
            Segment("through", "TEMPORAL_AFTER"),
            Segment("consequences", "SCOPE"),
            Segment("."),
        ),
        "mseb_sequence_context",
    )


def _memory_after_because(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment(ctx.subject, "SUBJECT"),
            Segment("moved from"),
            Segment(ctx.alternative, "REFERENT"),
            Segment("to"),
            Segment(ctx.referent, "REFERENT"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.event, "REFERENT"),
            Segment("because of", "CAUSAL_EXPLICIT"),
            Segment(ctx.cause, "REFERENT"),
            Segment("."),
        ),
        "memory_after_because",
    )


def _memory_before(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment("Before", "TEMPORAL_BEFORE"),
            Segment(ctx.event, "REFERENT"),
            Segment(","),
            Segment(ctx.subject, "SUBJECT"),
            Segment("used"),
            Segment(ctx.alternative, "REFERENT"),
            Segment("for"),
            Segment(ctx.scope, "SCOPE"),
            Segment("."),
        ),
        "memory_before_state",
    )


def _memory_since(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment("Since", "TEMPORAL_SINCE"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment(","),
            Segment(ctx.subject, "SUBJECT"),
            Segment("has used"),
            Segment(ctx.referent, "REFERENT"),
            Segment("in"),
            Segment(ctx.scope, "SCOPE"),
            Segment("."),
        ),
        "memory_since_state",
    )


def _memory_during_due_to(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment("During", "TEMPORAL_DURING"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment(","),
            Segment(ctx.subject, "SUBJECT"),
            Segment("switched to"),
            Segment(ctx.referent, "REFERENT"),
            Segment("due to", "CAUSAL_ALTLEX"),
            Segment(ctx.cause, "REFERENT"),
            Segment("."),
        ),
        "memory_during_altlex",
    )


def _memory_caused(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment(ctx.cause, "REFERENT"),
            Segment("caused", "CAUSAL_EXPLICIT"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("to replace"),
            Segment(ctx.alternative, "REFERENT"),
            Segment("with"),
            Segment(ctx.referent, "REFERENT"),
            Segment("."),
        ),
        "memory_caused_replace",
    )


def _memory_ordinal(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "memory",
        (
            Segment("The"),
            Segment(ctx.ordinal, "ORDINAL_NTH"),
            Segment(ctx.scope, "SCOPE"),
            Segment("decision"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.event, "REFERENT"),
            Segment("selected"),
            Segment(ctx.referent, "REFERENT"),
            Segment("."),
        ),
        "memory_ordinal_decision",
    )


def _counterfactual_if_not(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("If", "MODAL_HYPOTHETICAL"),
            Segment(ctx.event, "REFERENT"),
            Segment("had not happened", "MODAL_HYPOTHETICAL"),
            Segment(","),
            Segment("what would be current", "ASK_CURRENT"),
            Segment("for"),
            Segment(ctx.subject, "SUBJECT"),
            Segment("?"),
        ),
        "counterfactual_if_not",
    )


def _counterfactual_without(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Without", "MODAL_HYPOTHETICAL"),
            Segment(ctx.cause, "REFERENT"),
            Segment(","),
            Segment("what would have changed", "ASK_CHANGE"),
            Segment("in"),
            Segment(ctx.scope, "SCOPE"),
            Segment("?"),
        ),
        "counterfactual_without",
    )


def _counterfactual_had_kept(ctx: _TemplateContext) -> tuple[CTLGVoice, tuple[Segment, ...], str]:
    return (
        "query",
        (
            Segment("Had we kept", "MODAL_HYPOTHETICAL"),
            Segment(ctx.alternative, "REFERENT"),
            Segment(","),
            Segment("what would be current", "ASK_CURRENT"),
            Segment("after", "TEMPORAL_AFTER"),
            Segment(ctx.anchor, "TEMPORAL_ANCHOR"),
            Segment("?"),
        ),
        "counterfactual_had_kept",
    )


_QUERY_TEMPLATES: tuple[TemplateFn, ...] = (
    _query_after,
    _query_before_current,
    _query_since,
    _query_during,
    _query_ordinal_first,
    _query_ordinal_last,
    _query_ordinal_nth,
    _query_cause,
)
_MEMORY_TEMPLATES: tuple[TemplateFn, ...] = (
    _memory_after_because,
    _memory_before,
    _memory_since,
    _memory_during_due_to,
    _memory_caused,
    _memory_ordinal,
)
_COUNTERFACTUAL_TEMPLATES: tuple[TemplateFn, ...] = (
    _counterfactual_if_not,
    _counterfactual_without,
    _counterfactual_had_kept,
)
_MSEB_TARGETED_TEMPLATES: tuple[TemplateFn, ...] = (
    _query_mseb_current_adopted,
    _query_mseb_latest_approach,
    _query_mseb_ordinal_last_section,
    _query_mseb_final_conclusion,
    _query_mseb_preceded_adopted,
    _query_mseb_considered_before_choice,
    _query_mseb_retired_alternatives,
    _query_mseb_trace_context,
)


def generate_ctlg_sdg_examples(request: CTLGSDGRequest) -> list[CTLGExample]:
    """Generate deterministic BIO-valid CTLG examples."""
    if request.domain not in DOMAINS:
        raise ValueError(f"unknown domain {request.domain!r}")
    if request.n_rows <= 0:
        raise ValueError("n_rows must be positive")

    rng = random.Random(request.seed)
    templates = _templates_for_voice(request.voice)
    examples: list[CTLGExample] = []
    seen: set[str] = set()
    attempts = 0
    while len(examples) < request.n_rows and attempts < request.n_rows * 50:
        template = templates[attempts % len(templates)]
        ctx = _context_for(attempts, rng)
        voice, segments, note = template(ctx)
        ex = _example_from_segments(
            segments,
            ctx=ctx,
            domain=request.domain,
            voice=voice,
            split=request.split,
            source=request.source,
            note=note,
        )
        attempts += 1
        if ex.text in seen:
            continue
        seen.add(ex.text)
        examples.append(ex)
    if len(examples) < request.n_rows:
        raise RuntimeError(
            f"generated {len(examples)} unique CTLG SDG rows; requested {request.n_rows}",
        )
    return examples


def _templates_for_voice(voice: CTLGSDGVoice) -> tuple[TemplateFn, ...]:
    if voice == "query":
        return _QUERY_TEMPLATES
    if voice == "memory":
        return _MEMORY_TEMPLATES
    if voice == "counterfactual":
        return _COUNTERFACTUAL_TEMPLATES
    if voice == "mixed":
        return _QUERY_TEMPLATES + _MEMORY_TEMPLATES + _COUNTERFACTUAL_TEMPLATES
    if voice == "mseb_targeted":
        return _MSEB_TARGETED_TEMPLATES
    raise ValueError(f"unknown CTLG SDG voice {voice!r}")


def _context_for(i: int, rng: random.Random) -> _TemplateContext:
    subject = _sample(_SUBJECTS, i, rng)
    referent = _sample(_REFERENTS, i, rng)
    alternative = _sample(_REFERENTS, i + 5, rng)
    if alternative == referent:
        alternative = _REFERENTS[(i + 7) % len(_REFERENTS)]
    return _TemplateContext(
        subject=subject,
        referent=referent,
        alternative=alternative,
        cause=_sample(_CAUSES, i, rng),
        event=_sample(_CAUSES, i + 3, rng),
        anchor=_sample(_ANCHORS, i, rng),
        scope=_sample(_SCOPES, i, rng),
        ordinal=_sample(_ORDINALS, i, rng),
    )


def _sample(values: Sequence[str], i: int, rng: random.Random) -> str:
    return values[(i + rng.randrange(len(values))) % len(values)]


def _example_from_segments(
    segments: tuple[Segment, ...],
    *,
    ctx: _TemplateContext,
    domain: Domain,
    voice: CTLGVoice,
    split: CTLGSplit,
    source: str,
    note: str,
) -> CTLGExample:
    text, rendered = _render_segments(segments)
    tokens, offsets = _tokenize_with_offsets(text)
    cue_tags = _tags_for_tokens(offsets, rendered)
    expected_tlg_query = _expected_tlg_query_for_note(note, ctx) if voice == "query" else None
    return CTLGExample(
        text=text,
        tokens=tokens,
        cue_tags=cue_tags,
        char_offsets=offsets,
        domain=domain,
        voice=voice,
        split=split,
        source=source,
        note=note,
        expected_tlg_query=expected_tlg_query,
    )


def _expected_tlg_query_for_note(note: str, ctx: _TemplateContext) -> CTLGExpectedQuery | None:
    """Return the intended query-side TLG form for deterministic templates."""
    subject = ctx.subject.lower()
    referent = ctx.referent.lower()
    event = ctx.event.lower()
    cause = ctx.cause.lower()
    alternative = ctx.alternative.lower()
    anchor = ctx.anchor.lower()
    scope = ctx.scope.lower()
    if note == "query_after_change":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="after_named",
            referent=event,
            subject=subject,
        )
    if note == "query_before_current":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="predecessor",
            referent=event,
            scope=scope,
        )
    if note == "query_since_change":
        return CTLGExpectedQuery(
            axis="state",
            relation="declared",
            subject=subject,
            temporal_anchor=anchor,
        )
    if note == "query_during_change":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="during_interval",
            referent=referent,
            temporal_anchor=anchor,
        )
    if note == "query_ordinal_first":
        return CTLGExpectedQuery(
            axis="ordinal",
            relation="first",
            referent=referent,
            scope=scope,
        )
    if note == "query_ordinal_last":
        return CTLGExpectedQuery(
            axis="ordinal",
            relation="last",
            referent=event,
            subject=subject,
        )
    if note == "query_ordinal_nth":
        return CTLGExpectedQuery(
            axis="ordinal",
            relation="nth",
            temporal_anchor=anchor,
            scenario=ctx.ordinal.lower(),
        )
    if note == "query_causal_after":
        return CTLGExpectedQuery(
            axis="causal",
            relation="cause_of",
            referent=referent,
            secondary=cause,
            subject=subject,
        )
    if note == "mseb_current_adopted":
        return CTLGExpectedQuery(
            axis="state",
            relation="current",
            subject=subject,
            scope="decision",
        )
    if note == "mseb_current_latest_approach":
        return CTLGExpectedQuery(
            axis="state",
            relation="current",
            scope=scope,
        )
    if note == "mseb_ordinal_last_section":
        return CTLGExpectedQuery(
            axis="ordinal",
            relation="last",
            scope="section",
        )
    if note == "mseb_ordinal_last_conclusion":
        return CTLGExpectedQuery(
            axis="ordinal",
            relation="last",
            subject=subject,
            scope="conclusion",
        )
    if note == "mseb_predecessor_preceded":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="predecessor",
            referent="adopted decision",
        )
    if note == "mseb_predecessor_current_choice":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="predecessor",
            referent="current choice",
        )
    if note == "mseb_retirement_alternatives":
        return CTLGExpectedQuery(
            axis="state",
            relation="retired",
            referent="adopted decision",
            scope="alternatives",
        )
    if note == "mseb_sequence_context":
        return CTLGExpectedQuery(
            axis="temporal",
            relation="after_named",
            referent="context",
            scope="consequences",
        )
    if note == "counterfactual_if_not":
        return CTLGExpectedQuery(
            axis="modal",
            relation="would_be_current_if",
            referent=event,
            subject=subject,
            scenario=f"preserve_{event}",
        )
    if note == "counterfactual_without":
        return CTLGExpectedQuery(
            axis="modal",
            relation="would_be_current_if",
            referent=cause,
            scope=scope,
            scenario=f"preserve_{cause}",
        )
    if note == "counterfactual_had_kept":
        return CTLGExpectedQuery(
            axis="modal",
            relation="would_be_current_if",
            referent=alternative,
            temporal_anchor=anchor,
            scenario=f"preserve_{alternative}",
        )
    return None


def _render_segments(segments: tuple[Segment, ...]) -> tuple[str, tuple[_RenderedSegment, ...]]:
    text = ""
    rendered: list[_RenderedSegment] = []
    for segment in segments:
        piece = segment.text.strip()
        if not piece:
            continue
        if text and piece not in _PUNCT:
            text += " "
        start = len(text)
        text += piece
        rendered.append(_RenderedSegment(start, len(text), segment.cue_type))
    return text, tuple(rendered)


def _tokenize_with_offsets(text: str) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    tokens: list[str] = []
    offsets: list[tuple[int, int]] = []
    for match in _TOKEN_RE.finditer(text):
        tokens.append(match.group(0))
        offsets.append((match.start(), match.end()))
    return tuple(tokens), tuple(offsets)


def _tags_for_tokens(
    offsets: tuple[tuple[int, int], ...],
    segments: tuple[_RenderedSegment, ...],
) -> tuple[CueLabel, ...]:
    tags: list[CueLabel] = []
    previous_type: CueType | None = None
    for start, end in offsets:
        cue_type = _cue_type_for_token(start, end, segments)
        if cue_type is None:
            tags.append("O")
            previous_type = None
            continue
        prefix = "I" if previous_type == cue_type else "B"
        tags.append(cast(CueLabel, f"{prefix}-{cue_type}"))
        previous_type = cue_type
    return tuple(tags)


def _cue_type_for_token(
    start: int,
    end: int,
    segments: tuple[_RenderedSegment, ...],
) -> CueType | None:
    for segment in segments:
        if segment.cue_type is None:
            continue
        if start >= segment.start and end <= segment.end:
            return segment.cue_type
    return None


__all__ = [
    "CTLGSDGRequest",
    "CTLGSDGVoice",
    "Segment",
    "generate_ctlg_sdg_examples",
]
