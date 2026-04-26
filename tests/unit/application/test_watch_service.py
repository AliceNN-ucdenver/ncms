"""Tests for WatchService — domain classification and file hash tracking."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ncms.application.watch_service import (
    DIRECTORY_DOMAIN_MAP,
    EXTENSION_DOMAIN_MAP,
    WatchService,
)
from ncms.domain.watch import (
    DomainSource,
    FileChangeEvent,
    FileChangeType,
    WatchRule,
)


@pytest.fixture
def memory_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.store = AsyncMock()
    svc.store.get_consolidation_value = AsyncMock(return_value=None)
    svc.store.set_consolidation_value = AsyncMock()
    return svc


@pytest.fixture
def watch_svc(memory_svc: AsyncMock) -> WatchService:
    return WatchService(memory_svc)


class TestDomainClassification:
    """Tests for classify_domain heuristics."""

    def test_explicit_domain_override(self, memory_svc: AsyncMock) -> None:
        """Explicit --domain flag takes priority over all heuristics."""
        svc = WatchService(memory_svc, default_domain="my-domain")
        result = svc.classify_domain(Path("/some/path/file.md"))
        assert result.domain == "my-domain"
        assert result.source == DomainSource.EXPLICIT
        assert result.confidence == 1.0

    def test_path_rule_matching(self, memory_svc: AsyncMock) -> None:
        """Path rules match before directory/extension heuristics."""
        rules = [WatchRule(pattern="api/**", domain="api-docs", priority=10)]
        svc = WatchService(memory_svc, rules=rules)
        result = svc.classify_domain(
            Path("/project/api/spec.yaml"),
            watch_root=Path("/project"),
        )
        assert result.domain == "api-docs"
        assert result.source == DomainSource.PATH_RULE

    def test_directory_name_mapping(self, watch_svc: WatchService) -> None:
        """Parent directory name maps to domain."""
        result = watch_svc.classify_domain(Path("/project/docs/readme.md"))
        assert result.domain == "documentation"
        assert result.source == DomainSource.DIRECTORY_NAME

    def test_directory_name_mapping_all_entries(self) -> None:
        """All entries in DIRECTORY_DOMAIN_MAP are valid strings."""
        for key, value in DIRECTORY_DOMAIN_MAP.items():
            assert isinstance(key, str)
            assert isinstance(value, str)
            assert len(value) > 0

    def test_extension_mapping(self, watch_svc: WatchService) -> None:
        """File extension maps to domain when directory doesn't match."""
        result = watch_svc.classify_domain(Path("/project/misc/schema.sql"))
        assert result.domain == "database"
        assert result.source == DomainSource.EXTENSION

    def test_extension_mapping_all_entries(self) -> None:
        """All entries in EXTENSION_DOMAIN_MAP are valid."""
        for ext, domain in EXTENSION_DOMAIN_MAP.items():
            assert ext.startswith(".")
            assert isinstance(domain, str)

    def test_fallback_to_watch_root(self, watch_svc: WatchService) -> None:
        """Fallback uses watch root directory name."""
        result = watch_svc.classify_domain(
            Path("/my-project/random/file.txt"),
            watch_root=Path("/my-project"),
        )
        assert result.domain == "my-project"
        assert result.source == DomainSource.FALLBACK

    def test_fallback_to_parent_dir(self, watch_svc: WatchService) -> None:
        """Without watch root, fallback uses parent directory name."""
        result = watch_svc.classify_domain(Path("/some-project/custom/notes.txt"))
        assert result.source == DomainSource.FALLBACK

    def test_priority_order(self, memory_svc: AsyncMock) -> None:
        """Explicit > rule > directory > extension > fallback."""
        # docs/ directory should match directory mapping, not extension
        svc = WatchService(memory_svc)
        result = svc.classify_domain(Path("/project/docs/spec.sql"))
        assert result.domain == "documentation"  # directory wins over extension
        assert result.source == DomainSource.DIRECTORY_NAME

    def test_rules_sorted_by_priority(self, memory_svc: AsyncMock) -> None:
        """Higher priority rules are checked first."""
        rules = [
            WatchRule(pattern="**/*.md", domain="general-docs", priority=1),
            WatchRule(pattern="api/**", domain="api-docs", priority=10),
        ]
        svc = WatchService(memory_svc, rules=rules)
        result = svc.classify_domain(
            Path("/project/api/readme.md"),
            watch_root=Path("/project"),
        )
        # api/** rule has higher priority
        assert result.domain == "api-docs"


