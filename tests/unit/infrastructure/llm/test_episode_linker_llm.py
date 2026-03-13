"""Tests for LLM-based episode linking fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ncms.infrastructure.llm.episode_linker_llm import suggest_episode_links


class TestSuggestEpisodeLinks:
    """Tests for suggest_episode_links."""

    @pytest.fixture(autouse=True)
    def _patch_llm(self):
        with patch(
            "ncms.infrastructure.llm.episode_linker_llm.call_llm_json",
            new_callable=AsyncMock,
        ) as mock:
            self.mock_llm = mock
            yield

    def _make_summaries(self) -> list[dict[str, str]]:
        return [
            {"id": "ep-1", "topic": "API migration", "entities": "api, users", "domains": "backend"},
            {"id": "ep-2", "topic": "Auth overhaul", "entities": "auth, tokens", "domains": "security"},
        ]

    async def test_valid_response_returns_matches(self) -> None:
        self.mock_llm.return_value = [
            {"episode_id": "ep-1", "confidence": 0.85, "reasoning": "API related"},
        ]
        result = await suggest_episode_links(
            fragment_content="New API endpoint added",
            fragment_entities=["api"],
            fragment_domains=["backend"],
            fragment_agent="api-agent",
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert len(result) == 1
        assert result[0]["episode_id"] == "ep-1"
        assert result[0]["confidence"] == 0.85

    async def test_unknown_episode_id_filtered(self) -> None:
        self.mock_llm.return_value = [
            {"episode_id": "ep-unknown", "confidence": 0.9, "reasoning": "match"},
        ]
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert len(result) == 0

    async def test_empty_summaries_returns_empty(self) -> None:
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=[],
            model="test-model",
        )
        assert result == []
        self.mock_llm.assert_not_called()

    async def test_non_list_response_returns_empty(self) -> None:
        self.mock_llm.return_value = {"not": "a list"}
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert result == []

    async def test_llm_exception_returns_empty(self) -> None:
        self.mock_llm.side_effect = RuntimeError("LLM error")
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert result == []

    async def test_confidence_clamped(self) -> None:
        self.mock_llm.return_value = [
            {"episode_id": "ep-1", "confidence": 1.5, "reasoning": "over"},
        ]
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert result[0]["confidence"] == 1.0

    async def test_multiple_matches_returned(self) -> None:
        self.mock_llm.return_value = [
            {"episode_id": "ep-1", "confidence": 0.8, "reasoning": "api"},
            {"episode_id": "ep-2", "confidence": 0.6, "reasoning": "auth"},
        ]
        result = await suggest_episode_links(
            fragment_content="API auth changes",
            fragment_entities=["api", "auth"],
            fragment_domains=["backend"],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert len(result) == 2

    async def test_reasoning_truncated(self) -> None:
        self.mock_llm.return_value = [
            {"episode_id": "ep-1", "confidence": 0.8, "reasoning": "x" * 500},
        ]
        result = await suggest_episode_links(
            fragment_content="content",
            fragment_entities=[],
            fragment_domains=[],
            fragment_agent=None,
            episode_summaries=self._make_summaries(),
            model="test-model",
        )
        assert len(result[0]["reasoning"]) <= 200
