"""BM25 exemplar-based intent classifier.

Indexes ~70-100 example queries (from ``INTENT_EXEMPLARS``) into a small
in-memory Tantivy index.  At query time the user's query is searched against
these exemplars; BM25 scores are aggregated per intent class and the
highest-scoring intent wins.

BM25 with English stemming naturally generalises across paraphrases
(e.g. "current" ≈ "currently", "show" ≈ "showing") without explicit keyword
lists.  Adding new coverage only requires appending exemplar strings — no
weight tuning needed.
"""

from __future__ import annotations

import logging
import tempfile
from collections import defaultdict

import tantivy

from ncms.domain.intent import (
    INTENT_EXEMPLARS,
    INTENT_TARGETS,
    IntentResult,
    QueryIntent,
)

logger = logging.getLogger(__name__)


def _sanitize_query(query: str) -> str:
    """Escape special characters for Tantivy's query parser.

    Mirrors TantivyEngine._sanitize_query — single quotes and backticks are
    replaced with spaces, all other Tantivy syntax chars are backslash-escaped.
    """
    query = query.replace("'", " ").replace("`", " ")
    escape_chars = set('+^:{}"[]()~!\\*-/')
    escaped = []
    for ch in query:
        if ch in escape_chars:
            escaped.append(f"\\{ch}")
        else:
            escaped.append(ch)
    return "".join(escaped).strip()


class ExemplarIntentIndex:
    """BM25 exemplar index for intent classification.

    On construction the index is populated from ``INTENT_EXEMPLARS``.
    Each exemplar is a separate document with an ``intent`` label (stored)
    and ``content`` field (en_stem tokenized, not stored).

    Classification:
        1. Search exemplar index for the top-K hits.
        2. Group hits by intent, sum BM25 scores per intent.
        3. Highest-sum intent wins.
        4. Confidence = top / (top + runner-up).
        5. If no hits → fact_lookup with confidence 1.0.
    """

    def __init__(self, top_k: int = 5) -> None:
        self._top_k = top_k

        # Build in-memory Tantivy index
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("intent", stored=True, tokenizer_name="raw")
        builder.add_text_field("content", stored=False, tokenizer_name="en_stem")
        schema = builder.build()

        tmp_dir = tempfile.mkdtemp(prefix="ncms_intent_exemplar_")
        self._index = tantivy.Index(schema, path=tmp_dir)

        # Populate from exemplar data
        writer = self._index.writer()
        count = 0
        for intent, exemplars in INTENT_EXEMPLARS.items():
            for exemplar in exemplars:
                writer.add_document(
                    tantivy.Document(intent=intent.value, content=exemplar)
                )
                count += 1
        writer.commit()

        logger.info("Intent exemplar index built: %d documents across %d intents",
                     count, len(INTENT_EXEMPLARS))

    def classify(self, query: str) -> IntentResult:
        """Classify a query into one of 7 intent classes via BM25 exemplar matching.

        Args:
            query: Natural language search query.

        Returns:
            IntentResult with classified intent, confidence, and target node types.
        """
        safe_query = _sanitize_query(query)
        if not safe_query.strip():
            return IntentResult(
                intent=QueryIntent.FACT_LOOKUP,
                confidence=1.0,
                target_node_types=INTENT_TARGETS[QueryIntent.FACT_LOOKUP],
            )

        self._index.reload()
        searcher = self._index.searcher()

        try:
            parsed = self._index.parse_query(safe_query, ["content"])
        except Exception:
            logger.debug("Tantivy parse error for intent query: %r", query)
            return IntentResult(
                intent=QueryIntent.FACT_LOOKUP,
                confidence=1.0,
                target_node_types=INTENT_TARGETS[QueryIntent.FACT_LOOKUP],
            )

        results = searcher.search(parsed, limit=self._top_k).hits
        if not results:
            return IntentResult(
                intent=QueryIntent.FACT_LOOKUP,
                confidence=1.0,
                target_node_types=INTENT_TARGETS[QueryIntent.FACT_LOOKUP],
            )

        # Aggregate BM25 scores by intent
        scores: dict[str, float] = defaultdict(float)
        for score, doc_address in results:
            doc = searcher.doc(doc_address)
            intent_str = str(doc.get_first("intent"))
            scores[intent_str] += float(score)

        # Sort by score descending
        sorted_intents = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_intent_str, top_score = sorted_intents[0]

        # Confidence: ratio of top vs runner-up
        if len(sorted_intents) > 1:
            runner_up_score = sorted_intents[1][1]
            confidence = top_score / (top_score + runner_up_score)
        else:
            confidence = 1.0

        try:
            intent = QueryIntent(top_intent_str)
        except ValueError:
            logger.warning("Unknown intent from exemplar index: %s", top_intent_str)
            intent = QueryIntent.FACT_LOOKUP

        return IntentResult(
            intent=intent,
            confidence=confidence,
            target_node_types=INTENT_TARGETS[intent],
        )
