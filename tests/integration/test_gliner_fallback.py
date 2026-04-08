"""Integration tests for GLiNER entity extraction (now always used).

GLiNER is a required dependency — regex fallback no longer exists.
These tests verify the extraction pipeline works end-to-end.
"""

from __future__ import annotations

from ncms.domain.entity_extraction import UNIVERSAL_LABELS, resolve_labels
from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner


class TestGlinerAlwaysUsed:
    """Tests that GLiNER extraction is the sole extraction path."""

    def test_gliner_extracts_from_technical_text(self):
        """GLiNER should extract entities from technical content."""
        results = extract_entities_gliner(
            "PostgreSQL connection pooling with PgBouncer"
        )
        assert isinstance(results, list)
        assert len(results) >= 1
        names_lower = {e["name"].lower() for e in results}
        assert "postgresql" in names_lower or "pgbouncer" in names_lower

    def test_gliner_extracts_from_non_technical_text(self):
        """GLiNER should extract entities from general domain content."""
        results = extract_entities_gliner(
            "Apple Inc. acquired a startup in San Francisco last Tuesday"
        )
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_label_resolution_integrates_with_extraction(self):
        """Resolved labels should flow through to GLiNER extraction.

        In replace mode (default), domain labels replace universals.
        """
        cached = {"finance": ["company", "stock", "market"]}
        labels = resolve_labels(["finance"], cached_labels=cached)
        # Domain labels replace universal labels (replace mode)
        for domain_label in ["company", "stock", "market"]:
            assert domain_label in labels
        # Universal labels NOT included in replace mode
        assert "person" not in labels

        # These labels work with GLiNER
        results = extract_entities_gliner(
            "Apple stock rose after the company announced quarterly earnings",
            labels=labels,
        )
        assert isinstance(results, list)
        for entity in results:
            assert entity["type"] in labels

    def test_domain_cached_labels_replace_universal(self):
        """Domain-cached labels REPLACE universal defaults (replace mode)."""
        cached = {"biomedical": ["disease", "drug", "protein", "gene"]}
        labels = resolve_labels(["biomedical"], cached_labels=cached)
        assert "disease" in labels
        assert "person" not in labels  # Replace mode: universals excluded

    def test_domain_cached_labels_merged_with_keep_universal(self):
        """With keep_universal=True, domain labels merge with universals."""
        cached = {"biomedical": ["disease", "drug", "protein", "gene"]}
        labels = resolve_labels(
            ["biomedical"], cached_labels=cached, keep_universal=True,
        )
        assert "disease" in labels
        assert "person" in labels  # Universal labels included in additive mode

    def test_empty_text_returns_empty(self):
        """Empty or very short text should return empty list."""
        assert extract_entities_gliner("") == []
        assert extract_entities_gliner("a") == []

    def test_returns_correct_format(self):
        """Entities should be list[dict] with 'name' and 'type' keys."""
        results = extract_entities_gliner(
            "UserService calls GET /api/v2/users with JWT auth"
        )
        assert isinstance(results, list)
        for entity in results:
            assert isinstance(entity, dict)
            assert "name" in entity
            assert "type" in entity
            assert isinstance(entity["name"], str)
            assert isinstance(entity["type"], str)

    def test_universal_labels_used_without_cache(self):
        """Without cached labels, extraction should use UNIVERSAL_LABELS."""
        labels = resolve_labels(["unknown_domain"], cached_labels=None)
        assert labels == UNIVERSAL_LABELS

        results = extract_entities_gliner(
            "PostgreSQL and Redis are technologies used by the organization",
            labels=labels,
        )
        for entity in results:
            assert entity["type"] in UNIVERSAL_LABELS
