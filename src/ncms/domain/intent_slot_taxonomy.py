"""Intent-slot taxonomy constants + slot-label helpers.

Domain-layer constants for the ingest-side intent-slot classifier.
Pure Python, no infrastructure dependencies — the
``ncms.infrastructure.extraction.intent_slot`` package and the
training driver (``ncms.training.intent_slot``) both pull their
vocabularies from here.

Topic labels are **not** declared here — they live per-deployment
in the adapter's ``manifest.json`` + ``taxonomy.yaml``.  Only the
intent / admission / state_change enums are globally fixed, since
those represent universal ingest-pipeline decisions
(preference / admission / state transition) rather than
per-deployment content taxonomy.
"""

from __future__ import annotations

INTENT_CATEGORIES: tuple[str, ...] = (
    "positive", "negative", "habitual", "difficulty", "choice", "none",
)

ADMISSION_DECISIONS: tuple[str, ...] = ("persist", "ephemeral", "discard")

STATE_CHANGES: tuple[str, ...] = ("declaration", "retirement", "none")


#: Descriptive label phrases used by the E5 zero-shot method.
#: E5 requires a ``query:`` prefix for its asymmetric retrieval form.
INTENT_LABEL_DESCRIPTIONS: dict[str, str] = {
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


#: Reference per-domain slot taxonomy.  Adapter manifests override
#: this with their own slot_labels; this dict is used by the
#: experiment's SDG + as a reasonable default for the zero-shot
#: GLiNER+E5 backend.  Adding a new reference domain means adding a
#: row here AND shipping a taxonomy YAML under the adapter artifact.
SLOT_TAXONOMY: dict[str, tuple[str, ...]] = {
    "conversational": (
        "object",       # the thing the intent is about (food, hobby, person)
        "frequency",    # "every morning", "usually" (habitual slot)
        "alternative",  # "instead of X" (choice slot)
    ),
    "software_dev": (
        "library",      # FastAPI, Pydantic
        "language",     # Python, Rust
        "pattern",      # async, threads, event-loop
        "tool",         # IDE, linter, debugger
        "alternative",
        "frequency",    # "before every commit", "on save"
    ),
    "clinical": (
        "medication",   # metformin, aspirin
        "procedure",    # arthroscopy, MRI
        "symptom",      # nausea, headache
        "severity",     # mild, severe
        "alternative",  # "X instead of Y" choices
        "frequency",    # "every 6 hours", "twice daily"
    ),
}


def build_slot_bio_labels(domain: str) -> list[str]:
    """BIO tag list for a domain's slot taxonomy.

    Returns ``["O", "B-<slot>", "I-<slot>", ...]`` in deterministic
    order with ``"object"`` appended as a domain-common catch-all so
    conversational gold round-trips cleanly.
    """
    slots = list(SLOT_TAXONOMY.get(domain, ("object",))) + ["object"]
    labels: list[str] = ["O"]
    for slot in slots:
        labels.append(f"B-{slot}")
        labels.append(f"I-{slot}")
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            deduped.append(label)
    return deduped


__all__ = [
    "ADMISSION_DECISIONS",
    "INTENT_CATEGORIES",
    "INTENT_LABEL_DESCRIPTIONS",
    "SLOT_TAXONOMY",
    "STATE_CHANGES",
    "build_slot_bio_labels",
]
