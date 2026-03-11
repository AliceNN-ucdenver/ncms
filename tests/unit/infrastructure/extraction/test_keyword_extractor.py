"""Unit tests for LLM-based keyword extraction.

These tests mock litellm to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncms.infrastructure.extraction.keyword_extractor import extract_keywords

_PATCH_TARGET = "litellm.acompletion"


def _mock_llm_response(keywords: list[dict]) -> MagicMock:
    """Create a mock litellm acompletion response with the given keyword payload."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(keywords)
    return response


class TestKeywordExtractor:
    """Tests for the keyword extractor in isolation (mock litellm)."""

    @pytest.mark.asyncio
    async def test_extract_keywords_returns_correct_format(self):
        """Extracted keywords should be list[dict] with 'name' and 'type' keys."""
        mock_keywords = [
            {"name": "authentication", "domain": "security"},
            {"name": "data persistence", "domain": "storage"},
        ]
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(mock_keywords),
        ):
            results = await extract_keywords(
                "User authentication with session persistence",
                existing_entities=[],
            )

        assert isinstance(results, list)
        assert len(results) == 2
        for kw in results:
            assert isinstance(kw, dict)
            assert "name" in kw
            assert "type" in kw
            assert kw["type"] == "keyword"

    @pytest.mark.asyncio
    async def test_dedup_against_existing_entities(self):
        """Keywords should not duplicate existing entities (case-insensitive)."""
        mock_keywords = [
            {"name": "PostgreSQL", "domain": "database"},
            {"name": "data persistence", "domain": "storage"},
            {"name": "caching", "domain": "performance"},
        ]
        existing = [
            {"name": "PostgreSQL", "type": "technology"},
            {"name": "Caching", "type": "concept"},
        ]
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(mock_keywords),
        ):
            results = await extract_keywords(
                "PostgreSQL database with caching layer",
                existing_entities=existing,
            )

        names_lower = {r["name"].lower() for r in results}
        assert "postgresql" not in names_lower
        assert "caching" not in names_lower
        assert "data persistence" in names_lower

    @pytest.mark.asyncio
    async def test_max_keywords_cap(self):
        """Should respect the max_keywords parameter."""
        mock_keywords = [
            {"name": f"concept_{i}", "domain": "test"} for i in range(15)
        ]
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(mock_keywords),
        ):
            results = await extract_keywords(
                "A very complex text with many concepts",
                existing_entities=[],
                max_keywords=5,
            )

        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty(self):
        """Empty or very short text should return empty list without calling LLM."""
        results_empty = await extract_keywords("", existing_entities=[])
        results_short = await extract_keywords("hi", existing_entities=[])

        assert results_empty == []
        assert results_short == []

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        """LLM errors should result in empty list (non-fatal)."""
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            side_effect=RuntimeError("API rate limit exceeded"),
        ):
            results = await extract_keywords(
                "Some technical content about databases",
                existing_entities=[],
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        """Malformed LLM response should result in empty list."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "This is not valid JSON at all"

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=response,
        ):
            results = await extract_keywords(
                "Some content that produces garbage output",
                existing_entities=[],
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_code_fenced_json_response(self):
        """LLM responses wrapped in ```json fences should be parsed correctly."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = (
            '```json\n[{"name": "authentication", "domain": "security"}]\n```'
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=response,
        ):
            results = await extract_keywords(
                "User login with session management",
                existing_entities=[],
            )

        assert len(results) == 1
        assert results[0]["name"] == "authentication"
        assert results[0]["type"] == "keyword"

    @pytest.mark.asyncio
    async def test_dedup_within_keywords(self):
        """Duplicate keywords in the LLM response should be deduplicated."""
        mock_keywords = [
            {"name": "security", "domain": "auth"},
            {"name": "Security", "domain": "auth"},
            {"name": "SECURITY", "domain": "auth"},
            {"name": "access control", "domain": "auth"},
        ]
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(mock_keywords),
        ):
            results = await extract_keywords(
                "Security and access control mechanisms",
                existing_entities=[],
            )

        names_lower = [r["name"].lower() for r in results]
        assert len(names_lower) == len(set(names_lower)), (
            f"Duplicate keywords found: {names_lower}"
        )

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_empty(self):
        """Empty response content from LLM should return empty list."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = ""

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=response,
        ):
            results = await extract_keywords(
                "Some content here",
                existing_entities=[],
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_non_list_json_returns_empty(self):
        """If LLM returns valid JSON but not a list, should return empty."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = '{"name": "not a list"}'

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=response,
        ):
            results = await extract_keywords(
                "Some content",
                existing_entities=[],
            )

        assert results == []
