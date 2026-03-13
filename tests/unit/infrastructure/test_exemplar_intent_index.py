"""Tests for BM25 exemplar-based intent classification.

Verifies that the ExemplarIntentIndex classifies queries using BM25 matching
against exemplar queries rather than keyword pattern matching.
"""

from __future__ import annotations

from ncms.domain.intent import INTENT_EXEMPLARS, QueryIntent
from ncms.infrastructure.indexing.exemplar_intent_index import ExemplarIntentIndex


class TestExemplarIntentIndex:
    """Core exemplar classification tests."""

    def setup_method(self) -> None:
        self.idx = ExemplarIntentIndex(top_k=5)

    # ── Current State Lookup ──────────────────────────────────────────────

    def test_current_state_from_exemplar(self) -> None:
        result = self.idx.classify("What is the current status of the API?")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    def test_current_state_paraphrase(self) -> None:
        """BM25 stemming should handle paraphrases not in exemplar list."""
        result = self.idx.classify("Show the active deployment configuration")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    def test_latest_version_query(self) -> None:
        result = self.idx.classify("What version is currently running?")
        assert result.intent == QueryIntent.CURRENT_STATE_LOOKUP

    # ── Historical Lookup ─────────────────────────────────────────────────

    def test_historical_from_exemplar(self) -> None:
        result = self.idx.classify("What was the database schema in January?")
        assert result.intent == QueryIntent.HISTORICAL_LOOKUP

    def test_historical_paraphrase(self) -> None:
        result = self.idx.classify("How were things configured previously?")
        assert result.intent == QueryIntent.HISTORICAL_LOOKUP

    # ── Event Reconstruction ──────────────────────────────────────────────

    def test_event_from_exemplar(self) -> None:
        result = self.idx.classify("What happened during the deployment outage?")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    def test_event_paraphrase(self) -> None:
        result = self.idx.classify("Describe the sequence of failures")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    def test_incident_timeline(self) -> None:
        result = self.idx.classify("Walk me through the incident timeline")
        assert result.intent == QueryIntent.EVENT_RECONSTRUCTION

    # ── Change Detection ──────────────────────────────────────────────────

    def test_change_from_exemplar(self) -> None:
        result = self.idx.classify("What changed in the API since last release?")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    def test_change_paraphrase(self) -> None:
        result = self.idx.classify("What's different about the new version?")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    def test_compare_versions(self) -> None:
        result = self.idx.classify("Compare the before and after of the migration")
        assert result.intent == QueryIntent.CHANGE_DETECTION

    # ── Pattern Lookup ────────────────────────────────────────────────────

    def test_pattern_from_exemplar(self) -> None:
        result = self.idx.classify("Is there a pattern in the deployment failures?")
        assert result.intent == QueryIntent.PATTERN_LOOKUP

    def test_pattern_paraphrase(self) -> None:
        result = self.idx.classify("Are there recurring problems with this service?")
        assert result.intent == QueryIntent.PATTERN_LOOKUP

    # ── Strategic Reflection ──────────────────────────────────────────────

    def test_strategic_from_exemplar(self) -> None:
        result = self.idx.classify("What lessons have we learned from past incidents?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    def test_strategic_paraphrase(self) -> None:
        result = self.idx.classify("What are the best practices for migrations?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    def test_takeaway_query(self) -> None:
        result = self.idx.classify("What are the key takeaways?")
        assert result.intent == QueryIntent.STRATEGIC_REFLECTION

    # ── Fallback Behavior ─────────────────────────────────────────────────

    def test_empty_query_defaults_to_fact_lookup(self) -> None:
        result = self.idx.classify("")
        assert result.intent == QueryIntent.FACT_LOOKUP
        assert result.confidence == 1.0

    def test_generic_query(self) -> None:
        """Unambiguous generic queries should still return some result."""
        result = self.idx.classify("Tell me about the system")
        assert result.intent is not None
        assert result.confidence > 0

    # ── Confidence ────────────────────────────────────────────────────────

    def test_exact_exemplar_has_high_confidence(self) -> None:
        result = self.idx.classify("What is the current status of the API?")
        assert result.confidence >= 0.5

    def test_confidence_between_zero_and_one(self) -> None:
        result = self.idx.classify("What changed in the deployment pipeline?")
        assert 0.0 <= result.confidence <= 1.0

    # ── IntentResult structure ────────────────────────────────────────────

    def test_result_has_target_node_types(self) -> None:
        result = self.idx.classify("What is the current API status?")
        assert len(result.target_node_types) > 0

    def test_result_intent_is_valid_enum(self) -> None:
        result = self.idx.classify("Show me the timeline of events")
        assert isinstance(result.intent, QueryIntent)


class TestExemplarData:
    """Validate the exemplar data itself."""

    def test_all_non_fact_intents_have_exemplars(self) -> None:
        for intent in QueryIntent:
            if intent == QueryIntent.FACT_LOOKUP:
                continue
            assert intent in INTENT_EXEMPLARS, f"Missing exemplars for {intent}"
            assert len(INTENT_EXEMPLARS[intent]) >= 5, (
                f"Too few exemplars for {intent}: {len(INTENT_EXEMPLARS[intent])}"
            )

    def test_no_empty_exemplars(self) -> None:
        for intent, exemplars in INTENT_EXEMPLARS.items():
            for i, ex in enumerate(exemplars):
                assert ex.strip(), f"Empty exemplar at index {i} for {intent}"

    def test_fact_lookup_not_in_exemplars(self) -> None:
        """fact_lookup is the fallback — it shouldn't have dedicated exemplars."""
        assert QueryIntent.FACT_LOOKUP not in INTENT_EXEMPLARS
