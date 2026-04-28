"""Unit tests for the query-intent classifier.

Pins the three supported patterns: current / origin / still. Anything
else (or nothing matching) returns ``None`` so the caller can fall back
to BM25 unchanged.
"""

from __future__ import annotations

from ncms.domain.tlg import classify_query_intent


class TestStill:
    def test_are_we_still(self) -> None:
        assert classify_query_intent("Are we still using session cookies?") == "still"

    def test_do_we_still(self) -> None:
        assert classify_query_intent("Do we still run the nightly batch job?") == "still"

    def test_still_using(self) -> None:
        assert classify_query_intent("Is the legacy gateway still in use?") == "still"

    def test_have_we_retired(self) -> None:
        assert classify_query_intent("Have we retired the v1 API?") == "still"


class TestOrigin:
    def test_original(self) -> None:
        assert classify_query_intent("What was the original auth method?") == "origin"

    def test_initial(self) -> None:
        assert classify_query_intent("What was the initial DIAGNOSIS for the knee?") == "origin"

    def test_first(self) -> None:
        assert classify_query_intent("What did we first try for the payments system?") == "origin"

    def test_when_begin(self) -> None:
        assert classify_query_intent("When did the migration begin?") == "origin"


class TestCurrent:
    def test_current(self) -> None:
        assert classify_query_intent("What is the current auth method?") == "current"

    def test_latest(self) -> None:
        assert classify_query_intent("Show me the latest release notes.") == "current"

    def test_what_do_we_use(self) -> None:
        assert classify_query_intent("What does the gateway use for auth?") == "current"


class TestNoMatch:
    def test_plain_fact_lookup_returns_none(self) -> None:
        assert classify_query_intent("Who authored the design doc?") is None

    def test_empty_string_returns_none(self) -> None:
        assert classify_query_intent("") is None


class TestPriority:
    def test_still_takes_precedence_over_current(self) -> None:
        # Query contains both ``still`` and a current-tense verb; the
        # STILL pattern runs first so we get "still".
        assert classify_query_intent("Are we still using the current gateway?") == "still"

    def test_origin_takes_precedence_over_current(self) -> None:
        # "original" + "currently" — classifier checks origin before
        # current, so origin wins (matches research semantics).
        assert classify_query_intent("What was the original system we currently have?") == "origin"
