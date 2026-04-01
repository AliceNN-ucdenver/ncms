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
    keep_universal: bool | None = None,
) -> list[str]:
    """Resolve entity labels for extraction based on domain context.

    When domain-specific labels exist and keep_universal is False (or the
    cached labels include a ``_replace`` marker), domain labels REPLACE
    universal labels instead of adding to them. This keeps the total label
    count low (~10) for faster GLiNER extraction.

    When no domain-specific labels exist, UNIVERSAL_LABELS are returned
    as the fallback regardless of the keep_universal flag.

    Label resolution:
    1. Check if domain-specific labels exist in cache
    2. If yes and keep_universal is False: use domain labels only
    3. If yes and keep_universal is True: merge universal + domain (deduplicated)
    4. If no domain labels found: return UNIVERSAL_LABELS alone

    Args:
        domains: Memory domains to resolve labels for.
        cached_labels: Dict mapping domain -> label list.
        keep_universal: If True, always include universal labels (additive).
            If False, domain labels replace universal when available.
            If None (default), check for ``_keep_universal`` key in cache,
            otherwise default to False (replace mode for performance).
    """
    if not cached_labels or not domains:
        return list(UNIVERSAL_LABELS)

    # Collect all domain-specific labels
    domain_labels: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        for label in cached_labels.get(domain, []):
            low = label.lower()
            if low not in seen:
                seen.add(low)
                domain_labels.append(label)

    # No domain labels found — fall back to universal
    if not domain_labels:
        return list(UNIVERSAL_LABELS)

    # Determine whether to keep universal labels
    if keep_universal is None:
        # Check cache for explicit setting, default to False (replace mode)
        keep_universal = cached_labels.get("_keep_universal", False)

    if keep_universal:
        # Additive: universal first, then domain labels on top
        merged: list[str] = []
        merged_seen: set[str] = set()
        for label in UNIVERSAL_LABELS:
            low = label.lower()
            merged_seen.add(low)
            merged.append(label)
        for label in domain_labels:
            low = label.lower()
            if low not in merged_seen:
                merged_seen.add(low)
                merged.append(label)
        return merged

    # Replace mode: domain labels only (better performance)
    return domain_labels
