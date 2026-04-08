"""Unit tests for the query intent classifier."""

from __future__ import annotations

from ncms.domain.intent import (
    INTENT_EXEMPLARS,
    INTENT_TARGETS,
    QueryIntent,
    classify_intent,
)


class TestClassifyIntent:
    """Tests for classify_intent() — keyword pattern matching."""

    # ── Current State Lookup ─────────────────────────────────────────────

    def test_what_is_query(self) -> None:
        result = classify_intent("What is the current status of the auth service?")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    def test_current_status_query(self) -> None:
        result = classify_intent("Show me the current deployment state")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    def test_right_now_query(self) -> None:
        result = classify_intent("What's happening right now with the database?")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    def test_latest_query(self) -> None:
        result = classify_intent("What is the latest configuration?")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    # ── Historical Lookup ────────────────────────────────────────────────

    def test_what_was_query(self) -> None:
        result = classify_intent("What was the API version last month?")
        assert result.intent == QueryIntent.HISTORICAL_LOOKUP

    def test_in_month_query(self) -> None:
        result = classify_intent("What happened in january with the deployment?")
        # "What happened" is event_reconstruction but "in january" adds historical
        # The winner depends on cumulative weight — check it classifies to one of them
        assert result.intent in (
            QueryIntent.HISTORICAL_LOOKUP, QueryIntent.EVENT_RECONSTRUCTION,
        )

    def test_previously_query(self) -> None:
        result = classify_intent("What was the state previously before the migration?")
        assert result.intent == QueryIntent.HISTORICAL_LOOKUP

    def test_used_to_query(self) -> None:
        result = classify_intent("The service used to return XML format")
        assert result.intent == QueryIntent.HISTORICAL_LOOKUP

    # ── Event Reconstruction ─────────────────────────────────────────────

    def test_what_happened_query(self) -> None:
        result = classify_intent("What happened during the outage last night?")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    def test_incident_query(self) -> None:
        result = classify_intent("Tell me about the incident with the payment service")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    def test_timeline_query(self) -> None:
        result = classify_intent("Give me a timeline of the deployment process")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    def test_walk_through_query(self) -> None:
        result = classify_intent("Walk me through the database migration")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    # ── Change Detection ─────────────────────────────────────────────────

    def test_what_changed_query(self) -> None:
        result = classify_intent("What changed in the auth service configuration?")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    def test_compared_to_query(self) -> None:
        result = classify_intent("How is the new API compared to the old one?")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    def test_evolution_query(self) -> None:
        result = classify_intent("Show me the evolution of the database schema")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    def test_before_and_after_query(self) -> None:
        result = classify_intent("What's the before and after of the refactor?")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    # ── Pattern Lookup ───────────────────────────────────────────────────

    def test_pattern_query(self) -> None:
        result = classify_intent("Is there a pattern in the deployment failures?")
        assert result.intent == QueryIntent.PATTERN_LOOKUP

    def test_usually_query(self) -> None:
        result = classify_intent("What usually causes these kinds of errors?")
        assert result.intent == QueryIntent.PATTERN_LOOKUP

    def test_recurring_query(self) -> None:
        result = classify_intent("Are there recurring issues with the auth service?")
        assert result.intent == QueryIntent.PATTERN_LOOKUP

    # ── Strategic Reflection ─────────────────────────────────────────────

    def test_lessons_learned_query(self) -> None:
        result = classify_intent("What lessons have we learned from past incidents?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    def test_insight_query(self) -> None:
        result = classify_intent("Any insights from the last quarter's deployments?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    def test_takeaway_query(self) -> None:
        result = classify_intent("What are the key takeaways from the migration?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    def test_best_practices_query(self) -> None:
        result = classify_intent("What are the best practices for database migrations?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    # ── Fallback Behavior ────────────────────────────────────────────────

    def test_generic_query_defaults_to_fact_lookup(self) -> None:
        result = classify_intent("How does the authentication service work?")
        assert result.intent == QueryIntent.FACT_LOOKUP

    def test_empty_query_defaults_to_fact_lookup(self) -> None:
        result = classify_intent("")
        assert result.intent == QueryIntent.FACT_LOOKUP
        assert result.confidence == 1.0

    def test_unrelated_query_defaults_to_fact_lookup(self) -> None:
        result = classify_intent("PostgreSQL connection pooling configuration")
        assert result.intent == QueryIntent.FACT_LOOKUP

    # ── Confidence ───────────────────────────────────────────────────────

    def test_strong_match_has_high_confidence(self) -> None:
        """Multiple matching patterns → higher confidence."""
        result = classify_intent("What is the current status right now?")
        # "what is" (0.5) + "current" (0.4) + "status" (0.3) + "right now" (0.5)
        assert result.confidence >= 0.8

    def test_single_weak_match_has_lower_confidence(self) -> None:
        """Single weak pattern → lower confidence."""
        result = classify_intent("Show me the latest info")
        # Only "latest" (0.3) matches
        assert result.confidence <= 0.5

    def test_fact_lookup_has_full_confidence(self) -> None:
        """Default intent gets 1.0 confidence (we ARE sure it's a fact lookup)."""
        result = classify_intent("How does JWT token validation work?")
        assert result.intent == QueryIntent.FACT_LOOKUP
        assert result.confidence == 1.0


class TestIntentResult:
    """Tests for IntentResult dataclass."""

    def test_target_node_types_populated(self) -> None:
        result = classify_intent("What is the current API version?")
        assert len(result.target_node_types) > 0

    def test_frozen_dataclass(self) -> None:
        result = classify_intent("test query")
        try:
            result.intent = QueryIntent.CHANGE_DETECTION  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # Expected — frozen dataclass

    def test_all_intents_have_targets(self) -> None:
        """Every QueryIntent value has a mapping in INTENT_TARGETS."""
        for intent in QueryIntent:
            assert intent in INTENT_TARGETS
            assert len(INTENT_TARGETS[intent]) > 0


class TestIntentExemplars:
    """Tests for INTENT_EXEMPLARS data quality."""

    def test_all_non_fact_intents_have_exemplars(self) -> None:
        for intent in QueryIntent:
            if intent == QueryIntent.FACT_LOOKUP:
                continue
            assert intent in INTENT_EXEMPLARS

    def test_each_intent_has_at_least_10_exemplars(self) -> None:
        for intent, exemplars in INTENT_EXEMPLARS.items():
            assert len(exemplars) >= 10, f"{intent} has only {len(exemplars)} exemplars"

    def test_no_duplicate_exemplars_within_intent(self) -> None:
        for intent, exemplars in INTENT_EXEMPLARS.items():
            assert len(exemplars) == len(set(exemplars)), (
                f"Duplicate exemplars in {intent}"
            )
