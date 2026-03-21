"""Domain models for filesystem watching with auto-domain classification.

Pure domain models — zero infrastructure dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class FileChangeType(StrEnum):
    """Type of filesystem change detected."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class DomainSource(StrEnum):
    """How a domain classification was determined."""

    EXPLICIT = "explicit"          # User-specified via --domain flag
    PATH_RULE = "path_rule"        # Matched a configured path pattern
    DIRECTORY_NAME = "directory_name"  # Inferred from parent directory name
    EXTENSION = "extension"        # Inferred from file extension
    FALLBACK = "fallback"          # Used watch root directory name


@dataclass(frozen=True, slots=True)
class WatchRule:
    """Maps a path glob pattern to a domain."""

    pattern: str         # Glob pattern, e.g. "docs/api/**"
    domain: str          # Target domain, e.g. "api"
    priority: int = 0    # Higher = checked first


@dataclass(frozen=True, slots=True)
class DomainClassification:
    """Result of auto-classifying a file's domain."""

    domain: str
    source: DomainSource
    confidence: float = 1.0  # 1.0 for explicit, lower for heuristic


@dataclass(frozen=True, slots=True)
class FileChangeEvent:
    """A detected filesystem change."""

    path: str
    change_type: FileChangeType
    timestamp: datetime
    size_bytes: int = 0


@dataclass
class WatchStats:
    """Cumulative statistics for a watch session."""

    files_ingested: int = 0
    files_skipped_hash: int = 0
    files_skipped_unsupported: int = 0
    files_errored: int = 0
    domains_detected: set[str] = field(default_factory=set)
    total_memories_created: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "files_ingested": self.files_ingested,
            "files_skipped_hash": self.files_skipped_hash,
            "files_skipped_unsupported": self.files_skipped_unsupported,
            "files_errored": self.files_errored,
            "domains_detected": sorted(self.domains_detected),
            "total_memories_created": self.total_memories_created,
        }
