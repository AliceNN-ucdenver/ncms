"""Entity extraction label definitions — zero infrastructure dependencies.

Provides universal label constants and the label resolution interface
used by the application layer.  The actual NER extraction happens in
infrastructure/extraction/gliner_extractor.py (GLiNER).
"""

from __future__ import annotations

# Universal labels that work across all domains.
# Used as fallback when no domain-specific labels are cached.
UNIVERSAL_LABELS: list[str] = [
    "person",
    "organization",
    "location",
    "technology",
    "concept",
    "event",
    "product",
    "process",
    "document",
    "metric",
]

# Max entities per extraction (cap for GLiNER output)
MAX_ENTITIES = 20


def resolve_labels(
    domains: list[str],
    cached_labels: dict[str, list[str]] | None = None,
) -> list[str]:
    """Resolve entity labels for extraction based on domain context.

    Label resolution chain:
    1. If any domain has cached labels, merge them (union, deduplicated)
    2. If no cached labels found, fall back to UNIVERSAL_LABELS

    Args:
        domains: Memory domains to resolve labels for.
        cached_labels: Dict mapping domain -> label list, loaded from
                       consolidation_state by the application layer.

    Returns:
        Deduplicated list of entity labels for GLiNER extraction.
    """
    if not cached_labels or not domains:
        return list(UNIVERSAL_LABELS)

    merged: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        domain_labels = cached_labels.get(domain)
        if domain_labels:
            for label in domain_labels:
                low = label.lower()
                if low not in seen:
                    seen.add(low)
                    merged.append(label)

    return merged if merged else list(UNIVERSAL_LABELS)
