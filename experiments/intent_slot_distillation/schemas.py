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
from pathlib import Path
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

Domain = Literal["conversational", "software_dev", "clinical", "swe_diff"]

DOMAINS: tuple[Domain, ...] = (
    "conversational", "software_dev", "clinical", "swe_diff",
)

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
        "frequency",    # "every 6 hours", "twice daily"
    ),
    "swe_diff": (
        "file_path",   # astropy/modeling/separable.py
        "function",    # _cstack, URLValidator
        "symbol",      # class / variable / module identifier from a diff
        "test_path",   # tests/modeling/test_separable.py
        "issue_ref",   # "#12345" / upstream tracker IDs
        "alternative", # preceding implementation the patch replaces
    ),
}


# ---------------------------------------------------------------------------
# Domain manifest — explicit wiring of domain → (corpus / taxonomy /
# adapter output).  Every training / evaluation tool that used to
# string-interpolate ``f"gold_{domain}.jsonl"`` should go through this
# registry so adapter ↔ data ↔ taxonomy stays consistent and can be
# grepped in one place.
# ---------------------------------------------------------------------------

_EXPERIMENT_ROOT = Path(__file__).resolve().parent
_CORPUS_ROOT = _EXPERIMENT_ROOT / "corpus"
_TAXONOMY_ROOT = _EXPERIMENT_ROOT / "taxonomies"
_ADAPTER_ROOT = _EXPERIMENT_ROOT / "adapters"
_DEPLOYED_ROOT = Path.home() / ".ncms" / "adapters"


@dataclass(frozen=True)
class DomainManifest:
    """One row of the domain ↔ adapter ↔ corpus registry.

    Every training script and benchmark helper reads from this
    registry rather than string-interpolating domain names.  Makes
    it obvious which adapter was trained on which corpus — and
    catches mismatches at import time (e.g. adding a domain to
    the Literal but forgetting to register its corpus files).
    """

    name: Domain
    description: str
    intended_content: str
    gold_jsonl: Path
    sdg_jsonl: Path
    adversarial_train_jsonl: Path
    taxonomy_yaml: Path
    adapter_output_root: Path
    deployed_adapter_root: Path
    default_version: str = "v4"

    def training_inputs(self) -> tuple[Path, Path, Path]:
        """(gold, sdg, adversarial_train) — the three corpora the
        training loop concatenates for LoRA fine-tuning."""
        return (self.gold_jsonl, self.sdg_jsonl, self.adversarial_train_jsonl)

    def deployed_path(self, version: str | None = None) -> Path:
        """~/.ncms/adapters/<name>/<version>/ — where the extractor
        chain looks at runtime."""
        return self.deployed_adapter_root / (version or self.default_version)


DOMAIN_MANIFESTS: dict[Domain, DomainManifest] = {
    "conversational": DomainManifest(
        name="conversational",
        description=(
            "Conversational persona / preference corpus "
            "(LongMemEval-shaped dialog)."
        ),
        intended_content=(
            "User messages in multi-turn chat; positive/negative/"
            "habitual/difficulty/choice preference signals; state "
            "changes phrased as 'I used to X, now Y'."
        ),
        gold_jsonl=_CORPUS_ROOT / "gold_conversational.jsonl",
        sdg_jsonl=_CORPUS_ROOT / "sdg_conversational.jsonl",
        adversarial_train_jsonl=_CORPUS_ROOT
        / "adversarial_train_conversational.jsonl",
        taxonomy_yaml=_TAXONOMY_ROOT / "conversational.yaml",
        adapter_output_root=_ADAPTER_ROOT / "conversational",
        deployed_adapter_root=_DEPLOYED_ROOT / "conversational",
    ),
    "software_dev": DomainManifest(
        name="software_dev",
        description=(
            "Prose software-development content — ADRs, RFCs, design "
            "docs, post-mortems, threat models."
        ),
        intended_content=(
            "Natural-language state changes about software: 'Decision: "
            "use Postgres.  Supersedes ADR-003.' / 'Root cause: config "
            "drift after v2.3 rollout.'  NOT raw git diffs — see "
            "'swe_diff' for the diff-aware adapter."
        ),
        gold_jsonl=_CORPUS_ROOT / "gold_software_dev.jsonl",
        sdg_jsonl=_CORPUS_ROOT / "sdg_software_dev.jsonl",
        adversarial_train_jsonl=_CORPUS_ROOT
        / "adversarial_train_software_dev.jsonl",
        taxonomy_yaml=_TAXONOMY_ROOT / "software_dev.yaml",
        adapter_output_root=_ADAPTER_ROOT / "software_dev",
        deployed_adapter_root=_DEPLOYED_ROOT / "software_dev",
    ),
    "clinical": DomainManifest(
        name="clinical",
        description=(
            "Clinical case-report corpus — PMC Open Access narratives."
        ),
        intended_content=(
            "Sections of case reports: presentation, investigations, "
            "differential / initial / final diagnosis, treatment, "
            "outcome.  State changes = diagnostic revisions."
        ),
        gold_jsonl=_CORPUS_ROOT / "gold_clinical.jsonl",
        sdg_jsonl=_CORPUS_ROOT / "sdg_clinical.jsonl",
        adversarial_train_jsonl=_CORPUS_ROOT
        / "adversarial_train_clinical.jsonl",
        taxonomy_yaml=_TAXONOMY_ROOT / "clinical.yaml",
        adapter_output_root=_ADAPTER_ROOT / "clinical",
        deployed_adapter_root=_DEPLOYED_ROOT / "clinical",
    ),
    "swe_diff": DomainManifest(
        name="swe_diff",
        description=(
            "Software engineering diff-aware corpus — SWE-bench "
            "Verified style issue bodies + PR discussions + resolving "
            "patches + test patches."
        ),
        intended_content=(
            "Raw GitHub issue/PR artefacts with diff headers, function "
            "signatures, file paths.  State changes = resolving_patch "
            "retires prior impl / test_patch declares new invariant.  "
            "Distinct from 'software_dev' which covers PROSE software "
            "documentation — this adapter parses code-shaped content."
        ),
        gold_jsonl=_CORPUS_ROOT / "gold_swe_diff.jsonl",
        sdg_jsonl=_CORPUS_ROOT / "sdg_swe_diff.jsonl",
        adversarial_train_jsonl=_CORPUS_ROOT
        / "adversarial_train_swe_diff.jsonl",
        taxonomy_yaml=_TAXONOMY_ROOT / "swe_diff.yaml",
        adapter_output_root=_ADAPTER_ROOT / "swe_diff",
        deployed_adapter_root=_DEPLOYED_ROOT / "swe_diff",
        default_version="v1",
    ),
}


