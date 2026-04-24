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

Domain = Literal["conversational", "software_dev", "clinical"]

DOMAINS: tuple[Domain, ...] = (
    "conversational", "software_dev", "clinical",
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
        # Fine-grained, crisp functional categories for agent-SDLC
        # retrieval.  Boundary rules live in the LLM labeller prompt
        # + the SDG template pools — see
        # ``adapters/sdg/templates.py::SOFTWARE_DEV_TEMPLATES`` and
        # ``corpus/llm_slot_labeler.py::_SLOT_DESCRIPTIONS``.
        #
        # Agent queries expect direct typed handles:
        #   "what framework is service X?"  -> framework slot
        #   "what database do we use?"      -> database slot
        #   "what orchestration platform?"  -> platform slot
        "language",     # compiled/interpreted: Python, Rust, Go, TS, Java
        "framework",    # opinionated app framework: Django, React, Rails
        "library",      # imported dep that isn't a framework: Pydantic, Lodash
        "database",     # data stores / caches / queues / search indexes:
                        # Postgres, MongoDB, Redis, Kafka, Elasticsearch
        "platform",     # runtime / orchestration / cloud: Docker, K8s, AWS
        "tool",         # dev-time only: ruff, pytest, VS Code, Jenkins
        "pattern",      # architectural / coding pattern: async, CQRS, DI
        "alternative",  # contrast partner: "X over Y" / "instead of Y"
        "frequency",    # timing: "every commit", "on save"
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


# ---------------------------------------------------------------------------
# Domain manifest — explicit wiring of domain → (corpus / taxonomy /
# adapter output).  Every training / evaluation tool that used to
# string-interpolate ``f"gold_{domain}.jsonl"`` should go through this
# registry so adapter ↔ data ↔ taxonomy stays consistent and can be
# grepped in one place.
# ---------------------------------------------------------------------------

# Repo-relative paths.  Walk up from this file
# (src/ncms/application/adapters/schemas.py) to the repo root and
# anchor the artifact directories under ``adapters/``.  That's a
# first-class top-level directory (see adapters/README.md) so both
# built-in corpora/taxonomies/checkpoints AND user-added ones live
# in the same place and ship identically.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CORPUS_ROOT = _REPO_ROOT / "adapters" / "corpora"
_TAXONOMY_ROOT = _REPO_ROOT / "adapters" / "taxonomies"
_ADAPTER_ROOT = _REPO_ROOT / "adapters" / "checkpoints"
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
        default_version="v6",
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
            "drift after v2.3 rollout.'  Prose only — diff content "
            "is out of scope for v9."
        ),
        gold_jsonl=_CORPUS_ROOT / "gold_software_dev.jsonl",
        sdg_jsonl=_CORPUS_ROOT / "sdg_software_dev.jsonl",
        adversarial_train_jsonl=_CORPUS_ROOT
        / "adversarial_train_software_dev.jsonl",
        taxonomy_yaml=_TAXONOMY_ROOT / "software_dev.yaml",
        adapter_output_root=_ADAPTER_ROOT / "software_dev",
        deployed_adapter_root=_DEPLOYED_ROOT / "software_dev",
        default_version="v6",
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
        default_version="v6",
    ),
}


def get_domain_manifest(name: Domain) -> DomainManifest:
    """Look up a domain's manifest; raises KeyError for unknowns."""
    return DOMAIN_MANIFESTS[name]


# ---------------------------------------------------------------------------
# v9: hydrate SLOT_TAXONOMY + DOMAIN_MANIFESTS from the DomainSpec
# registry at module import.
#
# The inline dicts defined above serve as DEFAULTS / FALLBACKS for
# deployments where ``adapters/domains/`` isn't on disk (pip-install
# without the repo layout).  When the YAML registry loads
# successfully, its values override the inline defaults — the
# inline dicts become dead code in the common case, but stay as
# load-time safety for deployments that can't reach the YAML.
#
# Disable YAML hydration entirely via ``NCMS_V9_DOMAIN_LOADER=0``
# (escape hatch for debugging a YAML regression).  The inline
# defaults take over.
# ---------------------------------------------------------------------------


