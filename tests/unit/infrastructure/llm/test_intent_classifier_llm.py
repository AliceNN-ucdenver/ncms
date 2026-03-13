"""Tests for LLM-based intent classification fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ncms.domain.intent import QueryIntent
from ncms.infrastructure.llm.intent_classifier_llm import classify_intent_with_llm


class TestClassifyIntentWithLlm:
    """Tests for classify_intent_with_llm."""

    @pytest.fixture(autouse=True)
    def _patch_llm(self):
        """Patch call_llm_json for all tests."""
        with patch(
            "ncms.infrastructure.llm.intent_classifier_llm.call_llm_json",
            new_callable=AsyncMock,
        ) as mock:
            self.mock_llm = mock
            yield

    async def test_valid_response_returns_intent_result(self) -> None:
        self.mock_llm.return_value = {
            "intent": "change_detection",
            "confidence": 0.85,
        }
        result = await classify_intent_with_llm("What changed?", model="test-model")
        assert result is not None
        assert result.intent == QueryIntent.CHANGE_DETECTION
        assert result.confidence == 0.85
        assert len(result.target_node_types) > 0

    async def test_unknown_intent_returns_none(self) -> None:
        self.mock_llm.return_value = {
            "intent": "nonexistent_intent",
            "confidence": 0.9,
        }
        result = await classify_intent_with_llm("test query", model="test-model")
        assert result is None

    async def test_non_dict_response_returns_none(self) -> None:
        self.mock_llm.return_value = ["not", "a", "dict"]
        result = await classify_intent_with_llm("test query", model="test-model")
        assert result is None

    async def test_none_response_returns_none(self) -> None:
        self.mock_llm.return_value = None
        result = await classify_intent_with_llm("test query", model="test-model")
        assert result is None

    async def test_llm_exception_returns_none(self) -> None:
        self.mock_llm.side_effect = RuntimeError("LLM timeout")
        result = await classify_intent_with_llm("test query", model="test-model")
        assert result is None

    async def test_confidence_clamped_to_zero_one(self) -> None:
        self.mock_llm.return_value = {"intent": "fact_lookup", "confidence": 1.5}
        result = await classify_intent_with_llm("test", model="test-model")
        assert result is not None
        assert result.confidence == 1.0

    async def test_confidence_negative_clamped_to_zero(self) -> None:
        self.mock_llm.return_value = {"intent": "fact_lookup", "confidence": -0.5}
        result = await classify_intent_with_llm("test", model="test-model")
        assert result is not None
        assert result.confidence == 0.0

    async def test_query_truncated_in_prompt(self) -> None:
        long_query = "x" * 5000
        self.mock_llm.return_value = {"intent": "fact_lookup", "confidence": 0.9}
        await classify_intent_with_llm(long_query, model="test-model")
        # Verify the prompt was called with truncated query
        call_args = self.mock_llm.call_args
        prompt = call_args[0][0]  # First positional arg
        assert len(prompt) < 5000  # Truncated

    async def test_all_intent_types_accepted(self) -> None:
        for intent in QueryIntent:
            self.mock_llm.return_value = {
                "intent": intent.value,
                "confidence": 0.8,
            }
            result = await classify_intent_with_llm("test", model="test-model")
            assert result is not None
            assert result.intent == intent