def get_domain_manifest(name: Domain) -> DomainManifest:
    """Look up a domain's manifest; raises KeyError for unknowns."""
    return DOMAIN_MANIFESTS[name]


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

    # Multi-head outputs (Sprint 2).  ``None`` when the extractor
    # doesn't produce that head (e.g. zero-shot baselines).
    topic: str | None = None
    topic_confidence: float | None = None
    admission: AdmissionDecision | None = None
    admission_confidence: float | None = None
    state_change: StateChange | None = None
    state_change_confidence: float | None = None
    # 6th head (v6+) — query-shape.  None when the adapter predates
    # v6 or the head confidence is below the dispatch threshold.
    shape_intent: "ShapeIntent | None" = None
    shape_intent_confidence: float | None = None

    def is_confident(self, threshold: float = 0.7) -> bool:
        return self.intent_confidence >= threshold


#: Admission routing decision — coarse content-worthiness gate.
AdmissionDecision = Literal["persist", "ephemeral", "discard"]
ADMISSION_DECISIONS: tuple[AdmissionDecision, ...] = (
    "persist", "ephemeral", "discard",
)

#: State-change classification for ingest-time zone building.
#: Feeds TLG's L2 node induction + retirement extractor.
StateChange = Literal[
    "declaration",  # "Auth-svc: method = OAuth" — new state
    "retirement",   # "Deprecated X in favor of Y" — retires prior state
    "none",         # no state transition
]

#: Query-shape classification (6th head, v6+).  Produced by the
#: ``shape_intent_head`` on query-voice input.  Replaces the hand-
#: coded regex parser in ``ncms.domain.tlg.query_parser``.  ``none``
#: means the query does not match any TLG grammar shape — dispatcher
#: returns pure hybrid retrieval unchanged (zero-confidently-wrong
#: invariant preserved).
ShapeIntent = Literal[
    "current_state",
    "before_named",
    "concurrent",
    "origin",
    "retirement",
    "sequence",
    "predecessor",
    "transitive_cause",
    "causal_chain",
    "interval",
    "ordinal_first",
    "ordinal_last",
    "none",
]
SHAPE_INTENTS: tuple[ShapeIntent, ...] = (
    "current_state", "before_named", "concurrent", "origin",
    "retirement", "sequence", "predecessor", "transitive_cause",
    "causal_chain", "interval", "ordinal_first", "ordinal_last",
    "none",
)
STATE_CHANGES: tuple[StateChange, ...] = ("declaration", "retirement", "none")


@dataclass
class GoldExample:
    """One labelled example from gold, LLM-labelled, or SDG.

    Multi-head labels (``topic``, ``admission``, ``state_change``) are
    optional — old gold files that only carry intent/slots still
    validate.  The training loop computes losses only on heads that
    have labels (per-example label masking), so you can scale up the
    label set without reflowing the whole corpus.

    Serialises to JSONL::

        {"text": "I love sushi.",
         "domain": "conversational",
         "intent": "positive",
         "slots": {"object": "sushi"},
         "topic": "food_pref",
         "admission": "persist",
         "state_change": "none",
         "split": "gold",
         "source": "hand-labeled"}
    """

    text: str
    domain: Domain
    intent: Intent
    slots: dict[str, str] = field(default_factory=dict)

    # Multi-head optional labels (Sprint 2).  None means "unlabeled";
    # training skips loss contribution for unlabeled heads.
    topic: str | None = None
    admission: AdmissionDecision | None = None
    state_change: StateChange | None = None
    # 6th head (v6+) — query-shape.  None on ingest-side training
    # rows (they're not queries); set on query-side training rows
    # imported from MSEB gold.
    shape_intent: ShapeIntent | None = None

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
    """One cell of the evaluation matrix.

    Intent + slot + joint are reported for every method.  Topic /
    admission / state-change are reported as ``None`` when the
    corresponding head is absent from the method's output (e.g. all
    zero-shot baselines) OR when the gold split has no labels for
    that head (e.g. legacy gold without multi-head tags).
    """

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

    # Sprint 2 multi-head metrics.  None = head absent or not scored
    # (gold rows lacked labels for that head).
    topic_f1_macro: float | None = None
    admission_f1_macro: float | None = None
    state_change_f1_macro: float | None = None
    n_topic_labeled: int = 0
    n_admission_labeled: int = 0
    n_state_change_labeled: int = 0
