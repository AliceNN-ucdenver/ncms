"""Unit tests for GLiNER-based entity extraction.

These tests require the gliner package to be installed (pip install ncms[gliner]).
They are automatically skipped when gliner is not available.
"""

from __future__ import annotations

import pytest

gliner = pytest.importorskip("gliner", reason="GLiNER not installed (pip install ncms[gliner])")

from ncms.infrastructure.extraction.gliner_extractor import (
    DEFAULT_LABELS,
    _MAX_ENTITIES,
    extract_entities_gliner,
)


class TestGlinerExtractor:
    """Tests for the GLiNER entity extractor."""

    def test_extract_returns_correct_format(self):
        """Extracted entities should be list[dict] with 'name' and 'type' keys."""
        results = extract_entities_gliner(
            "PostgreSQL database stores user data via FastAPI REST endpoints"
        )
        assert isinstance(results, list)
        for entity in results:
            assert isinstance(entity, dict)
            assert "name" in entity
            assert "type" in entity
            assert isinstance(entity["name"], str)
            assert isinstance(entity["type"], str)
            assert len(entity["name"]) >= 2

    def test_extract_finds_technology_entities(self):
        """Should extract technology-related entities from technical text."""
        results = extract_entities_gliner(
            "PostgreSQL connection pooling with Redis caching for the FastAPI backend"
        )
        names_lower = {e["name"].lower() for e in results}
        # Should find at least one known technology
        found_tech = names_lower & {"postgresql", "redis", "fastapi"}
        assert len(found_tech) >= 1, f"Expected technology entities, got: {results}"

    def test_dedup_by_lowercase_name(self):
        """No duplicate entities when the same name appears multiple times."""
        results = extract_entities_gliner(
            "PostgreSQL is great. We use PostgreSQL for everything. PostgreSQL rocks."
        )
        names_lower = [e["name"].lower() for e in results]
        assert len(names_lower) == len(set(names_lower)), (
            f"Duplicate entities found: {names_lower}"
        )

    def test_max_entities_cap(self):
        """Should never return more than _MAX_ENTITIES."""
        # Create text with many potential entities
        techs = [
            "PostgreSQL", "Redis", "Kafka", "Docker", "Kubernetes",
            "React", "Angular", "Vue", "Django", "Flask",
            "FastAPI", "Express", "MongoDB", "Elasticsearch", "Cassandra",
            "RabbitMQ", "NATS", "Celery", "NGINX", "Apache",
            "Terraform", "Ansible", "Jenkins", "GitLab", "Prometheus",
        ]
        text = "Our stack uses " + ", ".join(techs) + " for various purposes."
        results = extract_entities_gliner(text)
        assert len(results) <= _MAX_ENTITIES

    def test_empty_text_returns_empty(self):
        """Empty or very short text should return an empty list."""
        assert extract_entities_gliner("") == []
        assert extract_entities_gliner("a") == []

    def test_entity_types_match_labels(self):
        """Extracted entity types should be from the default label set."""
        results = extract_entities_gliner(
            "The authentication service uses JWT tokens and connects to PostgreSQL"
        )
        for entity in results:
            assert entity["type"] in DEFAULT_LABELS, (
                f"Entity type '{entity['type']}' not in DEFAULT_LABELS"
            )

    def test_threshold_filters_low_confidence(self):
        """Higher threshold should produce fewer (or equal) entities."""
        text = "PostgreSQL database with Redis caching and FastAPI REST API"
        results_low = extract_entities_gliner(text, threshold=0.1)
        results_high = extract_entities_gliner(text, threshold=0.8)
        assert len(results_high) <= len(results_low)

    def test_plain_text_extracts_concepts(self):
        """GLiNER should extract semantic concepts that regex would miss."""
        results = extract_entities_gliner(
            "The authentication flow handles access control and session management"
        )
        # GLiNER should find conceptual entities like authentication, access control
        # (regex extractor would miss these since they're not PascalCase or in tech list)
        assert len(results) >= 1, "Should extract at least one concept entity"
