"""Integration tests for GLiNER/regex fallback routing logic.

These tests verify the extract_entities() routing function works correctly
regardless of whether GLiNER is installed. No GLiNER dependency needed.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import extract_entities, extract_entity_names


class TestGlinerFallback:
    """Tests for the extract_entities() routing and fallback behavior."""

    def test_extract_entities_uses_regex_when_gliner_disabled(self):
        """With gliner_enabled=False, should use regex extraction."""
        config = NCMSConfig(db_path=":memory:", gliner_enabled=False)
        text = "PostgreSQL connection pooling with PgBouncer"

        result = extract_entities(text, config=config)
        regex_result = extract_entity_names(text)

        # Should produce identical results to regex
        assert result == regex_result

    def test_extract_entities_uses_regex_when_no_config(self):
        """With no config, should fall back to regex extraction."""
        text = "FastAPI backend with Redis caching"

        result = extract_entities(text, config=None)
        regex_result = extract_entity_names(text)

        assert result == regex_result

    def test_extract_entities_falls_back_when_gliner_not_installed(self, caplog):
        """With gliner_enabled=True but gliner not installed, should fall back to regex."""
        import sys

        config = NCMSConfig(db_path=":memory:", gliner_enabled=True)
        text = "PostgreSQL connection pooling with PgBouncer"

        # Remove the module from sys.modules if cached, then block the import
        saved = sys.modules.pop("ncms.infrastructure.extraction.gliner_extractor", None)
        try:
            # Setting a module to None in sys.modules causes ImportError on import
            with patch.dict(
                sys.modules,
                {"ncms.infrastructure.extraction.gliner_extractor": None},
            ):
                with caplog.at_level(logging.WARNING):
                    result = extract_entities(text, config=config)
        finally:
            # Restore original state
            if saved is not None:
                sys.modules["ncms.infrastructure.extraction.gliner_extractor"] = saved

        regex_result = extract_entity_names(text)
        assert result == regex_result
        assert any("gliner" in r.message.lower() for r in caplog.records)

    def test_extract_entities_falls_back_on_gliner_error(self, caplog):
        """If GLiNER raises a runtime error, should fall back to regex with warning."""
        config = NCMSConfig(db_path=":memory:", gliner_enabled=True)
        text = "PostgreSQL connection pooling with PgBouncer"

        with patch(
            "ncms.infrastructure.extraction.gliner_extractor.extract_entities_gliner",
            side_effect=RuntimeError("Model loading failed"),
        ):
            with caplog.at_level(logging.WARNING):
                result = extract_entities(text, config=config)

        regex_result = extract_entity_names(text)
        assert result == regex_result
        assert any("falling back to regex" in r.message.lower() for r in caplog.records)

    def test_extract_entities_returns_same_format_as_regex(self):
        """extract_entities() should always return list[dict] with 'name' and 'type'."""
        config = NCMSConfig(db_path=":memory:", gliner_enabled=False)
        text = "UserService calls GET /api/v2/users with JWT auth"

        result = extract_entities(text, config=config)

        assert isinstance(result, list)
        for entity in result:
            assert isinstance(entity, dict)
            assert "name" in entity
            assert "type" in entity
            assert isinstance(entity["name"], str)
            assert isinstance(entity["type"], str)

    def test_extract_entities_empty_text(self):
        """Empty text should return empty list regardless of config."""
        config_on = NCMSConfig(db_path=":memory:", gliner_enabled=True)
        config_off = NCMSConfig(db_path=":memory:", gliner_enabled=False)

        # With gliner disabled — regex handles it
        assert extract_entities("", config=config_off) == []
        assert extract_entities("a", config=config_off) == []

        # With gliner enabled but import will fail — falls back to regex
        assert extract_entities("", config=config_on) == []
