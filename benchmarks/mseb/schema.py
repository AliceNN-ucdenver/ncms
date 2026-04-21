"""MSEB corpus + queries JSONL schema.

Shared across every ``benchmarks/mseb_<domain>/`` instantiation.
A domain contributes two JSONL files (corpus.jsonl + queries.jsonl);
the harness in ``benchmarks/mseb/harness.py`` consumes both and
produces the per-shape rank-1 / top-5 metrics reported in the
benchmark write-up.

See ``benchmarks/mseb/README.md`` §2 ("Schema") for the rationale
behind each field.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Message / memory kind taxonomy — common across all MSEB domains
# ---------------------------------------------------------------------------

MemoryKind = Literal[
    "declaration",     # "foo: field = value" / initial state observation
    "retirement",      # "deprecated X in favour of Y" / ruled-out diagnosis
    "causal_link",     # "caused by" / "regression from" / "due to"
    "ordinal_anchor",  # first presentation, first report, final outcome
    "none",            # no state-change signal (neutral / informational)
]

MESSAGE_KINDS: tuple[MemoryKind, ...] = (
    "declaration", "retirement", "causal_link", "ordinal_anchor", "none",
)


# ---------------------------------------------------------------------------
# Preference sub-type taxonomy — matches P2 intent_head output classes.
# Only conversational / persona domains carry non-"none" preference labels;
# SWE and Clinical gold queries default to ``"none"``.
# ---------------------------------------------------------------------------

PreferenceKind = Literal[
    "positive",    # "I love X" / "I use X" / "I prefer X"
    "avoidance",   # "I avoid Y" / "I can't eat Y" / "Y doesn't work for me"
    "habitual",    # "Every morning I..." / "I usually..." / recurring behaviour
    "difficult",   # "I struggle with Z" / "Z is hard for me"
    "none",        # non-preference query
]

PREFERENCE_KINDS: tuple[PreferenceKind, ...] = (
    "positive", "avoidance", "habitual", "difficult", "none",
)


# ---------------------------------------------------------------------------
# Query class — cross-cuts intent shapes; identifies which NCMS mechanism
# a query is expected to exercise.  Enables the "backend × class" table:
#
# | dataset × class  | general | temporal | preference | noise |
# | ncms-tlg-on      |  .73   |   .93   |    .18    |  0.00 |
# | mem0             |  .21   |   .20   |    .12    |  0.00 |
#
# Populated at gold-authoring time by a deterministic classifier
# (see ``benchmarks/mseb/query_class.py``).  Defaults to "general" for
# back-compat with gold files that predate this field.
# ---------------------------------------------------------------------------

QueryClass = Literal[
    "general",     # standard retrieval — BM25 + SPLADE + graph lift
    "temporal",    # ordinal / recency / range — NCMS temporal parser fires
    "preference",  # positive / avoidance / habitual / difficult — P2 intent_head
    "noise",       # adversarial / off-topic — all systems should reject
]

QUERY_CLASSES: tuple[QueryClass, ...] = (
    "general", "temporal", "preference", "noise",
)


# ---------------------------------------------------------------------------
# Intent shapes — fixed across all MSEB domains (matches TLG's 11)
# ---------------------------------------------------------------------------

IntentShape = Literal[
    "current_state",
    "origin",
    "ordinal_first",
    "ordinal_last",
    "sequence",
    "predecessor",
    "interval",
    "range",
    "transitive_cause",
    "causal_chain",
    "concurrent",
    "before_named",
    "retirement",
    "noise",
]

INTENT_SHAPES: tuple[IntentShape, ...] = (
    "current_state", "origin",
    "ordinal_first", "ordinal_last",
    "sequence", "predecessor",
    "interval", "range",
    "transitive_cause", "causal_chain",
    "concurrent", "before_named",
    "retirement", "noise",
)


# ---------------------------------------------------------------------------
# Corpus memory — one per JSONL line
# ---------------------------------------------------------------------------


@dataclass
class CorpusMemory:
    """One state-evolution memory in the corpus.

    Matches NCMS ingest schema closely so mined corpora feed the
    retrieval pipeline directly via ``store_memory`` — no
    transformation layer.  Required fields: ``mid``, ``subject``,
    ``content``, ``observed_at``.
    """

    mid: str
    """Stable memory ID.  Convention: ``<domain>-<subject>-m<NN>``
    (e.g. ``swe-django-1234-m07`` or ``clin-pmc8123-m02``)."""

    subject: str
    """Subject-chain identifier.  All memories with the same subject
    belong to one state-evolution trajectory.  Convention: a slugified
    issue / case / document ID."""

    content: str
    """Natural-language message body.  Should read as written in the
    source (GitHub comment, case-report section, etc.) — mining does
    not paraphrase or rewrite."""

    observed_at: str
    """ISO-8601 timestamp of the original event.  For GitHub issues
    this is the comment's ``created_at``; for case reports it's
    either an explicit date in the narrative or a synthetic monotonic
    timestamp seeded on the PMC publication date."""

    entities: list[str] = field(default_factory=list)
    """Optional entity list.  GLiNER-style; filled at mining time
    when source has structured entity tags, otherwise left empty and
    the ingest path fills from live extraction."""

    metadata: dict = field(default_factory=dict)
    """Free-form per-memory metadata.  Conventionally includes:
    ``kind``                 — :data:`MEMORY_KINDS` label
    ``supersedes``           — list of ``mid``s this memory supersedes
    ``retires_entities``     — list of entity names this memory retires
    ``source_msg_id``        — provenance: original comment / section ID
    ``source_url``           — permalink back to the source
    """

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Gold query — one per JSONL line
# ---------------------------------------------------------------------------


@dataclass
class GoldQuery:
    """One hand-labeled gold query.

    ``gold_mid`` is the primary correct answer; ``gold_alt`` is an
    optional list of alternative correct answers (for queries where
    multiple memories in the same chain could reasonably be top-1).
    Retrieval grading uses ``gold_mid ∪ gold_alt`` as the accept set.
    """

    qid: str
    """Stable query ID.  Convention: ``<domain>-<shape>-<NNN>``."""

    shape: IntentShape
    """Which TLG intent shape this query exercises."""

    text: str
    """The query as a user would ask it.  Natural language, no
    templating — phrasing matters for retrieval."""

    subject: str
    """Subject chain the answer lives in.  Matches ``CorpusMemory.subject``."""

    entity: str | None = None
    """Optional entity anchor — e.g. ``"MFA"`` for a predecessor query
    asking "what came before MFA?".  ``None`` for subject-level
    queries (``current_state``, ``ordinal_first``)."""

    gold_mid: str = ""
    """Primary acceptable answer (memory ID in the corpus)."""

    gold_alt: list[str] = field(default_factory=list)
    """Alternative acceptable answers.  Empty for unambiguous queries."""

    expected_proof_pattern: str | None = None
    """Optional regex pattern the TLG proof string should match.
    When set, the harness also asserts the proof contains this
    substring — helpful for debugging grammar regressions."""

    note: str = ""
    """Free-form note from the annotator.  Useful for adversarial
    / edge-case queries to explain what failure mode they probe."""

    preference: PreferenceKind = "none"
    """Preference sub-type anchor for this query.  ``"none"`` on
    non-preference queries (SWE, Clinical).  On MSEB-Convo this
    carries the gold ``intent_head`` preference class so the harness
    reports per-preference rank-1 / top-5 in addition to per-shape."""

    query_class: QueryClass = "general"
    """Cross-cutting class identifying which NCMS mechanism this
    query exercises.  ``general`` (BM25+SPLADE lift), ``temporal``
    (parser fires — ordinal / recency / range), ``preference``
    (P2 intent_head), or ``noise`` (adversarial).  Defaults to
    ``general`` for back-compat with pre-classifier gold files."""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def dump_corpus(memories: list[CorpusMemory], path: Path) -> None:
    """Write corpus memories to JSONL.  Deterministic order preserved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for mem in memories:
            fh.write(mem.to_json())
            fh.write("\n")


def dump_queries(queries: list[GoldQuery], path: Path) -> None:
    """Write gold queries to JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for q in queries:
            fh.write(q.to_json())
            fh.write("\n")


def load_corpus(path: Path) -> list[CorpusMemory]:
    """Read corpus JSONL into typed memories."""
    out: list[CorpusMemory] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.append(CorpusMemory(**row))
    return out


def load_queries(path: Path) -> list[GoldQuery]:
    """Read gold-query JSONL into typed queries."""
    out: list[GoldQuery] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.append(GoldQuery(**row))
    return out


__all__ = [
    "INTENT_SHAPES",
    "MESSAGE_KINDS",
    "PREFERENCE_KINDS",
    "QUERY_CLASSES",
    "CorpusMemory",
    "GoldQuery",
    "IntentShape",
    "MemoryKind",
    "PreferenceKind",
    "QueryClass",
    "dump_corpus",
    "dump_queries",
    "load_corpus",
    "load_queries",
]
