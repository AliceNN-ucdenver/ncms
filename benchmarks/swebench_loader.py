"""SWE-bench dataset loader for NCMS memory benchmark.

Downloads the SWE-bench dataset from HuggingFace, filters to Django
issues, and constructs corpus/query/qrel structures compatible with
the NCMS benchmark harness.

Each Django issue becomes a memory document with:
- Content: problem_statement + hints_text
- Metadata: instance_id, repo, version, created_at, files_touched, subsystem
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Django subsystem classification ──────────────────────────────────────

SUBSYSTEM_PREFIXES: list[tuple[str, str]] = [
    ("django/db/", "orm"),
    ("django/contrib/auth/", "auth"),
    ("django/contrib/admin/", "admin"),
    ("django/forms/", "forms"),
    ("django/template/", "templates"),
    ("django/urls/", "urls"),
    ("django/http/", "http"),
    ("django/views/", "views"),
    ("django/middleware/", "middleware"),
    ("django/core/management/", "management"),
    ("django/core/mail/", "mail"),
    ("django/core/cache/", "cache"),
    ("django/core/serializers/", "serializers"),
    ("django/core/validators/", "validators"),
    ("django/contrib/contenttypes/", "contenttypes"),
    ("django/contrib/sessions/", "sessions"),
    ("django/contrib/staticfiles/", "staticfiles"),
    ("django/utils/", "utils"),
    ("django/dispatch/", "signals"),
    ("django/test/", "testing"),
    ("tests/", "tests"),
]


@dataclass
class SWEInstance:
    """A single SWE-bench instance with parsed metadata."""

    instance_id: str
    repo: str
    version: str
    created_at: str  # ISO8601
    problem_statement: str
    hints_text: str
    patch: str
    test_patch: str
    files_touched: list[str] = field(default_factory=list)
    subsystem: str = "other"

    @property
    def content(self) -> str:
        """Full content for memory storage."""
        parts = [self.problem_statement]
        if self.hints_text:
            parts.append(f"\n\n---\nAdditional context:\n{self.hints_text}")
        return "\n".join(parts)

    @property
    def created_at_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))


def parse_files_from_patch(patch: str) -> list[str]:
    """Extract file paths from a unified diff patch.

    Looks for --- a/path and +++ b/path headers in the diff.
    Returns deduplicated list of file paths (without a/ or b/ prefix).
    """
    files: set[str] = set()
    for match in re.finditer(r"^(?:---|\+\+\+) [ab]/(.+)$", patch, re.MULTILINE):
        path = match.group(1)
        if path != "/dev/null":
            files.add(path)
    return sorted(files)


def classify_subsystem(files: list[str]) -> str:
    """Classify a set of files into a Django subsystem.

    Uses majority vote: whichever subsystem has the most files wins.
    Falls back to 'other' if no subsystem matches.
    """
    if not files:
        return "other"

    counts: dict[str, int] = {}
    for f in files:
        for prefix, subsystem in SUBSYSTEM_PREFIXES:
            if f.startswith(prefix):
                counts[subsystem] = counts.get(subsystem, 0) + 1
                break

    if not counts:
        return "other"

    return max(counts, key=counts.get)  # type: ignore[arg-type]


def load_swebench_django(
    min_issues: int = 10,
) -> list[SWEInstance]:
    """Load SWE-bench dataset, filter to Django, parse metadata.

    Args:
        min_issues: Minimum issues required (sanity check).

    Returns:
        List of SWEInstance sorted by created_at (chronological).

    Raises:
        RuntimeError: If fewer than min_issues Django instances found.
    """
    try:
        from datasets import load_dataset
    except ImportError as e:
        msg = "HuggingFace datasets required: uv sync --group bench"
        raise RuntimeError(msg) from e

    logger.info("Loading SWE-bench dataset from HuggingFace...")
    ds = load_dataset("princeton-nlp/SWE-bench", split="test")

    instances: list[SWEInstance] = []
    for row in ds:
        if row["repo"] != "django/django":
            continue

        files = parse_files_from_patch(row["patch"])
        subsystem = classify_subsystem(files)

        instances.append(
            SWEInstance(
                instance_id=row["instance_id"],
                repo=row["repo"],
                version=row["version"],
                created_at=row["created_at"],
                problem_statement=row["problem_statement"],
                hints_text=row.get("hints_text", "") or "",
                patch=row["patch"],
                test_patch=row.get("test_patch", "") or "",
                files_touched=files,
                subsystem=subsystem,
            )
        )

    if len(instances) < min_issues:
        msg = f"Only {len(instances)} Django issues found (need {min_issues})"
        raise RuntimeError(msg)

    # Sort chronologically
    instances.sort(key=lambda x: x.created_at)

    logger.info(
        "Loaded %d Django issues spanning %s to %s",
        len(instances),
        instances[0].created_at[:10],
        instances[-1].created_at[:10],
    )

    return instances


def build_corpus(
    instances: list[SWEInstance],
) -> dict[str, dict[str, str]]:
    """Convert instances to BEIR-style corpus dict.

    Returns:
        {instance_id: {"title": instance_id, "text": problem_statement + hints}}
    """
    corpus: dict[str, dict[str, str]] = {}
    for inst in instances:
        corpus[inst.instance_id] = {
            "title": inst.instance_id,
            "text": inst.content,
        }
    return corpus


def split_train_test(
    instances: list[SWEInstance],
    test_fraction: float = 0.2,
) -> tuple[list[SWEInstance], list[SWEInstance]]:
    """Split instances into train/test by chronological order.

    The most recent test_fraction of issues become the test set (queries).
    Earlier issues form the training set (corpus).

    This ensures no temporal data leakage — queries always come after
    corpus documents chronologically.
    """
    # Instances should already be sorted by created_at
    split_idx = int(len(instances) * (1 - test_fraction))
    train = instances[:split_idx]
    test = instances[split_idx:]
    logger.info(
        "Split: %d train (corpus), %d test (queries) — cutoff: %s",
        len(train),
        len(test),
        train[-1].created_at[:10] if train else "N/A",
    )
    return train, test
