"""Shared data shapes for the intent+slot experiment.

Kept deliberately minimal so we can swap methods behind one
protocol and compare matrices across them.  Three pieces:

* :class:`ExtractedLabel` — what every method returns.
* :class:`GoldExample` — what gold + LLM-labeled + SDG-expanded
  examples all serialise to (one JSONL line per example).
* Intent taxonomy + per-domain slot taxonomies.

The intent taxonomy is FIXED across all three domains — the five
preference categories defined in the p2-plan (positive / negative
/ habitual / difficulty / choice).  Slot taxonomies are
per-domain because the entities that participate differ (a
clinical memory has ``medication``, a software memory has
``library``, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Intent taxonomy
# ---------------------------------------------------------------------------

Intent = Literal[
    "positive",    # "I love sushi"
    "negative",    # "I can't stand snow"
    "habitual",    # "I take the subway every morning"
    "difficulty",  # "this math test is hard"
    "choice",      # "I went with the vegetarian option"
    "none",        # no preference statement in the input
]

INTENT_CATEGORIES: tuple[Intent, ...] = (
    "positive", "negative", "habitual", "difficulty", "choice", "none",
)

#: Descriptive label phrases used by the E5 zero-shot method.
#: E5 requires a ``query:`` prefix for its asymmetric retrieval form;
#: including it here so the caller can encode directly.
INTENT_LABEL_DESCRIPTIONS: dict[Intent, str] = {
    "positive":
        "query: a statement expressing a like, preference, or enjoyment",
    "negative":
        "query: a statement expressing a dislike, hate, or aversion",
    "habitual":
        "query: a statement about a routine, frequency, or regular habit",
    "difficulty":
        "query: a statement evaluating how hard or challenging a task is",
    "choice":
        "query: a statement about a specific selection or decision made",
    "none":
        "query: a neutral statement without any preference",
}


# ---------------------------------------------------------------------------
# Domains + slot taxonomies
# ---------------------------------------------------------------------------

Domain = Literal["conversational", "software_dev", "clinical"]

DOMAINS: tuple[Domain, ...] = ("conversational", "software_dev", "clinical")

#: Slot tag universe per domain.  Used both by the hand-labelling
#: gold files (authors pick from this list) and by the Joint BERT
#: method's BIO label head.
SLOT_TAXONOMY: dict[Domain, tuple[str, ...]] = {
    "conversational": (
        "object",      # the thing the intent is about (food, hobby, person)
        "frequency",   # "every morning", "usually" (habitual slot)
        "alternative", # "instead of X" (choice slot)
    ),
    "software_dev": (
        "library",     # FastAPI, Pydantic
        "language",    # Python, Rust
        "pattern",     # async, threads, event-loop
        "tool",        # IDE, linter, debugger
        "alternative",
        "frequency",   # "before every commit", "on save"
    ),
    "clinical": (
        "medication",   # metformin, aspirin
        "procedure",    # arthroscopy, MRI
        "symptom",      # nausea, headache
        "severity",     # mild, severe
        "alternative",  # "X instead of Y" choices
    ),
}


# ---------------------------------------------------------------------------
# Method outputs + labelled examples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedLabel:
    """Unified output shape across all three methods.

    ``slots`` maps slot_name (from the domain's taxonomy) to the
    surface form extracted from the text.  ``slot_confidences`` is
    per-slot when the method exposes it; aggregate or missing
    confidences pass through as ``None``.
    """

    intent: Intent
    intent_confidence: float
    slots: dict[str, str] = field(default_factory=dict)
    slot_confidences: dict[str, float] = field(default_factory=dict)
    method: str = ""          # name of the method that produced it (for logs)

    def is_confident(self, threshold: float = 0.7) -> bool:
        return self.intent_confidence >= threshold


@dataclass
class GoldExample:
    """One labelled example from gold, LLM-labelled, or SDG.

    Serialises to JSONL:

        {"text": "I love sushi.",
         "domain": "conversational",
         "intent": "positive",
         "slots": {"object": "sushi"},
         "split": "gold",
         "source": "hand-labeled"}
    """

    text: str
    domain: Domain
    intent: Intent
    slots: dict[str, str] = field(default_factory=dict)
    # Which data tier this came from.
    split: Literal["gold", "llm", "sdg", "adversarial"] = "gold"
    # Free-form provenance string ("hand-labeled 2026-04-19",
    # "qwen-3.5-35b", "template-v1").
    source: str = ""
    # Optional note — typically used on adversarial rows to
    # document what failure mode they exercise.
    note: str = ""


# ---------------------------------------------------------------------------
# Evaluation matrix row
# ---------------------------------------------------------------------------


@dataclass
class MethodResult:
    """One cell of the 3 × 3 × 2 evaluation matrix."""

    method: str
    domain: Domain
    split: Literal["trained", "held_out"]
    n_examples: int
    intent_f1_macro: float
    slot_f1_macro: float
    joint_accuracy: float
    latency_p50_ms: float
    latency_p95_ms: float
    confidently_wrong_rate: float
    # Optional per-intent breakdown for deeper reporting.
    per_intent_f1: dict[Intent, float] = field(default_factory=dict)
