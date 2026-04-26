"""Unit tests for LLM-based contradiction detection.

Mocks litellm to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncms.domain.models import Memory
from ncms.infrastructure.llm.contradiction_detector import detect_contradictions


def _make_memory(content: str, memory_id: str | None = None) -> Memory:
    mem = Memory(content=content, type="fact")
    if memory_id:
        mem.id = memory_id
    return mem


def _mock_response(content: str) -> MagicMock:
    """Build a mock litellm response with the given content."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


class TestContradictionDetector:
    @pytest.mark.asyncio
    async def test_detect_returns_correct_format(self):
        """Valid contradiction JSON should be parsed correctly."""
        new = _make_memory("Python 3.12 is the latest version")
        existing = _make_memory("Python 3.11 is the latest version", "mem-old")

        result_json = json.dumps(
            [
                {
                    "existing_memory_id": "mem-old",
                    "contradiction_type": "temporal",
                    "explanation": "Version mismatch",
                    "severity": "high",
                }
            ]
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response(result_json)
            result = await detect_contradictions(new, [existing])

        assert len(result) == 1
        assert result[0]["existing_memory_id"] == "mem-old"
        assert result[0]["contradiction_type"] == "temporal"
        assert result[0]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_no_contradictions_returns_empty(self):
        """When LLM finds no contradictions, should return empty list."""
        new = _make_memory("Flask uses Jinja2")
        existing = _make_memory("Flask supports Werkzeug", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response("[]")
            result = await detect_contradictions(new, [existing])

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_existing_returns_empty(self):
        """With no existing memories, should return empty without LLM call."""
        new = _make_memory("something")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            result = await detect_contradictions(new, [])

        mock_llm.assert_not_called()
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        """LLM failure should return empty list (non-fatal)."""
        new = _make_memory("test content")
        existing = _make_memory("other content", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("LLM unavailable")
            result = await detect_contradictions(new, [existing])

        assert result == []

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        """Garbage LLM response should return empty list."""
        new = _make_memory("content")
        existing = _make_memory("other", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response("not valid json at all")
            result = await detect_contradictions(new, [existing])

        assert result == []

    @pytest.mark.asyncio
    async def test_code_fenced_response_parsed(self):
        """JSON wrapped in markdown code fences should be parsed correctly."""
        new = _make_memory("API uses JWT")
        existing = _make_memory("API uses session cookies", "mem-api")

        inner_json = json.dumps(
            [
                {
                    "existing_memory_id": "mem-api",
                    "contradiction_type": "configuration",
                    "explanation": "Auth mechanism differs",
                    "severity": "medium",
                }
            ]
        )
        fenced = f"```json\n{inner_json}\n```"

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response(fenced)
            result = await detect_contradictions(new, [existing])

        assert len(result) == 1
        assert result[0]["existing_memory_id"] == "mem-api"

    @pytest.mark.asyncio
    async def test_hallucinated_memory_id_filtered_out(self):
        """Memory IDs not in the candidate set should be filtered out."""
        new = _make_memory("new info")
        existing = _make_memory("old info", "mem-real")

        result_json = json.dumps(
            [
                {
                    "existing_memory_id": "mem-hallucinated",
                    "contradiction_type": "factual",
                    "explanation": "made up",
                    "severity": "high",
                },
                {
                    "existing_memory_id": "mem-real",
                    "contradiction_type": "factual",
                    "explanation": "real conflict",
                    "severity": "medium",
                },
            ]
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response(result_json)
            result = await detect_contradictions(new, [existing])

        # Only the real ID should remain
        assert len(result) == 1
        assert result[0]["existing_memory_id"] == "mem-real"

    @pytest.mark.asyncio
    async def test_non_list_json_returns_empty(self):
        """If LLM returns a JSON object instead of array, return empty."""
        new = _make_memory("test")
        existing = _make_memory("other", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response('{"not": "an array"}')
            result = await detect_contradictions(new, [existing])

        assert result == []

    @pytest.mark.asyncio
    async def test_api_base_passed_to_litellm(self):
        """api_base should be forwarded to litellm.acompletion."""
        new = _make_memory("new")
        existing = _make_memory("old", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response("[]")
            await detect_contradictions(new, [existing], api_base="http://localhost:8000/v1")

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["api_base"] == "http://localhost:8000/v1"

    @pytest.mark.asyncio
    async def test_api_base_not_in_kwargs_when_none(self):
        """api_base should not appear in kwargs when not provided."""
        new = _make_memory("new")
        existing = _make_memory("old", "mem-1")

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _mock_response("[]")
            await detect_contradictions(new, [existing])

        call_kwargs = mock_llm.call_args[1]
        assert "api_base" not in call_kwargs
