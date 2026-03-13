"""Tests for LLM-based abstract memory synthesis (Phase 5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ncms.infrastructure.consolidation.abstract_synthesizer import (
    synthesize_episode_summary,
    synthesize_recurring_pattern,
    synthesize_state_trajectory,
)


class TestSynthesizeEpisodeSummary:
    """Tests for synthesize_episode_summary."""

    @pytest.fixture(autouse=True)
    def _patch_llm(self):
        with patch(
            "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock:
            self.mock_llm = mock
            yield

    async def test_valid_response_returns_summary(self) -> None:
        self.mock_llm.return_value = {
            "summary": "The team migrated the API.",
            "actors": ["api-team"],
            "artifacts": ["api-v2"],
            "decisions": ["use REST"],
            "outcome": "Migration complete.",
            "confidence": 0.85,
        }
        result = await synthesize_episode_summary(
            episode_title="API Migration",
            member_contents=["Added new endpoint", "Updated auth"],
            model="test-model",
        )
        assert result is not None
        assert result["summary"] == "The team migrated the API."
        assert result["actors"] == ["api-team"]
        assert result["confidence"] == 0.85

    async def test_empty_members_returns_none(self) -> None:
        result = await synthesize_episode_summary(
            episode_title="Test", member_contents=[], model="test-model",
        )
        assert result is None
        self.mock_llm.assert_not_called()

    async def test_llm_exception_returns_none(self) -> None:
        self.mock_llm.side_effect = RuntimeError("LLM error")
        result = await synthesize_episode_summary(
            episode_title="Test",
            member_contents=["content"],
            model="test-model",
        )
        assert result is None

    async def test_non_dict_response_returns_none(self) -> None:
        self.mock_llm.return_value = ["not", "a", "dict"]
        result = await synthesize_episode_summary(
            episode_title="Test",
            member_contents=["content"],
            model="test-model",
        )
        assert result is None

    async def test_confidence_clamped(self) -> None:
        self.mock_llm.return_value = {
            "summary": "test",
            "confidence": 1.5,
        }
        result = await synthesize_episode_summary(
            episode_title="Test",
            member_contents=["content"],
            model="test-model",
        )
        assert result is not None
        assert result["confidence"] == 1.0

    async def test_members_truncated(self) -> None:
        """Long member content should be truncated in the prompt."""
        self.mock_llm.return_value = {
            "summary": "test",
            "confidence": 0.8,
        }
        long_content = "x" * 5000
        await synthesize_episode_summary(
            episode_title="Test",
            member_contents=[long_content],
            model="test-model",
        )
        # Verify prompt was called and content was truncated
        call_args = self.mock_llm.call_args
        prompt = call_args[0][0]
        assert len(prompt) < 5000


class TestSynthesizeStateTrajectory:
    """Tests for synthesize_state_trajectory."""

    @pytest.fixture(autouse=True)
    def _patch_llm(self):
        with patch(
            "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock:
            self.mock_llm = mock
            yield

    async def test_valid_response_returns_trajectory(self) -> None:
        self.mock_llm.return_value = {
            "narrative": "The service improved steadily.",
            "trend": "improving",
            "key_transitions": ["v1 to v2", "v2 to v3"],
            "confidence": 0.9,
        }
        result = await synthesize_state_trajectory(
            entity_name="api-service",
            state_key="version",
            states=[
                {"value": "v1", "timestamp": "2026-01-01"},
                {"value": "v2", "timestamp": "2026-02-01"},
                {"value": "v3", "timestamp": "2026-03-01"},
            ],
            model="test-model",
        )
        assert result is not None
        assert result["trend"] == "improving"
        assert len(result["key_transitions"]) == 2

    async def test_empty_states_returns_none(self) -> None:
        result = await synthesize_state_trajectory(
            entity_name="test", state_key="key", states=[], model="test-model",
        )
        assert result is None

    async def test_llm_exception_returns_none(self) -> None:
        self.mock_llm.side_effect = RuntimeError("fail")
        result = await synthesize_state_trajectory(
            entity_name="test",
            state_key="key",
            states=[{"value": "v1", "timestamp": "t1"}],
            model="test-model",
        )
        assert result is None


class TestSynthesizeRecurringPattern:
    """Tests for synthesize_recurring_pattern."""

    @pytest.fixture(autouse=True)
    def _patch_llm(self):
        with patch(
            "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock:
            self.mock_llm = mock
            yield

    async def test_valid_response_returns_pattern(self) -> None:
        self.mock_llm.return_value = {
            "pattern": "API migrations follow a common pattern.",
            "pattern_type": "workflow",
            "recurrence_count": 3,
            "confidence": 0.8,
            "key_entities": ["api", "auth"],
        }
        result = await synthesize_recurring_pattern(
            episode_summaries=["ep1 summary", "ep2 summary", "ep3 summary"],
            shared_entities=["api", "auth"],
            model="test-model",
        )
        assert result is not None
        assert result["pattern_type"] == "workflow"
        assert result["recurrence_count"] == 3

    async def test_empty_summaries_returns_none(self) -> None:
        result = await synthesize_recurring_pattern(
            episode_summaries=[], shared_entities=[], model="test-model",
        )
        assert result is None

    async def test_llm_exception_returns_none(self) -> None:
        self.mock_llm.side_effect = RuntimeError("fail")
        result = await synthesize_recurring_pattern(
            episode_summaries=["summary"],
            shared_entities=["entity"],
            model="test-model",
        )
        assert result is None
