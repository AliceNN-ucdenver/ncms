"""Unit tests for LLM-based pattern synthesis.

Tests that synthesize_insight correctly calls litellm, parses JSON responses,
handles errors gracefully, and respects api_base configuration.

All tests mock litellm.acompletion to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncms.domain.models import Memory
from ncms.infrastructure.consolidation.clusterer import MemoryCluster
from ncms.infrastructure.consolidation.synthesizer import synthesize_insight

_PATCH_TARGET = "litellm.acompletion"


def _make_cluster(
    num_memories: int = 3,
    domains: set[str] | None = None,
    shared_entities: set[str] | None = None,
) -> MemoryCluster:
    """Create a test MemoryCluster."""
    memories = [
        Memory(content=f"Test memory content {i}", domains=list(domains or {"test"}))
        for i in range(num_memories)
    ]
    return MemoryCluster(
        memories=memories,
        shared_entity_ids=shared_entities or {"ent-1", "ent-2"},
        domains=domains or {"test"},
    )


def _mock_llm_response(content: str) -> MagicMock:
    """Create a mock litellm acompletion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


class TestSynthesizeInsight:
    """Tests for synthesize_insight()."""

    @pytest.mark.asyncio
    async def test_synthesize_returns_insight_dict(self):
        """Valid JSON response should be parsed into an insight dict."""
        result_json = json.dumps(
            {
                "insight": "These memories reveal a dependency pattern between auth and DB.",
                "pattern_type": "dependency",
                "confidence": 0.85,
                "key_entities": ["auth-service", "postgres"],
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is not None
        assert "insight" in result
        assert result["pattern_type"] == "dependency"
        assert result["confidence"] == 0.85
        assert "auth-service" in result["key_entities"]

    @pytest.mark.asyncio
    async def test_synthesize_with_api_base(self):
        """api_base should be passed to litellm when provided."""
        result_json = json.dumps(
            {
                "insight": "Test insight",
                "pattern_type": "architecture",
                "confidence": 0.7,
                "key_entities": [],
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ) as mock_llm:
            await synthesize_insight(
                _make_cluster(),
                model="openai/llama-3",
                api_base="http://localhost:8000/v1",
            )

            # Verify api_base was passed to litellm
            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["api_base"] == "http://localhost:8000/v1"
            assert call_kwargs["model"] == "openai/llama-3"

    @pytest.mark.asyncio
    async def test_synthesize_without_api_base(self):
        """api_base should not be in kwargs when not provided."""
        result_json = json.dumps(
            {
                "insight": "Test insight",
                "pattern_type": "workflow",
                "confidence": 0.6,
                "key_entities": [],
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ) as mock_llm:
            await synthesize_insight(_make_cluster())

            call_kwargs = mock_llm.call_args[1]
            assert "api_base" not in call_kwargs

    @pytest.mark.asyncio
    async def test_synthesize_error_returns_none(self):
        """LLM error should return None (non-fatal)."""
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM API unavailable"),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_malformed_json_returns_none(self):
        """Garbage response should return None."""
        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response("This is not JSON at all"),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_missing_insight_field_returns_none(self):
        """JSON without 'insight' key should return None."""
        result_json = json.dumps(
            {
                "pattern_type": "dependency",
                "confidence": 0.5,
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_code_fenced_response(self):
        """Handles ```json wrapping around the response."""
        inner_json = json.dumps(
            {
                "insight": "A pattern from code-fenced response",
                "pattern_type": "impact",
                "confidence": 0.9,
                "key_entities": ["service-a"],
            }
        )
        fenced = f"```json\n{inner_json}\n```"

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(fenced),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is not None
        assert result["insight"] == "A pattern from code-fenced response"
        assert result["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_synthesize_empty_cluster(self):
        """Empty cluster should return None."""
        cluster = MemoryCluster(memories=[], shared_entity_ids=set(), domains=set())

        # Should not even call LLM
        result = await synthesize_insight(cluster)
        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_normalizes_fields(self):
        """Fields should be normalized to correct types."""
        result_json = json.dumps(
            {
                "insight": 42,  # Not a string — should be cast
                "pattern_type": "architecture",
                "confidence": "0.75",  # String — should be cast to float
                "key_entities": ["e1"],
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ):
            result = await synthesize_insight(_make_cluster())

        assert result is not None
        assert isinstance(result["insight"], str)
        assert isinstance(result["confidence"], float)
        assert result["confidence"] == 0.75

    @pytest.mark.asyncio
    async def test_memory_content_truncated_in_prompt(self):
        """Long memory content should be truncated at 2000 chars in the prompt."""
        long_content = "x" * 3000
        cluster = MemoryCluster(
            memories=[Memory(content=long_content, domains=["test"])],
            shared_entity_ids={"e1"},
            domains={"test"},
        )

        result_json = json.dumps(
            {
                "insight": "Test",
                "pattern_type": "dependency",
                "confidence": 0.5,
                "key_entities": [],
            }
        )

        with patch(
            _PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=_mock_llm_response(result_json),
        ) as mock_llm:
            await synthesize_insight(cluster)

            # Check that content was truncated in the prompt
            call_kwargs = mock_llm.call_args[1]
            prompt_content = call_kwargs["messages"][0]["content"]
            # The full 3000-char content should not appear
            assert long_content not in prompt_content
            # But the first 2000 chars should
            assert long_content[:2000] in prompt_content
