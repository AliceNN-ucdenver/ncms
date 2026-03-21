"""Tests for domain watch models."""

from __future__ import annotations

from datetime import UTC, datetime

from ncms.domain.watch import (
    DomainClassification,
    DomainSource,
    FileChangeEvent,
    FileChangeType,
    WatchRule,
    WatchStats,
)


class TestFileChangeType:
    def test_values(self) -> None:
        assert FileChangeType.CREATED.value == "created"
        assert FileChangeType.MODIFIED.value == "modified"
        assert FileChangeType.DELETED.value == "deleted"


class TestDomainSource:
    def test_values(self) -> None:
        assert DomainSource.EXPLICIT.value == "explicit"
        assert DomainSource.PATH_RULE.value == "path_rule"
        assert DomainSource.DIRECTORY_NAME.value == "directory_name"
        assert DomainSource.EXTENSION.value == "extension"
        assert DomainSource.FALLBACK.value == "fallback"


class TestWatchRule:
    def test_creation(self) -> None:
        rule = WatchRule(pattern="docs/**", domain="documentation", priority=5)
        assert rule.pattern == "docs/**"
        assert rule.domain == "documentation"
        assert rule.priority == 5

    def test_default_priority(self) -> None:
        rule = WatchRule(pattern="*", domain="general")
        assert rule.priority == 0


class TestDomainClassification:
    def test_creation(self) -> None:
        dc = DomainClassification(
            domain="api",
            source=DomainSource.DIRECTORY_NAME,
            confidence=0.7,
        )
        assert dc.domain == "api"
        assert dc.source == DomainSource.DIRECTORY_NAME
        assert dc.confidence == 0.7

    def test_default_confidence(self) -> None:
        dc = DomainClassification(domain="test", source=DomainSource.EXPLICIT)
        assert dc.confidence == 1.0


class TestFileChangeEvent:
    def test_creation(self) -> None:
        ts = datetime.now(UTC)
        event = FileChangeEvent(
            path="/some/file.md",
            change_type=FileChangeType.CREATED,
            timestamp=ts,
        )
        assert event.path == "/some/file.md"
        assert event.change_type == FileChangeType.CREATED
        assert event.timestamp == ts
        assert event.size_bytes == 0


class TestWatchStats:
    def test_defaults(self) -> None:
        stats = WatchStats()
        assert stats.files_ingested == 0
        assert stats.files_skipped_hash == 0
        assert stats.files_skipped_unsupported == 0
        assert stats.files_errored == 0
        assert stats.domains_detected == set()
        assert stats.total_memories_created == 0

    def test_to_dict(self) -> None:
        stats = WatchStats(
            files_ingested=10,
            files_skipped_hash=5,
            files_skipped_unsupported=2,
            files_errored=1,
            domains_detected={"code", "api"},
            total_memories_created=42,
        )
        d = stats.to_dict()
        assert d["files_ingested"] == 10
        assert d["files_skipped_hash"] == 5
        assert d["files_skipped_unsupported"] == 2
        assert d["files_errored"] == 1
        assert d["domains_detected"] == ["api", "code"]  # sorted
        assert d["total_memories_created"] == 42

    def test_to_dict_empty_domains(self) -> None:
        stats = WatchStats()
        d = stats.to_dict()
        assert d["domains_detected"] == []
