"""Unit tests for the L1 subject-vocabulary induction.

Pins the behaviours the research code validated:

* Every entity becomes a routing token for its subject.
* Multi-word entities also register their individual words as
  secondary tokens.
* Ambiguous tokens route to their majority subject.
* :func:`lookup_subject` prefers primary tokens over word-split
  secondaries, and prefers distinctive tokens over shared ones.
* Short-token + digit filters drop noise.
"""

from __future__ import annotations

from ncms.domain.tlg.vocabulary import (
    SubjectMemory,
    induce_vocabulary,
    lookup_entity,
    lookup_subject,
)


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
    ]


def _payments_corpus() -> list[SubjectMemory]:
    return [
        SubjectMemory(
            subject="payments",
            entities=frozenset({"payments project", "ledger"}),
        ),
        SubjectMemory(
            subject="payments",
            entities=frozenset({"payments project", "settlement"}),
        ),
    ]


def _identity_corpus() -> list[SubjectMemory]:
    return [
        SubjectMemory(
            subject="identity_project",
            entities=frozenset({"identity project", "roadmap"}),
        ),
    ]


# ---------------------------------------------------------------------------


class TestInduction:
    def test_primary_entity_tokens_registered(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        assert "session cookies" in vocab.subject_lookup
        assert vocab.subject_lookup["session cookies"] == "auth"
        assert "session cookies" in vocab.primary_tokens

    def test_split_words_registered_as_secondary(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        # "session" and "cookies" both become secondary tokens
        assert "session" in vocab.subject_lookup
        assert "cookies" in vocab.subject_lookup
        assert "session" not in vocab.primary_tokens
        assert "cookies" not in vocab.primary_tokens

    def test_short_lowercase_tokens_dropped(self) -> None:
        vocab = induce_vocabulary([
            SubjectMemory(subject="s", entities=frozenset({"on ramp"})),
        ])
        # "on" is a 2-char lowercase word → dropped
        assert "on" not in vocab.subject_lookup
        assert "ramp" in vocab.subject_lookup

    def test_two_char_allcaps_kept(self) -> None:
        vocab = induce_vocabulary([
            SubjectMemory(subject="clinic", entities=frozenset({"PT program"})),
        ])
        # "PT" (uppercase) → kept as signal
        assert "pt" in vocab.subject_lookup

    def test_digit_tokens_dropped(self) -> None:
        vocab = induce_vocabulary([
            SubjectMemory(subject="s", entities=frozenset({"2024 roadmap"})),
        ])
        assert "2024" not in vocab.subject_lookup
        assert "roadmap" in vocab.subject_lookup

    def test_ambiguous_token_picks_majority(self) -> None:
        # "authentication" appears in both subjects but more often in auth.
        corpus = _auth_corpus() + [
            SubjectMemory(
                subject="docs", entities=frozenset({"authentication guide"}),
            ),
        ]
        vocab = induce_vocabulary(corpus)
        assert vocab.subject_lookup["authentication"] == "auth"

    def test_ranked_lists_are_longest_first(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        # Longest entity token first (len-sorted desc)
        lengths = [len(t) for t in vocab.entity_tokens_ranked]
        assert lengths == sorted(lengths, reverse=True)


# ---------------------------------------------------------------------------


class TestLookupSubject:
    def test_direct_primary_match(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        assert lookup_subject("Are we still using session cookies?", vocab) == "auth"

    def test_primary_beats_split_derived(self) -> None:
        # "project" is a split-derived token shared across subjects;
        # "roadmap" is a primary token in only identity_project.  The
        # query mentions both — primary-ness should win.
        corpus = _payments_corpus() + _identity_corpus()
        vocab = induce_vocabulary(corpus)
        assert lookup_subject(
            "what's on the roadmap for the project?", vocab
        ) == "identity_project"

    def test_stem_match_handles_morphology(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        # Query uses "authenticate"; corpus has "authentication".  Same stem.
        assert lookup_subject("How do we authenticate new users?", vocab) == "auth"

    def test_no_match_returns_none(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        assert lookup_subject("what's for lunch?", vocab) is None


# ---------------------------------------------------------------------------


class TestLookupEntity:
    def test_returns_canonical_casing(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        # Query is lowercase, canonical form keeps original.
        result = lookup_entity("did we drop session cookies?", vocab)
        assert result == "session cookies"

    def test_longest_match_wins(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        # "session cookies" (phrase) should beat "session" (word split)
        # because entity_tokens_ranked is sorted longest-first.
        result = lookup_entity("rolling back session cookies behaviour", vocab)
        assert result == "session cookies"

    def test_no_match_returns_none(self) -> None:
        vocab = induce_vocabulary(_auth_corpus())
        assert lookup_entity("nothing matches here", vocab) is None


# ---------------------------------------------------------------------------


class TestEmptyCorpus:
    def test_empty_input_produces_empty_vocab(self) -> None:
        vocab = induce_vocabulary([])
        assert vocab.subject_lookup == {}
        assert vocab.entity_lookup == {}
        assert vocab.primary_tokens == frozenset()
        assert lookup_subject("anything", vocab) is None
        assert lookup_entity("anything", vocab) is None

    def test_memory_with_no_subject_skipped(self) -> None:
        vocab = induce_vocabulary([
            SubjectMemory(subject="", entities=frozenset({"something"})),
        ])
        assert vocab.subject_lookup == {}
