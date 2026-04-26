"""WatchService — filesystem monitor with auto-domain classification.

Watches directories for file changes, classifies domains heuristically,
debounces rapid changes, tracks file hashes to avoid re-ingestion, and
delegates to KnowledgeLoader for actual ingestion into memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ncms.application.knowledge_loader import KnowledgeLoader
from ncms.application.memory_service import MemoryService
from ncms.domain.watch import (
    DomainClassification,
    DomainSource,
    FileChangeEvent,
    FileChangeType,
    WatchRule,
    WatchStats,
)

logger = logging.getLogger(__name__)

# Directory name → domain mapping (common conventions)
DIRECTORY_DOMAIN_MAP: dict[str, str] = {
    "docs": "documentation",
    "doc": "documentation",
    "documentation": "documentation",
    "src": "code",
    "lib": "code",
    "tests": "testing",
    "test": "testing",
    "config": "configuration",
    "configs": "configuration",
    "deploy": "deployment",
    "deployment": "deployment",
    "infra": "infrastructure",
    "infrastructure": "infrastructure",
    "api": "api",
    "scripts": "scripts",
    "ops": "operations",
    "security": "security",
    "auth": "authentication",
    "frontend": "frontend",
    "backend": "backend",
    "db": "database",
    "database": "database",
    "models": "models",
    "schemas": "schemas",
}

# File extension → domain hint mapping
EXTENSION_DOMAIN_MAP: dict[str, str] = {
    ".sql": "database",
    ".proto": "api",
    ".graphql": "api",
    ".openapi": "api",
    ".swagger": "api",
    ".yml": "configuration",
    ".yaml": "configuration",
    ".toml": "configuration",
    ".ini": "configuration",
    ".cfg": "configuration",
    ".env": "configuration",
    ".dockerfile": "deployment",
    ".tf": "infrastructure",
    ".hcl": "infrastructure",
    ".md": "documentation",
    ".rst": "documentation",
    ".adoc": "documentation",
}

# Hash persistence key in consolidation_state table
HASH_STORE_KEY = "watch_file_hashes"


class WatchService:
    """Orchestrates filesystem watching with auto-domain classification.

    Responsibilities:
    - Domain classification (heuristic, rule-based)
    - File hash tracking (dedup unchanged files)
    - Delegation to KnowledgeLoader for ingestion
    - Statistics tracking
    """

    def __init__(
        self,
        memory_svc: MemoryService,
        *,
        rules: list[WatchRule] | None = None,
        default_domain: str | None = None,
        default_importance: float = 6.0,
    ) -> None:
        self._memory_svc = memory_svc
        self._loader = KnowledgeLoader(memory_svc)
        self._rules = sorted(rules or [], key=lambda r: -r.priority)
        self._default_domain = default_domain
        self._default_importance = default_importance
        self._file_hashes: dict[str, str] = {}
        self._stats = WatchStats()
        self._event_log: Any = None  # Set externally if dashboard is active

    @property
    def stats(self) -> WatchStats:
        return self._stats

    def set_event_log(self, event_log: Any) -> None:
        """Attach an EventLog for dashboard observability."""
        self._event_log = event_log

    # ── Domain Classification ────────────────────────────────────────────

    def classify_domain(
        self,
        file_path: Path,
        watch_root: Path | None = None,
    ) -> DomainClassification:
        """Classify a file's domain using heuristic rules.

        Priority order (first match wins):
        1. Explicit default_domain (user-specified --domain)
        2. Path pattern rules (configurable WatchRule list)
        3. Directory name mapping (parent dir name → domain)
        4. Extension-based hints (file extension → domain)
        5. Fallback to watch root directory name
        """
        # 1. Explicit domain override
        if self._default_domain:
            return DomainClassification(
                domain=self._default_domain,
                source=DomainSource.EXPLICIT,
                confidence=1.0,
            )

        # 2. Path pattern rules
        import contextlib

        rel_path = str(file_path)
        if watch_root:
            with contextlib.suppress(ValueError):
                rel_path = str(file_path.relative_to(watch_root))

        for rule in self._rules:
            if fnmatch(rel_path, rule.pattern):
                return DomainClassification(
                    domain=rule.domain,
                    source=DomainSource.PATH_RULE,
                    confidence=0.9,
                )

        # 3. Directory name mapping
        parent_name = file_path.parent.name.lower()
        if parent_name in DIRECTORY_DOMAIN_MAP:
            return DomainClassification(
                domain=DIRECTORY_DOMAIN_MAP[parent_name],
                source=DomainSource.DIRECTORY_NAME,
                confidence=0.7,
            )

        # 4. Extension-based hints
        ext = file_path.suffix.lower()
        if ext in EXTENSION_DOMAIN_MAP:
            return DomainClassification(
                domain=EXTENSION_DOMAIN_MAP[ext],
                source=DomainSource.EXTENSION,
                confidence=0.5,
            )

        # 5. Fallback to watch root or parent directory
        fallback_name = (watch_root or file_path.parent).name.lower()
        return DomainClassification(
            domain=fallback_name or "general",
            source=DomainSource.FALLBACK,
            confidence=0.3,
        )

    # ── File Hash Tracking ───────────────────────────────────────────────

    def compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA-256 hash of file contents."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def should_ingest(self, file_path: Path) -> bool:
        """Check if file has changed since last ingestion."""
        path_key = str(file_path.resolve())
        try:
            current_hash = self.compute_file_hash(file_path)
        except (OSError, PermissionError):
            return False

        if path_key in self._file_hashes and self._file_hashes[path_key] == current_hash:
            return False

        self._file_hashes[path_key] = current_hash
        return True

    def is_supported(self, file_path: Path) -> bool:
        """Check if the file extension is supported by KnowledgeLoader."""
        return file_path.suffix.lower() in self._loader.SUPPORTED_EXTENSIONS

    # ── File Event Handling ──────────────────────────────────────────────

    async def handle_file_event(
        self,
        event: FileChangeEvent,
        watch_root: Path | None = None,
    ) -> bool:
        """Process a single file change event.

        Returns True if the file was ingested, False if skipped.
        """
        file_path = Path(event.path)

        # Skip deleted files
        if event.change_type == FileChangeType.DELETED:
            return False

        # Skip unsupported extensions
        if not self.is_supported(file_path):
            self._stats.files_skipped_unsupported += 1
            return False

        # Skip if file doesn't exist (race condition with rapid edits)
        if not file_path.exists():
            return False

        # Skip unchanged files (hash check)
        if not self.should_ingest(file_path):
            self._stats.files_skipped_hash += 1
            if self._event_log:
                self._event_log.watch_file_skipped(
                    str(file_path),
                    "unchanged (hash match)",
                )
            return False

        # Classify domain
        classification = self.classify_domain(file_path, watch_root)
        self._stats.domains_detected.add(classification.domain)

        if self._event_log:
            self._event_log.watch_file_detected(
                str(file_path),
                classification.domain,
                classification.source.value,
            )

        # Ingest via KnowledgeLoader
        try:
            stats = await self._loader.load_file(
                file_path,
                domains=[classification.domain],
                source_agent="file-watcher",
                importance=self._default_importance,
            )
            self._stats.files_ingested += 1
            self._stats.total_memories_created += stats.memories_created

            if self._event_log:
                self._event_log.watch_file_ingested(
                    str(file_path),
                    classification.domain,
                    stats.memories_created,
                )

            logger.info(
                "Ingested %s → domain '%s' (%d memories, source: %s)",
                file_path.name,
                classification.domain,
                stats.memories_created,
                classification.source.value,
            )
            return True

        except Exception:
            self._stats.files_errored += 1
            logger.exception("Failed to ingest %s", file_path)
            return False

    # ── Hash Persistence ─────────────────────────────────────────────────

    async def load_hashes(self) -> None:
        """Load file hashes from persistent store (crash recovery)."""
        try:
            raw = await self._memory_svc.store.get_consolidation_value(HASH_STORE_KEY)
            if raw:
                self._file_hashes = json.loads(raw)
                logger.info("Loaded %d file hashes from store", len(self._file_hashes))
        except Exception:
            logger.debug("No persisted file hashes found")

    async def save_hashes(self) -> None:
        """Persist file hashes to store."""
        try:
            await self._memory_svc.store.set_consolidation_value(
                HASH_STORE_KEY,
                json.dumps(self._file_hashes),
            )
        except Exception:
            logger.debug("Failed to persist file hashes", exc_info=True)

    # ── Initial Directory Scan ───────────────────────────────────────────

    async def scan_directory(
        self,
        directory: Path,
        recursive: bool = True,
    ) -> WatchStats:
        """Perform initial scan of a directory, ingesting new/changed files.

        Returns statistics for this scan pass.
        """
        await self.load_hashes()

        pattern = "**/*" if recursive else "*"
        for file_path in sorted(directory.glob(pattern)):
            if file_path.is_file() and self.is_supported(file_path):
                event = FileChangeEvent(
                    path=str(file_path),
                    change_type=FileChangeType.CREATED,
                    timestamp=__import__("datetime").datetime.now(
                        __import__("datetime").UTC,
                    ),
                )
                await self.handle_file_event(event, watch_root=directory)

        await self.save_hashes()
        return self._stats