def _hydrate_from_domain_registry() -> None:
    """Populate SLOT_TAXONOMY + DOMAIN_MANIFESTS from YAML domains.

    Run at module import.  Failures (missing directory, YAML
    errors) fall back to the inline defaults silently — except
    validation errors from malformed YAML, which surface loudly
    so bad domain specs fail fast rather than silently reverting
    to stale defaults.
    """
    import os
    if os.environ.get("NCMS_V9_DOMAIN_LOADER", "1") == "0":
        return

    # Deferred import — domain_loader depends on constants defined
    # above in this module (INTENT_CATEGORIES, ADMISSION_DECISIONS,
    # STATE_CHANGES, ROLE_LABELS), so we can only import it after
    # those are in place.  Python's sys.modules cache handles the
    # circular import correctly: by the time domain_loader imports
    # schemas, the constants it needs are already defined.
    try:
        from ncms.application.adapters.domain_loader import (
            DomainValidationError,
            load_all_domains,
        )
    except ImportError:  # pragma: no cover
        return

    # Repo-root discovery: walk up from this module looking for
    # pyproject.toml, then check for adapters/domains/ underneath.
    from pathlib import Path
    here = Path(__file__).resolve()
    domains_root: Path | None = None
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            candidate = parent / "adapters" / "domains"
            if candidate.is_dir():
                domains_root = candidate
            break
    if domains_root is None:
        return

    try:
        specs = load_all_domains(domains_root)
    except DomainValidationError:
        # Don't mask validation errors — a broken YAML should halt
        # service startup rather than silently serve stale inline
        # dicts.  Developers get a clear error pointing at the bad
        # YAML file.
        raise

    for name, spec in specs.items():
        # Slot taxonomy: YAML wins over any inline default for this name.
        SLOT_TAXONOMY[name] = spec.slots  # type: ignore[index]

        # Manifest: synthesize from DomainSpec.  Fields map 1:1 with
        # the inline DomainManifest constructor shown above.
        DOMAIN_MANIFESTS[name] = DomainManifest(  # type: ignore[index]
            name=name,
            description=spec.description or f"{name} domain",
            intended_content=spec.intended_content,
            gold_jsonl=spec.gold_jsonl_path,
            sdg_jsonl=spec.sdg_jsonl_path,
            adversarial_train_jsonl=spec.adversarial_jsonl_path,
            taxonomy_yaml=_TAXONOMY_ROOT / f"{name}.yaml",
            adapter_output_root=spec.adapter_output_root,
            deployed_adapter_root=spec.deployed_adapter_root,
            default_version=spec.default_adapter_version,
        )


# NOTE: the call to _hydrate_from_domain_registry() lives at the
# very END of this module — it needs ADMISSION_DECISIONS,
# INTENT_CATEGORIES, ROLE_LABELS, STATE_CHANGES (defined below in
# the "Method outputs" section) because domain_loader imports
# them.  Calling here would hit a circular-import ImportError.


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
    # 6th head (v7+) — role-classified gazetteer spans.  Superset of
    # the ``slots`` dict: ``slots`` is derived from ``role_spans`` at
    # inference time (primary → typed slot, alternative → alternative
    # slot, casual/not_relevant → dropped).  Empty tuple when the
    # adapter predates v7 or no catalog spans were detected.
    role_spans: tuple[RoleSpan, ...] = field(default_factory=tuple)

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

#: Role classification (v7+ replaces the BIO slot head).  Assigned
#: per detected catalog surface in a row.  The architectural split:
#: the gazetteer (``catalog.detect_spans``) finds surfaces + their
#: catalog slot; the role head classifies WHAT THIS SURFACE IS
#: DOING in the sentence.  Primary spans become typed slot values
#: (``database=postgres``), alternative spans become the
#: ``alternative`` slot, casual/not_relevant spans are dropped.
Role = Literal[
    "primary",        # the main subject of the row for its slot
    "alternative",    # the X-vs-Y contrast partner being rejected
    "casual",         # mentioned in passing, NOT the row's subject
    "not_relevant",   # distractor / unrelated / noise
]
ROLE_LABELS: tuple[Role, ...] = (
    "primary", "alternative", "casual", "not_relevant",
)


@dataclass(frozen=True)
class DetectedSpan:
    """One gazetteer hit inside a row's text.

    ``char_start`` / ``char_end`` are character offsets into the
    original text (slice-compatible, end-exclusive).  ``surface`` is
    the exact substring as it appeared in the text; ``canonical`` and
    ``slot`` come from the authoritative catalog.  ``source_alias``
    records which catalog alias matched (handy for debugging misses).
    """

    char_start: int
    char_end: int
    surface: str       # literal substring from text
    canonical: str     # catalog canonical form
    slot: str          # catalog-authoritative slot
    topic: str         # catalog topic
    source_alias: str = ""


@dataclass(frozen=True)
class RoleSpan:
    """One role-labeled span — either a training target or inference
    output of the v7+ role head.

    Serialises as a nested dict inside ``GoldExample.role_spans``.  At
    training time the role label is the ground-truth; at inference
    time the role head predicts it and the surface + slot come from
    the gazetteer pass that preceded the model forward.
    """

    char_start: int
    char_end: int
    surface: str
    canonical: str
    slot: str
    role: Role
    # Optional training-time provenance.
    source: str = ""


# The ``ShapeIntent`` literal + ``SHAPE_INTENTS`` tuple were removed
# in v8.1 along with the failed ``shape_intent_head`` classifier.
# Query-shape classification is now produced compositionally by the
# CTLG synthesizer (:func:`ncms.domain.tlg.semantic_parser.synthesize`)
# from the cue-tag head's BIO-labeled output.


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

    # v7+ role-head ground-truth.  Empty list when the row predates
    # v7 labelling — the training loop skips the role loss for those
    # rows (per-example mask, same pattern as topic/admission/state).
    role_spans: list[RoleSpan] = field(default_factory=list)

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


# ---------------------------------------------------------------------------
# v9 domain-registry hydration — called at end of module import, AFTER
# all module-level constants the domain_loader needs (INTENT_CATEGORIES,
# ADMISSION_DECISIONS, ROLE_LABELS, STATE_CHANGES) are in place.
# ---------------------------------------------------------------------------

_hydrate_from_domain_registry()
