"""Query intent classification for intent-aware retrieval.

Provides the QueryIntent taxonomy, IntentResult value type, exemplar queries
for BM25-based classification, and a keyword-based fallback classifier.

The primary classification path uses a BM25 exemplar index (see
infrastructure/indexing/exemplar_intent_index.py).  The keyword-based
``classify_intent()`` function remains as a zero-dependency fallback when no
index is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QueryIntent(StrEnum):
    """Classified query intent — determines which node types to boost."""

    FACT_LOOKUP = "fact_lookup"
    CURRENT_STATE_LOOKUP = "current_state_lookup"
    HISTORICAL_LOOKUP = "historical_lookup"
    EVENT_RECONSTRUCTION = "event_reconstruction"
    CHANGE_DETECTION = "change_detection"
    PATTERN_LOOKUP = "pattern_lookup"
    STRATEGIC_REFLECTION = "strategic_reflection"


@dataclass(frozen=True, slots=True)
class IntentResult:
    """Classification result with intent, confidence, and target node types."""

    intent: QueryIntent
    confidence: float  # 0.0 - 1.0
    target_node_types: tuple[str, ...]  # NodeType values this intent targets


# Intent → preferred node types (strings matching NodeType enum values).
INTENT_TARGETS: dict[QueryIntent, tuple[str, ...]] = {
    QueryIntent.FACT_LOOKUP: ("atomic", "entity_state"),
    QueryIntent.CURRENT_STATE_LOOKUP: ("entity_state",),
    QueryIntent.HISTORICAL_LOOKUP: ("entity_state", "episode"),
    QueryIntent.EVENT_RECONSTRUCTION: ("episode", "atomic"),
    QueryIntent.CHANGE_DETECTION: ("entity_state",),
    QueryIntent.PATTERN_LOOKUP: ("abstract",),
    QueryIntent.STRATEGIC_REFLECTION: ("abstract",),
}

# ---------------------------------------------------------------------------
# Exemplar queries for BM25-based intent classification.
#
# Each intent class has 10-15 representative queries.  These are indexed in a
# small in-memory Tantivy index at startup.  At query time, the user's query
# is matched against these exemplars via BM25; the intent whose exemplars
# score highest wins.
#
# BM25 with English stemming naturally handles paraphrases (e.g. "current" ≈
# "currently", "show" ≈ "showing") without explicit keyword lists.
# ---------------------------------------------------------------------------
INTENT_EXEMPLARS: dict[QueryIntent, list[str]] = {
    QueryIntent.CURRENT_STATE_LOOKUP: [
        "What is the current status of the API?",
        "What's the database schema right now?",
        "Show me the latest deployment config",
        "What are the current settings?",
        "What version is running in production?",
        "What's the state of the auth service?",
        "Show me the most recent configuration",
        "What does the API currently return?",
        "Is the feature flag enabled right now?",
        "What are we using for caching at the moment?",
        "Current status of the deployment pipeline",
        "What is the active database connection pool size?",
    ],
    QueryIntent.HISTORICAL_LOOKUP: [
        "What was the database schema in January?",
        "How was the API configured last quarter?",
        "What were the settings before the migration?",
        "Show me the historical deployment frequency",
        "What did the auth flow look like previously?",
        "How was this endpoint configured back in March?",
        "What was running in production last week?",
        "What were the cache settings before the upgrade?",
        "Show me what the schema used to look like",
        "What was the API version at that time?",
        "Previously how was the rate limiting configured?",
        "Back when we had the old auth, what tokens did it use?",
    ],
    QueryIntent.EVENT_RECONSTRUCTION: [
        "What happened during the deployment outage?",
        "Walk me through the incident on Friday",
        "Reconstruct the sequence of events for the data loss",
        "What occurred during the migration window?",
        "Timeline of the authentication failure",
        "How did the rollback proceed?",
        "Recount what happened when the database went down",
        "Walk through the chain of events for the release",
        "What happened step by step during the outage?",
        "Describe the incident response timeline",
        "How did the service degradation unfold?",
        "What was the sequence of failures in the pipeline?",
    ],
    QueryIntent.CHANGE_DETECTION: [
        "What changed in the API since last release?",
        "What has changed in the schema compared to v3?",
        "Show me the differences between old and new config",
        "How has the architecture evolved over time?",
        "What's different about the new deployment pipeline?",
        "Compare the before and after of the refactor",
        "What shifted in our authentication approach?",
        "Show me the progression of the database schema",
        "What transitioned in the API design?",
        "How have the endpoint contracts changed?",
        "What is different between the v1 and v2 API?",
        "Show the evolution of our error handling strategy",
    ],
    QueryIntent.PATTERN_LOOKUP: [
        "Is there a pattern in the deployment failures?",
        "What do these errors have in common?",
        "Are there recurring issues with the auth service?",
        "What's the typical failure mode for this endpoint?",
        "Do we tend to see this kind of bug after releases?",
        "Is there a common theme across these incidents?",
        "What usually causes these timeout errors?",
        "Are there frequently occurring problems in staging?",
        "What patterns emerge from the incident reports?",
        "Do we typically see spikes after deployments?",
        "Is there a recurring theme in the performance issues?",
    ],
    QueryIntent.STRATEGIC_REFLECTION: [
        "What lessons have we learned from past incidents?",
        "Any insights from the last quarter's deployments?",
        "What are the key takeaways from the migration project?",
        "What are the best practices for database migrations?",
        "What recommendations came out of the retrospective?",
        "What should we do differently next time?",
        "What can we learn from the auth service redesign?",
        "Key insights about our deployment process",
        "What would you recommend for improving reliability?",
        "Lessons from the API versioning experience",
        "What are the main takeaways from the outage review?",
        "Reflections on our scaling strategy",
    ],
}

# ---------------------------------------------------------------------------
# Keyword fallback classifier
#
# Used when no BM25 exemplar index is available.  Scores each intent by
# summing weights of matched keyword groups.
# ---------------------------------------------------------------------------
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]

_INTENT_PATTERNS: dict[QueryIntent, list[tuple[list[str], float]]] = {
    QueryIntent.CURRENT_STATE_LOOKUP: [
        (["what is", "what's", "what are"], 0.3),
        (["current", "currently"], 0.4),
        (["status", "state of"], 0.3),
        (["right now", "at the moment", "presently"], 0.5),
        (["latest", "newest", "most recent"], 0.3),
    ],
    QueryIntent.HISTORICAL_LOOKUP: [
        (["what was", "what were"], 0.5),
        (["historically", "in the past"], 0.4),
        (["back in", "back when"], 0.4),
        (["last month", "last week", "last year", "last quarter"], 0.4),
        ([f"in {m}" for m in _MONTHS], 0.4),
        (["before", "previously", "formerly"], 0.3),
        (["at that time", "at the time"], 0.4),
        (["used to"], 0.4),
    ],
    QueryIntent.EVENT_RECONSTRUCTION: [
        (["what happened", "what occurred"], 0.5),
        (["incident", "outage", "event"], 0.4),
        (["during", "timeline of"], 0.4),
        (["sequence of events", "chain of events"], 0.5),
        (["how did", "how was"], 0.3),
        (["walk me through", "walk through"], 0.4),
        (["reconstruct", "recount"], 0.4),
    ],
    QueryIntent.CHANGE_DETECTION: [
        (["what changed", "what has changed", "what's changed"], 0.7),
        (["different", "difference between", "differences"], 0.3),
        (["compared to", "versus", " vs "], 0.4),
        (["evolution", "evolved", "progression"], 0.4),
        (["transition", "transitioned", "shifted"], 0.3),
        (["before and after"], 0.5),
    ],
    QueryIntent.PATTERN_LOOKUP: [
        (["pattern", "patterns"], 0.5),
        (["tend to", "tendency"], 0.4),
        (["usually", "typically", "often"], 0.4),
        (["recurring", "recurrence", "repeating"], 0.5),
        (["common", "frequent", "common theme"], 0.3),
    ],
    QueryIntent.STRATEGIC_REFLECTION: [
        (["learned", "lesson", "lessons learned"], 0.5),
        (["insight", "insights"], 0.5),
        (["takeaway", "takeaways", "key takeaway"], 0.5),
        (["best practice", "best practices"], 0.4),
        (["recommendation", "recommendations"], 0.4),
        (["what should we", "what can we"], 0.3),
        (["retrospective", "reflection"], 0.4),
    ],
}


def classify_intent(query: str) -> IntentResult:
    """Keyword-based fallback intent classifier.

    Used when no BM25 exemplar index is available.  Scores each intent by
    summing weights of matched keyword groups.  Returns the highest-scoring
    intent with confidence = min(1.0, score).  Falls back to fact_lookup when
    no patterns match.

    Args:
        query: Natural language search query.

    Returns:
        IntentResult with classified intent, confidence, and target node types.
    """
    query_lower = query.lower()

    best_intent = QueryIntent.FACT_LOOKUP
    best_score = 0.0

    for intent, patterns in _INTENT_PATTERNS.items():
        score = 0.0
        for keywords, weight in patterns:
            for kw in keywords:
                if kw in query_lower:
                    score += weight
                    break  # Only count each pattern group once
        if score > best_score:
            best_score = score
            best_intent = intent

    # No patterns matched → confident fact_lookup (the default intent)
    if best_score == 0.0:
        return IntentResult(
            intent=QueryIntent.FACT_LOOKUP,
            confidence=1.0,
            target_node_types=INTENT_TARGETS[QueryIntent.FACT_LOOKUP],
        )

    confidence = min(1.0, best_score)

    return IntentResult(
        intent=best_intent,
        confidence=confidence,
        target_node_types=INTENT_TARGETS[best_intent],
    )
