"""Unit tests for the entity extraction label resolution module."""

from ncms.domain.entity_extraction import MAX_ENTITIES, UNIVERSAL_LABELS, resolve_labels


class TestUniversalLabels:
    def test_universal_labels_is_list(self):
        assert isinstance(UNIVERSAL_LABELS, list)
        assert len(UNIVERSAL_LABELS) >= 5

    def test_universal_labels_are_strings(self):
        for label in UNIVERSAL_LABELS:
            assert isinstance(label, str)
            assert len(label) >= 2

    def test_universal_labels_contains_core_types(self):
        """Should include broad, domain-agnostic entity types."""
        for expected in ["person", "organization", "technology", "concept"]:
            assert expected in UNIVERSAL_LABELS

    def test_max_entities_is_positive_int(self):
        assert isinstance(MAX_ENTITIES, int)
        assert MAX_ENTITIES > 0


class TestResolveLabels:
    def test_no_cache_returns_universal(self):
        """Without cached labels, should return universal defaults."""
        result = resolve_labels(["api"], cached_labels=None)
        assert result == UNIVERSAL_LABELS

    def test_empty_domains_returns_universal(self):
        result = resolve_labels([], cached_labels={"api": ["endpoint"]})
        assert result == UNIVERSAL_LABELS

    def test_empty_cache_dict_returns_universal(self):
        result = resolve_labels(["api"], cached_labels={})
        assert result == UNIVERSAL_LABELS

    def test_cached_labels_replace_universal_by_default(self):
        """Cached domain labels REPLACE universal labels (replace mode is default)."""
        cached = {"api": ["endpoint", "service", "protocol"]}
        result = resolve_labels(["api"], cached_labels=cached)
        # Domain labels only (replace mode)
        assert result == ["endpoint", "service", "protocol"]
        # Universal labels NOT included in replace mode
        for label in UNIVERSAL_LABELS:
            if label not in ["endpoint", "service", "protocol"]:
                assert label not in result

    def test_cached_labels_merged_with_keep_universal(self):
        """With keep_universal=True, domain labels merge with universals."""
        cached = {"api": ["endpoint", "service", "protocol"]}
        result = resolve_labels(["api"], cached_labels=cached, keep_universal=True)
        # Universal labels come first
        for label in UNIVERSAL_LABELS:
            assert label in result
        # Domain labels added on top
        for label in ["endpoint", "service", "protocol"]:
            assert label in result
        assert len(result) == len(UNIVERSAL_LABELS) + 3

    def test_multi_domain_merge(self):
        """Labels from multiple domains should be merged."""
        cached = {
            "api": ["endpoint", "service"],
            "db": ["table", "column"],
        }
        result = resolve_labels(["api", "db"], cached_labels=cached)
        assert "endpoint" in result
        assert "service" in result
        assert "table" in result
        assert "column" in result

    def test_dedup_across_domains(self):
        """Duplicate labels across domains should be deduplicated."""
        cached = {
            "api": ["service", "endpoint"],
            "auth": ["service", "token"],
        }
        result = resolve_labels(["api", "auth"], cached_labels=cached)
        lower_result = [r.lower() for r in result]
        assert lower_result.count("service") == 1
        assert "endpoint" in lower_result
        assert "token" in lower_result

    def test_dedup_is_case_insensitive(self):
        """Deduplication should be case-insensitive."""
        cached = {
            "api": ["Service"],
            "auth": ["service"],
        }
        result = resolve_labels(["api", "auth"], cached_labels=cached)
        service_count = sum(1 for r in result if r.lower() == "service")
        assert service_count == 1

    def test_domain_not_in_cache_falls_back(self):
        """If requested domain has no cached labels, should fall back to universal."""
        cached = {"api": ["endpoint"]}
        result = resolve_labels(["finance"], cached_labels=cached)
        assert result == UNIVERSAL_LABELS

    def test_partial_cache_replaces_with_available(self):
        """If some domains have cached labels, use domain labels only (replace mode).

        Domain 'api' has cached labels, 'finance' does not. In replace mode,
        only the cached domain labels are returned (universals not merged).
        """
        cached = {"api": ["endpoint", "service"]}
        result = resolve_labels(["api", "finance"], cached_labels=cached)
        # Domain labels used (replace mode)
        assert "endpoint" in result
        assert "service" in result
        # Universal labels NOT included in replace mode
        assert "person" not in result

    def test_partial_cache_merges_with_keep_universal(self):
        """With keep_universal=True, partial cache merges with universals."""
        cached = {"api": ["endpoint", "service"]}
        result = resolve_labels(
            ["api", "finance"], cached_labels=cached, keep_universal=True,
        )
        # Universal labels included
        for label in UNIVERSAL_LABELS:
            assert label in result
        # Domain labels merged on top
        assert "endpoint" in result
        assert "service" in result

    def test_returns_copy_not_reference(self):
        """Should return a new list, not a reference to UNIVERSAL_LABELS."""
        result = resolve_labels([], cached_labels=None)
        assert result == UNIVERSAL_LABELS
        assert result is not UNIVERSAL_LABELS