class TestFileHashTracking:
    """Tests for file hash deduplication."""

    def test_compute_file_hash(self, watch_svc: WatchService) -> None:
        """SHA-256 hash is computed correctly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("test content")
            f.flush()
            hash1 = watch_svc.compute_file_hash(Path(f.name))
            assert len(hash1) == 64  # SHA-256 hex digest
            # Same content should produce same hash
            hash2 = watch_svc.compute_file_hash(Path(f.name))
            assert hash1 == hash2

    def test_should_ingest_new_file(self, watch_svc: WatchService) -> None:
        """New files should be ingested."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("new content")
            f.flush()
            assert watch_svc.should_ingest(Path(f.name)) is True

    def test_should_not_ingest_unchanged(self, watch_svc: WatchService) -> None:
        """Unchanged files (same hash) should be skipped."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("same content")
            f.flush()
            path = Path(f.name)
            assert watch_svc.should_ingest(path) is True
            assert watch_svc.should_ingest(path) is False  # Second call: unchanged

    def test_should_ingest_modified_file(self, watch_svc: WatchService) -> None:
        """Modified files (different hash) should be ingested."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("original content")
            f.flush()
            path = Path(f.name)
            assert watch_svc.should_ingest(path) is True

            # Modify the file
            with open(path, "w") as f2:
                f2.write("modified content")
            assert watch_svc.should_ingest(path) is True

    def test_should_ingest_nonexistent_file(self, watch_svc: WatchService) -> None:
        """Nonexistent files return False."""
        assert watch_svc.should_ingest(Path("/nonexistent/file.md")) is False


class TestFileEventHandling:
    """Tests for handle_file_event."""

    async def test_skip_deleted_events(self, watch_svc: WatchService) -> None:
        """Deleted files are skipped."""
        event = FileChangeEvent(
            path="/some/file.md",
            change_type=FileChangeType.DELETED,
            timestamp=datetime.now(UTC),
        )
        result = await watch_svc.handle_file_event(event)
        assert result is False

    async def test_skip_unsupported_extension(self, watch_svc: WatchService) -> None:
        """Files with unsupported extensions are skipped."""
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"unsupported")
            f.flush()
            event = FileChangeEvent(
                path=f.name,
                change_type=FileChangeType.CREATED,
                timestamp=datetime.now(UTC),
            )
            result = await watch_svc.handle_file_event(event)
            assert result is False
            assert watch_svc.stats.files_skipped_unsupported == 1

    async def test_ingest_new_markdown(self, watch_svc: WatchService) -> None:
        """New markdown files are ingested."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
            dir="/tmp",
        ) as f:
            f.write("# Test\n\nSome knowledge content.")
            f.flush()
            event = FileChangeEvent(
                path=f.name,
                change_type=FileChangeType.CREATED,
                timestamp=datetime.now(UTC),
            )
            result = await watch_svc.handle_file_event(event)
            assert result is True
            assert watch_svc.stats.files_ingested == 1

    async def test_skip_unchanged_file(self, watch_svc: WatchService) -> None:
        """Unchanged files (same hash) are skipped on second event."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
            dir="/tmp",
        ) as f:
            f.write("# Stable\n\nContent that doesn't change.")
            f.flush()
            event = FileChangeEvent(
                path=f.name,
                change_type=FileChangeType.CREATED,
                timestamp=datetime.now(UTC),
            )
            await watch_svc.handle_file_event(event)
            # Second event: same file, same content
            result = await watch_svc.handle_file_event(event)
            assert result is False
            assert watch_svc.stats.files_skipped_hash == 1


class TestSupportedExtensions:
    """Tests for is_supported."""

    def test_markdown_supported(self, watch_svc: WatchService) -> None:
        assert watch_svc.is_supported(Path("readme.md")) is True

    def test_json_supported(self, watch_svc: WatchService) -> None:
        assert watch_svc.is_supported(Path("data.json")) is True

    def test_csv_supported(self, watch_svc: WatchService) -> None:
        assert watch_svc.is_supported(Path("data.csv")) is True

    def test_unsupported(self, watch_svc: WatchService) -> None:
        assert watch_svc.is_supported(Path("image.png")) is False
        assert watch_svc.is_supported(Path("binary.exe")) is False


class TestHashPersistence:
    """Tests for hash load/save."""

    async def test_load_hashes_empty(self, watch_svc: WatchService) -> None:
        """Loading with no persisted hashes succeeds."""
        await watch_svc.load_hashes()
        assert watch_svc._file_hashes == {}

    async def test_load_hashes_from_store(
        self,
        memory_svc: AsyncMock,
    ) -> None:
        """Persisted hashes are loaded correctly."""
        import json

        hashes = {"/path/to/file.md": "abc123"}
        memory_svc.store.get_consolidation_value = AsyncMock(
            return_value=json.dumps(hashes),
        )
        svc = WatchService(memory_svc)
        await svc.load_hashes()
        assert svc._file_hashes == hashes

    async def test_save_hashes(self, watch_svc: WatchService) -> None:
        """Hashes are persisted to store."""
        watch_svc._file_hashes = {"/path/to/file.md": "abc123"}
        await watch_svc.save_hashes()
        watch_svc._memory_svc.store.set_consolidation_value.assert_called_once()


class TestWatchStats:
    """Tests for WatchStats serialization."""

    def test_to_dict(self) -> None:
        from ncms.domain.watch import WatchStats

        stats = WatchStats(
            files_ingested=5,
            files_skipped_hash=3,
            domains_detected={"api", "docs"},
        )
        d = stats.to_dict()
        assert d["files_ingested"] == 5
        assert d["files_skipped_hash"] == 3
        assert d["domains_detected"] == ["api", "docs"]  # sorted
