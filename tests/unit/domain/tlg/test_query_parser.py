"""Unit tests for the structural query parser.

Covers every production (interval, before_named, transitive_cause,
concurrent, sequence, predecessor, range, retirement, still,
cause_of, origin, current) plus:

* ``ParserContext`` augmentation — seed + L2-induced markers.
* ``compute_domain_nouns`` frequency rule.
* Retirement structure gate (bare verb without imperative/passive
  structure rejects).
* cause_of domain-noun-collapse rejection.
* Range-detection filters (recency / ordinal / <7-day spans).

Fixtures stay small — two-subject synthetic corpora are enough
to exercise the production-rule behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from ncms.domain.tlg import (
    InducedEdgeMarkers,
    SubjectMemory,
    induce_vocabulary,
)
from ncms.domain.tlg.query_parser import (
    ISSUE_SEED,
    ParserContext,
    QueryStructure,
    analyze_query,
    compute_domain_nouns,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    induced: dict[str, frozenset[str]] | None = None,
    issue_entities: frozenset[str] = ISSUE_SEED,
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
        induced_markers=InducedEdgeMarkers(
            markers=induced or {},
        ),
        issue_entities=issue_entities,
        domain_nouns=domain_nouns,
    )


# ---------------------------------------------------------------------------
# ParserContext augmentation
# ---------------------------------------------------------------------------


class TestAugmentation:
    def test_seed_only_when_no_l2_induced(self) -> None:
        ctx = _fixture_ctx()
        augmented = ctx.augmented_markers()
        assert "retire" in augmented["retirement"]  # seed
        assert augmented["retirement"] == frozenset(
            ["retired", "retire", "deprecated", "stopped using",
             "ended", "decommissioned"]
        )

    def test_l2_supersedes_extends_retirement_bucket(self) -> None:
        ctx = _fixture_ctx(induced={
            "supersedes": frozenset({"replaced", "sunset"}),
        })
        augmented = ctx.augmented_markers()
        assert "replaced" in augmented["retirement"]
        assert "sunset" in augmented["retirement"]
        # Seed still present.
        assert "retire" in augmented["retirement"]

    def test_l2_retires_extends_retirement_bucket(self) -> None:
        ctx = _fixture_ctx(induced={
            "retires": frozenset({"mothballed"}),
        })
        augmented = ctx.augmented_markers()
        assert "mothballed" in augmented["retirement"]

    def test_seed_still_bucket_unchanged_by_l2(self) -> None:
        # L2 only extends retirement; other intents stay seed-only.
        ctx = _fixture_ctx(induced={
            "supersedes": frozenset({"replaced"}),
        })
        augmented = ctx.augmented_markers()
        assert "replaced" not in augmented["still"]
        assert "replaced" not in augmented["current"]


class TestDomainNouns:
    def test_high_frequency_entity_admitted(self) -> None:
        fixtures = [
            _MemFixture(subject="auth", entities=frozenset({"authentication"})),
            _MemFixture(subject="auth", entities=frozenset({"authentication"})),
            _MemFixture(subject="auth", entities=frozenset({"authentication"})),
        ]
        nouns = compute_domain_nouns(
            fixtures, min_memories_per_subject=3,
        )
        assert "authentication" in nouns

    def test_small_subject_skipped(self) -> None:
        fixtures = [
            _MemFixture(subject="auth", entities=frozenset({"authentication"})),
            _MemFixture(subject="auth", entities=frozenset({"authentication"})),
        ]
        nouns = compute_domain_nouns(
            fixtures, min_memories_per_subject=3,
        )
        assert "authentication" not in nouns

    def test_null_subject_skipped(self) -> None:
        fixtures = [
            _MemFixture(subject=None, entities=frozenset({"foo"})),
            _MemFixture(subject=None, entities=frozenset({"foo"})),
            _MemFixture(subject=None, entities=frozenset({"foo"})),
        ]
        assert compute_domain_nouns(fixtures) == frozenset()


# ---------------------------------------------------------------------------
# Production matchers
# ---------------------------------------------------------------------------


class TestCurrent:
    def test_current_marker(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("What is the current authentication method?", ctx)
        assert r.intent == "current"
        assert r.subject == "auth"


class TestOrigin:
    def test_original_marker(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("What was the original authentication method?", ctx)
        assert r.intent == "origin"
        assert r.subject == "auth"

    def test_first_marker(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("What did we first use for auth?", ctx)
        assert r.intent == "origin"


class TestStill:
    def test_still_with_action_verb_and_object(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("Are we still using session cookies?", ctx)
        assert r.intent == "still"
        # Target should canonicalize to session cookies via vocab.
        assert r.target_entity == "session cookies"

    def test_still_with_no_object_rejects(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("are we still?", ctx)
        # Parser rejects still without a resolvable object → falls
        # through to none / whatever other intent claims the query.
        assert r.intent != "still"

    def test_currently_in_pattern(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("is the team currently in physical therapy?", ctx)
        assert r.intent == "still"


class TestRetirement:
    def test_imperative_retirement(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("Did we retire the session cookies?", ctx)
        assert r.intent == "retirement"

    def test_passive_retirement(self) -> None:
        # Passive regex requires aux + verb consecutive:
        # ``<X> (was|were|is|are|has been|have been) <verb>``.
        ctx = _fixture_ctx()
        r = analyze_query(
            "session cookies were deprecated",
            ctx,
        )
        assert r.intent == "retirement"

    def test_bare_moved_without_from_rejects(self) -> None:
        # Non-structural "moved" — bare motion, not retirement.
        ctx = _fixture_ctx(induced={
            "supersedes": frozenset({"moved"}),
        })
        r = analyze_query("did Rachel move to Seattle?", ctx)
        # Must NOT match retirement — falls through to another intent
        # or none.
        assert r.intent != "retirement"

    def test_moves_from_fires_retirement(self) -> None:
        ctx = _fixture_ctx(induced={
            "supersedes": frozenset({"moves"}),
        })
        r = analyze_query(
            "authentication moves from session cookies",
            ctx,
        )
        assert r.intent == "retirement"


class TestCauseOf:
    def test_what_caused(self) -> None:
        # "What caused the blocker on auth?" — issue entity "blocker"
        # wins over subject-domain noun "auth".
        ctx = _fixture_ctx()
        r = analyze_query(
            "What caused the blocker on authentication?",
            ctx,
        )
        assert r.intent == "cause_of"
        assert r.target_entity == "blocker"

    def test_target_collapsing_to_domain_noun_rejects(self) -> None:
        # cause_of on a subject-domain noun — should REJECT rather
        # than silently returning subject origin.
        ctx = _fixture_ctx()
        r = analyze_query("What caused authentication?", ctx)
        assert r.intent != "cause_of"


class TestSequence:
    def test_what_came_after(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What came after session cookies?",
            ctx,
        )
        assert r.intent == "sequence"
        assert r.target_entity == "session cookies"

    def test_non_wh_question_rejects(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("we decided after session cookies", ctx)
        assert r.intent != "sequence"


class TestPredecessor:
    def test_what_came_before(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("What came before session cookies?", ctx)
        assert r.intent == "predecessor"
        assert r.target_entity == "session cookies"


class TestBeforeNamed:
    def test_anchored_yes_no(self) -> None:
        # Entities with periods in their surface form aren't captured
        # whole by the `[\w\s-]` regex — research-code behaviour.
        # Use hyphenated or bare names.
        ctx = _fixture_ctx()
        r = analyze_query(
            "Did session cookies come before JWT tokens?",
            ctx,
        )
        assert r.intent == "before_named"
        assert r.target_entity == "session cookies"
        # _extract_event_name falls through to lowercased raw when
        # multiple tokens resolve to non-domain entities.
        assert "jwt" in (r.secondary_entity or "").lower()

    def test_which_first_alternation(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "Which system came first, the JWT tokens or the session cookies?",
            ctx,
        )
        assert r.intent == "before_named"


class TestInterval:
    def test_between_and(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What happened between session cookies and JWT tokens?",
            ctx,
        )
        assert r.intent == "interval"
        assert r.target_entity == "session cookies"
        # _extract_event_name falls through to lowercased raw when
        # multiple tokens resolve to non-domain entities.
        assert "jwt" in (r.secondary_entity or "").lower()


class TestConcurrent:
    def test_during(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What else was happening during the session cookies rollout?",
            ctx,
        )
        assert r.intent == "concurrent"
        # Target is the phrase trimmed by _extract_event_name —
        # exact canonicalization depends on vocab coverage.
        assert r.target_entity is not None


class TestTransitiveCause:
    def test_eventually_led_to(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What eventually led to the OAuth 2.0 rollout?",
            ctx,
        )
        assert r.intent == "transitive_cause"


class TestRange:
    def test_q1_2024_fires_range(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What happened in Q1 2024 for authentication?",
            ctx,
        )
        assert r.intent == "range"
        assert r.range_start is not None
        assert r.range_end is not None

    def test_recency_bias_does_not_fire_range(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query(
            "What is the latest authentication change?", ctx,
        )
        # "latest" triggers recency in the temporal parser → range
        # production rejects → falls through.  Current production
        # picks it up because "latest" is in the current seed set.
        assert r.intent != "range"


class TestNoneFallthrough:
    def test_random_question_returns_none(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("who authored the README file?", ctx)
        assert r.intent == "none"

    def test_empty_string_returns_none(self) -> None:
        ctx = _fixture_ctx()
        r = analyze_query("", ctx)
        assert r.intent == "none"


# ---------------------------------------------------------------------------
# QueryStructure invariants
# ---------------------------------------------------------------------------


class TestQueryStructure:
    def test_has_grammar_answer_false_for_none(self) -> None:
        qs = QueryStructure(
            intent="none", subject=None,
            target_entity=None, detected_marker=None,
        )
        assert not qs.has_grammar_answer()

    def test_has_grammar_answer_true_for_current(self) -> None:
        qs = QueryStructure(
            intent="current", subject="s",
            target_entity=None, detected_marker="current",
        )
        assert qs.has_grammar_answer()
