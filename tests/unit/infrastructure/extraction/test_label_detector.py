"""Unit tests for the LLM-based label detector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDetectLabels:
    """Tests for the detect_labels() async function."""

    @pytest.mark.asyncio
    async def test_returns_labels_from_llm(self):
        """Should return parsed labels from a successful LLM response."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content='["endpoint", "service", "protocol"]'))
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("api", ["Sample API documentation text"])

        assert labels == ["endpoint", "service", "protocol"]

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        """Should strip markdown code fences from LLM response."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content='```json\n["endpoint", "service"]\n```'
                )
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("api", ["Sample text"])

        assert labels == ["endpoint", "service"]

    @pytest.mark.asyncio
    async def test_caps_at_15_labels(self):
        """Should return at most 15 labels."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        many_labels = [f"label_{i}" for i in range(25)]
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=str(many_labels).replace("'", '"')
                )
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("test", ["Sample text"])

        assert len(labels) <= 15

    @pytest.mark.asyncio
    async def test_empty_samples_returns_empty(self):
        """Empty sample list should return empty labels without calling LLM."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        labels = await detect_labels("api", [])
        assert labels == []

    @pytest.mark.asyncio
    async def test_error_returns_empty(self):
        """LLM errors should return empty list (non-fatal)."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        with patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            labels = await detect_labels("api", ["Sample text"])

        assert labels == []

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(self):
        """Non-JSON LLM response should return empty list."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="not valid json at all"))
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("api", ["Sample text"])

        assert labels == []

    @pytest.mark.asyncio
    async def test_filters_non_string_labels(self):
        """Should filter out non-string items from the JSON array."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(content='["endpoint", 42, null, "service"]')
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("api", ["Sample text"])

        assert labels == ["endpoint", "service"]

    @pytest.mark.asyncio
    async def test_filters_labels_too_long(self):
        """Labels longer than 50 chars should be filtered out."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content='["endpoint", "' + "x" * 60 + '", "service"]'
                )
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            labels = await detect_labels("api", ["Sample text"])

        assert labels == ["endpoint", "service"]

    @pytest.mark.asyncio
    async def test_ollama_model_disables_think(self):
        """Ollama models should have think=False in kwargs."""
        from ncms.infrastructure.extraction.label_detector import detect_labels

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content='["endpoint"]'))
        ]

        with patch(
            "litellm.acompletion", new_callable=AsyncMock, return_value=mock_response
        ) as mock_completion:
            await detect_labels(
                "api", ["Sample text"], model="ollama_chat/qwen3.5:35b-a3b"
            )

        call_kwargs = mock_completion.call_args
        assert call_kwargs.kwargs.get("think") is False or (
            "think" in (call_kwargs[1] if len(call_kwargs) > 1 else {})
        )
