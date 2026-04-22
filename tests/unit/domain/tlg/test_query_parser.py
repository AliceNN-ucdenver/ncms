"""Unit tests for the post-v6 minimal L3 query entity extractor.

Before v6 this module tested ~15 regex intent production rules
(SEED_INTENT_MARKERS + _match_current / _match_origin / …).  Those
were deleted when the SLM's ``shape_intent_head`` took over query
classification; the surviving :func:`analyze_query` does only two
things:

1. Resolve a corpus subject via L1 vocabulary fuzzy-match.
2. Extract a target entity surface form from the query text.

These tests cover both paths, plus ``compute_domain_nouns`` (still
used by the extractor to skip subject-topical nouns when picking a
target entity).
"""

from __future__ import annotations

from dataclasses import dataclass

from ncms.domain.tlg import (
    InducedEdgeMarkers,
    SubjectMemory,
    induce_vocabulary,
)
from ncms.domain.tlg.query_parser import (
    ParserContext,
    QueryStructure,
    analyze_query,
    compute_domain_nouns,
)


@dataclass(frozen=True)
class _MemFixture:
    subject: str | None
    entities: frozenset[str]


def _auth_corpus() -> list[SubjectMemory]:
    return [
        SubjectMemory(
            subject="auth",
            entities=frozenset({"session cookies", "authentication"}),
        ),
        SubjectMemory(
            subject="auth",
            entities=frozenset({"OAuth 2.0", "authentication"}),
        ),
        SubjectMemory(
            subject="auth",
            entities=frozenset({"JWT", "authentication", "tokens"}),
        ),
        SubjectMemory(
            subject="payments",
            entities=frozenset({"payments project", "ledger"}),
        ),
    ]


def _fixture_ctx(
    *,
    issue_entities: frozenset[str] = frozenset(
        {"bug", "bugs", "issue", "issues", "problem", "problems"},
    ),
) -> ParserContext:
    corpus = _auth_corpus()
    vocab = induce_vocabulary(corpus)
    mem_fixtures = [
        _MemFixture(subject=m.subject, entities=m.entities)
        for m in corpus
    ]
    domain_nouns = compute_domain_nouns(
        mem_fixtures, min_memories_per_subject=2,
    )
    return ParserContext(
        vocabulary=vocab,
        induced_markers=InducedEdgeMarkers(markers={}),
        issue_entities=issue_entities,
        domain_nouns=domain_nouns,
    )


# ---------------------------------------------------------------------------
# compute_domain_nouns — frequency-based "topical noun" filter
# ---------------------------------------------------------------------------


class TestDomainNouns:
    def test_entity_present_in_all_subject_memories_is_domain_noun(self) -> None:
        # "authentication" is in 3/3 auth memories, so at 60%
        # threshold it's a domain noun for the auth subject.
        ctx = _fixture_ctx()
        assert "authentication" in ctx.domain_nouns

    def test_subject_below_min_memories_contributes_nothing(self) -> None:
        # The payments subject has 1 memory, below min=2 threshold.
        # "payments project" should NOT be a domain noun.
        ctx = _fixture_ctx()
        assert "payments project" not in ctx.domain_nouns
        assert "ledger" not in ctx.domain_nouns


# ---------------------------------------------------------------------------
# analyze_query — subject resolution + target_entity extraction
# ---------------------------------------------------------------------------


class TestAnalyzeQuery:
    def test_returns_query_structure_with_intent_none(self) -> None:
        ctx = _fixture_ctx()
        qs = analyze_query("what is the current auth method?", ctx)
        assert isinstance(qs, QueryStructure)
        # Post-v6: analyze_query NEVER assigns intent; SLM does.
        assert qs.intent is None

    def test_resolves_subject_from_vocabulary_match(self) -> None:
        ctx = _fixture_ctx()
        qs = analyze_query(
            "how has authentication evolved?", ctx,
        )
        # "authentication" is in the auth subject's entity set;
        # vocabulary lookup resolves to subject="auth".
        assert qs.subject == "auth"

    def test_no_subject_when_no_vocab_match(self) -> None:
        ctx = _fixture_ctx()
        qs = analyze_query("what did I have for breakfast?", ctx)
        assert qs.subject is None

    def test_skips_domain_noun_as_target_entity(self) -> None:
        ctx = _fixture_ctx()
        # "authentication" is a domain noun — too general to be a
        # target entity.  Extraction should skip it; with no other
        # L1 entity and no issue-seed match in the query, returns
        # None.
        qs = analyze_query("what is the current authentication?", ctx)
        assert qs.target_entity != "authentication"

    def test_issue_seed_fallback_when_no_l1_entity(self) -> None:
        ctx = _fixture_ctx()
        # "problem" is in issue_entities; query has no distinctive
        # L1 entities, so the issue-seed fallback picks it up.
        qs = analyze_query(
            "what was the latest reported problem?", ctx,
        )
        assert qs.target_entity == "problem"
