# Adding a new domain (v9 plugin architecture)

This guide walks through building a new NCMS domain end-to-end.  A
domain defines the vocabulary the 5-head SLM classifier learns for
a slice of user content — slots (what kinds of entities exist),
topics (how content is grouped), an optional gazetteer (closed-
vocabulary entity catalog), a diversity taxonomy (generation-time
variety), and archetypes (stratified template seeds that steer the
SDG expander).

We'll build a hypothetical `legal` domain covering litigation,
contracts, and compliance notes.  By the end you'll have a
directory that loads cleanly via `load_domain()`, passes
integration tests, and is ready for Phase B'.2 corpus generation
and Phase C' adapter training.

**Prerequisites:** familiarity with Python, YAML, and NCMS
section §27 (the intent-slot SLM design).  You do not need any
prior NCMS contributions — the plugin system is designed so a new
domain is a pure-YAML contribution touching no Python.

**Related reading:**
- `docs/research/v9-domain-plugin-architecture.md` — full design
- `src/ncms/application/adapters/domain_loader.py` — loader + validator
- `adapters/domains/clinical/` — most complete reference domain
- `adapters/domains/conversational/` — open-vocabulary reference
- `adapters/domains/software_dev/` — catalog-heavy reference

---

## 1. Directory layout

Every domain lives in one directory under `adapters/domains/`:

```
adapters/domains/legal/
  domain.yaml         # required — slot + topic vocabulary, file refs
  gazetteer.yaml      # optional — closed-vocab inference-time catalog
  diversity.yaml      # required — generation-time taxonomy
  archetypes.yaml     # required — stratified template seeds
```

The directory name IS the domain name — `load_domain()` enforces
that `domain.yaml::name` equals the directory name.  Pick a
lowercase, underscore-separated identifier that won't collide
with existing domains (`software_dev`, `conversational`,
`clinical` are taken).

Create the directory first:

```bash
mkdir -p adapters/domains/legal
```

Downstream code locates your domain automatically: `load_all_domains()`
walks every subdirectory of `adapters/domains/` that contains a
`domain.yaml`.  You do not register your domain anywhere else —
`schemas.py::DOMAIN_MANIFESTS` is hydrated from the YAML registry
at import time (`_hydrate_from_domain_registry`).

---

## 2. `domain.yaml` — the manifest

This file declares the domain's vocabulary: what **slots** the
role head will target, what **topics** the topic head will
predict, and pointers to the three supporting files.

Create `adapters/domains/legal/domain.yaml`:

```yaml
# v9 legal domain spec.
#
# Legal content — litigation notes, contract observations,
# compliance decisions.  Prose only: we're not trying to parse
# case citations or statutory references programmatically (those
# belong in a structured legal-research tool, not a cognitive
# memory layer).

name: legal
description: >-
  Legal prose — litigation notes, contract observations,
  compliance decisions, regulatory triage.
intended_content: |
  Short natural-language legal observations:
  "Plaintiff filed motion to dismiss under Rule 12(b)(6)."
  "Switched from arbitration clause to judicial forum in v3."
  "GDPR DPIA required for new vendor pipeline."
  "Decision: retain Clifford Chance for M&A work."

slots:
  - case_type        # "breach of contract", "patent infringement", ...
  - party_role       # "plaintiff", "defendant", "amicus", ...
  - document_type    # "motion to dismiss", "MSA", "NDA", ...
  - venue            # "SDNY", "EDTX", "AAA arbitration", ...
  - regulation       # "GDPR", "CCPA", "HIPAA", ...
  - counsel          # law firm or individual counsel
  - alternative      # standard contrast-partner role placeholder
  - frequency        # "annually", "pre-closing", "on renewal"

topics:
  - litigation
  - contracts
  - compliance
  - corporate_transactions
  - employment_law
  - ip_law
  - other

# File refs — the defaults match the filenames below, listed
# explicitly so it's obvious what each file is.
gazetteer_path: gazetteer.yaml
diversity_path: diversity.yaml
archetypes_path: archetypes.yaml

default_adapter_version: v9
```

**What slots are for:** the `role_head` classifies every entity
span (gazetteer hit OR GLiNER hit) as primary / alternative /
casual / not_relevant.  The slot TAG tells the trainer *which
kind* of entity this is — a `case_type` span gets different
role-assignment behavior than a `party_role` span.  Slots are
also the keys the gazetteer + diversity files pivot on.

**What topics are for:** the `topic_head` emits one label per
memory, auto-populating `Memory.domains`.  Topics should be
coarse enough that the head has ~30+ training rows per label
once the corpus is generated.  `other` is a valid topic — use it
as a safety net so the head doesn't have to refuse classification.

**Slot naming conventions:**
- Use singular nouns (`medication`, not `medications`).
- Include `alternative` and `frequency` as standard placeholders
  if archetypes reference `{alternative}` / `{frequency}` in
  phrasings.  They're optional but the other three domains all
  include them.

**Topic naming conventions:**
- Snake_case, no spaces.
- 5-8 labels is the sweet spot.  Fewer than 5 → insufficient
  discrimination; more than 10 → per-label training data gets
  thin.

---

## 3. `gazetteer.yaml` — closed-vocab entity catalog

**Use a gazetteer when:** the slot space is finite and well-
defined (medications, programming languages, legal case types).
The gazetteer enables deterministic `detect_spans()` longest-
match lookup at inference time, which beats GLiNER on recall
for known surfaces.

**Skip the gazetteer when:** the slot is fundamentally open-
vocabulary (e.g. conversational `object`: any noun a user cares
about).  A closed catalog either leaks (incomplete) or biases
training.  In that case, delete the `gazetteer_path` line from
`domain.yaml` and rely on GLiNER + inline diversity.

For `legal`, a gazetteer makes sense — case types, regulations,
document types, and venues have bounded, authoritative
vocabularies.  Create `adapters/domains/legal/gazetteer.yaml`:

```yaml
# Legal gazetteer — seed entries.
# Sources: Black's Law Dictionary (case types), Federal Rules
# (document types), PACER court codes (venues), regulatory
# authoritative texts (GDPR, CCPA, HIPAA).  Scope: common-
# knowledge subset sufficient for text-classifier training —
# not a substitute for Lexis or Westlaw.

entries:
# ── case_type ────────────────────────────────────────────────
- canonical: breach of contract
  slot: case_type
  topic: litigation
  aliases:
  - contract breach
  - contractual breach
  source: blacks-law

- canonical: patent infringement
  slot: case_type
  topic: ip_law
  aliases:
  - patent suit
  source: blacks-law

- canonical: trademark infringement
  slot: case_type
  topic: ip_law
  source: blacks-law

- canonical: wrongful termination
  slot: case_type
  topic: employment_law
  aliases:
  - unlawful termination
  source: blacks-law

# ── party_role ───────────────────────────────────────────────
- canonical: plaintiff
  slot: party_role
  topic: litigation
  aliases:
  - claimant
  source: frcp

- canonical: defendant
  slot: party_role
  topic: litigation
  aliases:
  - respondent
  source: frcp

- canonical: amicus curiae
  slot: party_role
  topic: litigation
  aliases:
  - amicus
  - friend of the court
  source: frcp

# ── document_type ────────────────────────────────────────────
- canonical: motion to dismiss
  slot: document_type
  topic: litigation
  aliases:
  - MTD
  - 12(b)(6) motion
  source: frcp-12

- canonical: master services agreement
  slot: document_type
  topic: contracts
  aliases:
  - MSA
  source: common-contract

- canonical: non-disclosure agreement
  slot: document_type
  topic: contracts
  aliases:
  - NDA
  source: common-contract

# ── regulation ───────────────────────────────────────────────
- canonical: general data protection regulation
  slot: regulation
  topic: compliance
  aliases:
  - GDPR
  source: eu-regulation-2016-679

- canonical: california consumer privacy act
  slot: regulation
  topic: compliance
  aliases:
  - CCPA
  source: cal-civ-code-1798

- canonical: health insurance portability and accountability act
  slot: regulation
  topic: compliance
  aliases:
  - HIPAA
  source: us-code-42-usc-1320d

# ── venue ────────────────────────────────────────────────────
- canonical: southern district of new york
  slot: venue
  topic: litigation
  aliases:
  - SDNY
  source: pacer

- canonical: eastern district of texas
  slot: venue
  topic: litigation
  aliases:
  - EDTX
  source: pacer

- canonical: american arbitration association
  slot: venue
  topic: litigation
  aliases:
  - AAA
  source: aaa-rules

# ── counsel ──────────────────────────────────────────────────
- canonical: outside counsel
  slot: counsel
  topic: corporate_transactions
  source: generator-helper

- canonical: in-house counsel
  slot: counsel
  topic: corporate_transactions
  source: generator-helper

# ── alternative ──────────────────────────────────────────────
- canonical: the prior arrangement
  slot: alternative
  topic: other
  source: generator-helper

- canonical: the previous vendor
  slot: alternative
  topic: other
  source: generator-helper

# ── frequency ────────────────────────────────────────────────
- canonical: annually
  slot: frequency
  topic: compliance
  source: common-cadence

- canonical: pre-closing
  slot: frequency
  topic: corporate_transactions
  source: common-cadence

- canonical: on renewal
  slot: frequency
  topic: contracts
  source: common-cadence
```

**Entry schema** (validated by `_load_gazetteer`):

| Field | Required | Notes |
|---|---|---|
| `canonical` | yes | Unique within the gazetteer. Lowercased preferred. |
| `slot` | yes | Must appear in `domain.yaml::slots`. |
| `topic` | yes | Must appear in `domain.yaml::topics`. |
| `aliases` | no | List of surface forms the detector should also match. |
| `source` | no | Citation / provenance (free text). |
| `notes` | no | Free-text clarification. |

Duplicate canonicals are rejected.  Slot / topic values are
cross-validated against the manifest.  That's exactly where
`clinical` has 536 entries and `software_dev` has 712 — the
gazetteer grows over time via the catalog self-evolution loop
(§28 of CLAUDE.md), but a seed of 50-150 entries per slot is
plenty to start.

---

## 4. `diversity.yaml` — generation-time taxonomy

The diversity file is **only consumed at corpus-generation
time**.  It controls how the SDG expander rotates through
entity types when building training rows, so no single
subcategory dominates a batch (the "100 pizza rows" failure
mode).

This is separate from the gazetteer because they have different
jobs:
- **Gazetteer:** inference-time lookup — "did the user just
  mention a known entity?"
- **Diversity:** generation-time variety — "which *type* of
  entity should the generator use in the next training row?"

Two sourcing modes exist:
- `source: gazetteer` — filter the gazetteer by one or more
  slots and sample from the matching entries.  Use this for
  catalog-backed domains where the gazetteer IS the authoritative
  entity list.
- `source: inline` — list example members directly.  Use this
  when the slot is open-vocabulary (conversational `object`) or
  when you want to steer generation toward a specific subset
  that isn't cleanly expressible as a slot filter.

**Design principle (from the clinical domain build):** one job
per YAML file.  Diversity partitions by slot; sub-specialty /
topical steering lives in archetypes, not in a third tag axis.
The clinical domain has exactly six diversity nodes — one per
slot — and steers cardiology-vs-endocrinology at the archetype
level.  Don't try to stratify diversity by clinical specialty
AND by slot; it makes the generator combinatorial and the
coverage audits unreadable.

For `legal`, a slot-partitioned diversity file works.  Create
`adapters/domains/legal/diversity.yaml`:

```yaml
# v9 legal diversity taxonomy.
#
# Partitions the gazetteer by slot so the generator rotates
# across entity types within an archetype batch.  Topical
# sub-specialty (IP vs M&A vs employment) is an archetype
# concern — described in archetypes.yaml via per-archetype
# example_utterances + phrasings.

entities:
  case_types:
    description: "All case types in the gazetteer — contract, IP, employment, etc."
    topic_hint: litigation
    source: gazetteer
    filter_slots: [case_type]
    n_examples_per_batch: 8

  document_types:
    description: "Filings, contracts, and transactional documents."
    topic_hint: contracts
    source: gazetteer
    filter_slots: [document_type]
    n_examples_per_batch: 8

  regulations:
    description: "Regulatory regimes users reference by name."
    topic_hint: compliance
    source: gazetteer
    filter_slots: [regulation]
    n_examples_per_batch: 6

  venues:
    description: "Courts, arbitration panels, and administrative tribunals."
    topic_hint: litigation
    source: gazetteer
    filter_slots: [venue]
    n_examples_per_batch: 4

utility:
  party_roles:
    description: "Party roles — plaintiff, defendant, amicus, etc."
    topic_hint: litigation
    source: gazetteer
    filter_slots: [party_role]
    n_examples_per_batch: 4

  counsels:
    description: "Counsel placeholders — in-house vs outside."
    topic_hint: corporate_transactions
    source: gazetteer
    filter_slots: [counsel]
    n_examples_per_batch: 2

  frequencies:
    description: "Legal cadences — annually, pre-closing, on renewal."
    topic_hint: compliance
    source: gazetteer
    filter_slots: [frequency]
    n_examples_per_batch: 2

  alternatives:
    description: "Generic contrast partners for choice archetypes."
    topic_hint: other
    source: gazetteer
    filter_slots: [alternative]
    n_examples_per_batch: 2
```

**Inline-mode example** (copied from conversational for reference
— do NOT mix inline and gazetteer sourcing gratuitously; pick
the one that matches your slot's nature):

```yaml
foods:
  cuisines:
    description: "World / regional cuisines users express preferences about."
    topic_hint: food_pref
    source: inline
    n_examples_per_batch: 6
    examples:
      - Italian
      - Japanese
      - Mexican
      - Thai
      - Vietnamese
      - Korean
      - Ethiopian
      - Peruvian
```

**Validator requirements** (`_load_diversity`):
- Leaf nodes have a `source` key (`inline` | `gazetteer`).
- `topic_hint` must appear in `domain.yaml::topics`.
- For `source: gazetteer`, `filter_slots` must be non-empty and
  every slot must be declared in `domain.yaml::slots`.
- For `source: inline`, `examples` must be non-empty.
- Non-leaf nodes are mappings without a `source` key — they just
  group leaves for presentation in coverage reports.  Depth is
  unrestricted.
- At least one leaf node must exist overall.

**Cross-file validation:** `load_domain` verifies that every
`source: gazetteer` node has at least one matching gazetteer
entry.  If you filter on a slot with no entries, loading fails
loudly — better than silently generating a row where the
generator can't pick a member.

---

## 5. `archetypes.yaml` — stratified template seeds

Archetypes are the SDG expander's prompt-engineering contract.
Each archetype:
- targets one specific **joint label combination** (intent +
  admission + state_change + topic + role assignment)
- carries **example_utterances** that seed the Spark Nemotron
  prompt
- carries **phrasings** (template strings) that pin entity-slot
  combinations so the role head gets consistent supervision
- requests a specific **number of gold + SDG rows** (`n_gold`,
  `n_sdg`) so stratification is explicit

An archetype's `role_spans` list says "this prompt asks the LLM
to generate text containing exactly 1 primary `case_type` span
and 1 alternative `case_type` span."  At training time, the
role_head sees both spans and learns which one was primary.

Create `adapters/domains/legal/archetypes.yaml`:

```yaml
# v9 legal archetypes — starter set.
#
# Six archetypes covering the major joint label combinations.
# Full 16-archetype stratified set (with discard / not_relevant /
# query-voice archetypes) lands when the corpus regeneration
# pipeline is wired up.

archetypes:
  - name: positive_counsel_retention
    intent: positive
    admission: persist
    state_change: declaration
    topic: corporate_transactions
    role_spans:
      - {role: primary, slot: counsel, count: 1}
    n_gold: 40
    n_sdg: 160
    target_min_chars: 30
    target_max_chars: 180
    batch_size: 10
    description: >-
      Team retains counsel for ongoing work — declaration plus
      persist plus primary role on the counsel slot.
    example_utterances:
      - "Retained Clifford Chance for the European M&A work."
      - "Bringing on outside counsel for the antitrust review."
      - "Hired Wilson Sonsini to handle the IPO prep."
      - "Engaged in-house counsel as lead on the regulatory filing."
    phrasings:
      - "Retained {primary} for {scope}."
      - "Engaged {primary} — {rationale}."
      - "Brought on {primary} to handle {scope}."

  - name: choice_venue_selection
    intent: choice
    admission: persist
    state_change: declaration
    topic: litigation
    role_spans:
      - {role: primary, slot: venue, count: 1}
      - {role: alternative, slot: venue, count: 1}
    n_gold: 40
    n_sdg: 160
    target_min_chars: 40
    target_max_chars: 220
    batch_size: 10
    description: >-
      Team selects a litigation venue over an alternative, with
      rationale.  Classic choice archetype.
    example_utterances:
      - "Filed in SDNY instead of AAA arbitration — jury trial preferable here."
      - "Elected EDTX over SDNY for the patent claim — docket velocity."
      - "Going with AAA arbitration rather than SDNY, confidentiality matters."
    phrasings:
      - "Filed in {primary} instead of {alternative} — {rationale}."
      - "Chose {primary} over {alternative} for {scope}."
      - "Went with {primary} rather than {alternative}."

  - name: habitual_compliance_cadence
    intent: habitual
    admission: persist
    state_change: none
    topic: compliance
    role_spans:
      - {role: primary, slot: regulation, count: 1}
      - {role: primary, slot: frequency, count: 1}
    n_gold: 30
    n_sdg: 140
    target_min_chars: 25
    target_max_chars: 140
    batch_size: 10
    description: >-
      Recurring compliance review / filing cadence — ongoing
      regulatory work with an established timing marker.
    example_utterances:
      - "We run a GDPR DPIA annually for the vendor pipeline."
      - "HIPAA risk assessment every renewal cycle."
      - "CCPA disclosure refresh on renewal."
    phrasings:
      - "{primary} review {frequency}."
      - "We run {primary} {frequency}."
      - "{primary} assessment {frequency} — {scope}."

  - name: negative_contract_termination
    intent: negative
    admission: persist
    state_change: retirement
    topic: contracts
    role_spans:
      - {role: primary, slot: document_type, count: 1}
    n_gold: 30
    n_sdg: 140
    target_min_chars: 30
    target_max_chars: 180
    batch_size: 10
    description: >-
      Team terminates or lets expire a contract — retirement
      state change with negative intent.
    example_utterances:
      - "Terminated the MSA after the breach notice went unanswered."
      - "Let the NDA lapse — no ongoing discussions."
      - "Cancelled the consulting engagement effective month-end."
    phrasings:
      - "Terminated {primary} — {rationale}."
      - "Let {primary} lapse."
      - "Cancelled {primary} — {outcome}."

  - name: neutral_filing_observation
    intent: none
    admission: persist
    state_change: none
    topic: litigation
    role_spans:
      - {role: casual, slot: document_type, count: 1}
    n_gold: 30
    n_sdg: 120
    target_min_chars: 30
    target_max_chars: 180
    batch_size: 10
    description: >-
      Factual litigation observation without expressed preference —
      pure status update.
    example_utterances:
      - "Plaintiff filed a motion to dismiss under Rule 12(b)(6)."
      - "Opposing party served the NDA draft this morning."
      - "Court entered the MSA as an exhibit to the pleading."
    phrasings:
      - "{party} filed {casual}."
      - "{casual} served on {date}."
      - "Court entered {casual} as {disposition}."

  - name: difficulty_temporary_friction
    intent: difficulty
    admission: ephemeral
    state_change: none
    topic: litigation
    role_spans:
      - {role: primary, slot: document_type, count: 1}
    n_gold: 20
    n_sdg: 100
    target_min_chars: 25
    target_max_chars: 140
    batch_size: 10
    description: >-
      Transient process friction — shouldn't persist to long-term
      memory.  Admission=ephemeral.
    example_utterances:
      - "MSA redlines are a mess today — will come back to it tomorrow."
      - "NDA negotiation stuck on IP carve-outs this morning."
      - "Motion filing hit a PACER outage — retry later."
    phrasings:
      - "{primary} is giving us trouble today."
      - "Stuck on {primary} this afternoon."
      - "{primary} acting up — will revisit tomorrow."
```

**Archetype schema** (validated by `_load_archetypes`):

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Unique within the domain. |
| `intent` | yes | Must be one of `INTENT_CATEGORIES` in schemas.py. |
| `admission` | yes | `persist` / `ephemeral` / `discard`. |
| `state_change` | yes | `declaration` / `retirement` / `none`. |
| `topic` | no | If set, must appear in `domain.yaml::topics`. |
| `description` | yes | Human-readable prompt guidance. |
| `role_spans` | yes | List of `{role, slot, count}` — drives the role-head supervision. |
| `example_utterances` | no | Few-shot seeds for the Spark Nemotron prompt. |
| `phrasings` | no | Template strings with `{slot}` placeholders. |
| `phrasings_path` | no | Alternative: load phrasings from a sibling file. |
| `n_gold`, `n_sdg` | no | Defaults 30 / 150. |
| `target_min_chars`, `target_max_chars` | no | Defaults 20 / 200. |

**Cross-file validation** (`_validate_archetype_entity_sources`):
every archetype's `role_spans` references a slot that either
has gazetteer entries OR has an inline diversity node.
Otherwise the generator has no way to pick entities for that
archetype and `load_domain` fails.

**Archetype design tips:**
- Cover every combination of (intent × admission × state_change)
  your corpus needs.  The three shipped domains each start with
  6 archetypes covering the main head positive/negative/habitual/
  choice/neutral/difficulty pattern; the stratified set grows to
  ~16 in Phase B'.2.
- Include one `admission: ephemeral` archetype per domain so the
  admission head learns to down-route transient content.
- Include one `intent: none` + `role: casual` archetype so the
  role head learns the not-primary pattern.
- Example utterances should reference entities from your own
  gazetteer — they're few-shot prompts, and referencing real
  catalog entries signals the LLM to stay in-vocabulary.

---

## 6. Validate the domain

With all four files in place, load the domain in a REPL to
catch schema errors early:

```python
>>> from pathlib import Path
>>> from ncms.application.adapters.domain_loader import load_domain
>>> spec = load_domain(Path("adapters/domains/legal"))
>>> spec.name
'legal'
>>> spec.slots
('case_type', 'party_role', 'document_type', 'venue',
 'regulation', 'counsel', 'alternative', 'frequency')
>>> len(spec.gazetteer)
24
>>> spec.has_gazetteer
True
>>> [n.qualified_name for n in spec.diversity.nodes]
['entities.case_types', 'entities.document_types',
 'entities.regulations', 'entities.venues',
 'utility.party_roles', 'utility.counsels',
 'utility.frequencies', 'utility.alternatives']
>>> [a.name for a in spec.archetypes]
['positive_counsel_retention', 'choice_venue_selection',
 'habitual_compliance_cadence', 'negative_contract_termination',
 'neutral_filing_observation', 'difficulty_temporary_friction']
```

If any of the following validation errors surface, fix the
indicated file before proceeding:

| Error | Cause | Fix |
|---|---|---|
| `entry X references slot Y not in domain.slots` | Gazetteer uses a slot name not declared in domain.yaml | Either add the slot to `domain.yaml::slots` or rename the gazetteer entry's slot. |
| `entry X topic Y not in domain.topics` | Gazetteer uses a topic not in the domain's topic vocab | Add to `domain.yaml::topics` or fix the gazetteer entry. |
| `diversity node X filter_slots Y: no gazetteer entries match` | Diversity filters on an empty slot | Either add gazetteer entries in that slot or remove the node. |
| `archetype X role_spans Y.slot: Z not in domain.slots` | Archetype asks for a slot the domain doesn't declare | Add to `domain.yaml::slots` or fix the archetype. |
| `archetype X needs role=Y slot=Z` (entity-source gap) | Archetype requires entities from a slot with no gazetteer entries AND no inline diversity | Add entries or convert to open-vocab by adding an inline diversity node. |

The validator surfaces file paths + offending keys in every
message, so debugging is usually "open the file, find the line,
fix it."

Once `load_domain` succeeds, `load_all_domains` automatically
picks up your domain the next time NCMS imports
`ncms.application.adapters.schemas`.  You do not need to
manually register it.

---

## 7. Add an integration test

Every shipped domain has a `tests/integration/test_v9_<name>_yaml.py`
file that validates structural properties.  Use
`tests/integration/test_v9_conversational_yaml.py` or
`tests/integration/test_v9_clinical_yaml.py` as a template.

Create `tests/integration/test_v9_legal_yaml.py`:

```python
"""v9 legal YAML domain integration test."""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_DOMAIN = _REPO / "adapters/domains/legal"


@pytest.fixture(scope="module")
def spec():
    from ncms.application.adapters.domain_loader import load_domain
    if not _DOMAIN.is_dir():
        pytest.skip(f"legal domain not present at {_DOMAIN}")
    return load_domain(_DOMAIN)


class TestLegalYAMLDomain:
    def test_loads_cleanly(self, spec):
        assert spec.name == "legal"
        assert "case_type" in spec.slots
        assert "litigation" in spec.topics

    def test_gazetteer_nonempty(self, spec):
        """Closed-vocab domain — gazetteer must have entries."""
        assert spec.has_gazetteer
        assert len(spec.gazetteer) >= 20

    def test_every_slot_has_gazetteer_entries(self, spec):
        """Every declared slot except open-vocab placeholders
        should appear in the gazetteer so the role head has
        something to classify."""
        seen_slots = {e.slot for e in spec.gazetteer}
        for slot in spec.slots:
            assert slot in seen_slots, f"slot {slot!r} has no gazetteer entries"

    def test_diversity_covers_all_topics(self, spec):
        """Every declared topic should be represented in
        diversity nodes so the topic head gets training signal."""
        seen_topics = {n.topic_hint for n in spec.diversity.nodes}
        missing = set(spec.topics) - seen_topics - {"other"}
        # Allow a couple of topics to appear only via archetypes.topic
        # — diversity is the primary source but not the only one.
        assert len(missing) <= 2, f"topics missing from diversity: {missing}"

    def test_archetypes_span_head_classes(self, spec):
        """Starter archetype set should hit every admission
        class + state_change class + at least 4 distinct intents."""
        admissions = {a.admission for a in spec.archetypes}
        state_changes = {a.state_change for a in spec.archetypes}
        intents = {a.intent for a in spec.archetypes}
        assert admissions >= {"persist", "ephemeral"}
        assert state_changes >= {"declaration", "retirement", "none"}
        assert len(intents) >= 4
```

Run it:

```bash
uv run pytest tests/integration/test_v9_legal_yaml.py -v
```

---

## 8. Run the coverage audit

Once your gazetteer is non-trivial (say >100 entries), verify
it actually covers the benchmark text corpora you plan to use.
The coverage audit tool measures:

1. **Catalog hit rate** — % of benchmark rows where
   `detect_spans` finds at least one gazetteer surface.
2. **Missing-surface targets** — noun phrases appearing in the
   corpus ≥ N times that aren't in the gazetteer.
3. **Per-slot distribution** — which slots are over- / under-
   represented.

```bash
uv run python scripts/v9/catalog_coverage.py --domain legal
uv run python scripts/v9/catalog_coverage.py \
  --domain legal --min-mentions 3 --top-n 100
```

The missing-surface list is your backfill queue: add the highest-
frequency missing surfaces to `gazetteer.yaml` and rerun.  Aim
for 80%+ hit rate on your target benchmark before training.

---

## 9. Generate training data (forward-looking, Phase B'.2)

Once Phase B'.2 lands the corpus-regeneration pipeline, you'll
generate training data via the standard CLI:

```bash
ncms adapters generate-sdg --domain legal --n-per-archetype auto
```

The generator reads `DomainSpec.archetypes` + `.diversity` +
`.gazetteer` and produces per-archetype batches using Spark
Nemotron, with openai-backed quality gates (reject rows that
fail detect_spans, violate the archetype's role_spans shape, or
fall outside `target_min_chars` / `target_max_chars`).

Output lands at:

- `adapters/corpora/v9/legal/gold.jsonl` — human-curated seeds
- `adapters/corpora/v9/legal/sdg.jsonl` — generator output
- `adapters/corpora/v9/legal/adv.jsonl` — adversarial / edge-case rows

These paths are the `DomainSpec.gold_jsonl_path` etc. defaults;
override via `domain.yaml::paths` if needed.

---

## 10. Train the adapter (forward-looking, Phase C')

Phase C' lands the 5-head training loop.  Train a v9 adapter:

```bash
ncms adapters train --domain legal --version v9
```

The trainer reads `DomainSpec` + the three JSONL corpora and
writes:

- `adapters/checkpoints/legal/v9/` — raw LoRA weights + manifest
- `adapters/checkpoints/legal/v9/head_metrics.json` — per-head F1

Deploy to the user-facing adapter cache:

```bash
ncms adapters deploy --domain legal --version v9
```

This copies the checkpoint to
`~/.ncms/adapters/legal/v9/` (~2.4 MB).  The runtime picks it up
automatically when `NCMS_DEFAULT_ADAPTER_DOMAIN=legal` is set;
override the discovery path with `NCMS_SLM_CHECKPOINT_DIR` for
canary/staging deployments.

---

## Reference: the three shipped domains

When in doubt, read the existing domain YAML — all three are
~47 lines of `domain.yaml` plus varying gazetteer / diversity /
archetype size.

- **`adapters/domains/software_dev/`** — largest gazetteer (712
  entries), 9 slots, 6-archetype starter set, diversity
  partitioned by slot with topical grouping
  (`languages_and_patterns`, `frameworks_and_libs`, etc.).
  Reference for: catalog-heavy domains with mixed slot roles.

- **`adapters/domains/clinical/`** — 536 gazetteer entries, 6
  slots, diversity partitioned strictly by slot (this is where
  the "one job per YAML file" principle was extracted — the
  design comment in `clinical/diversity.yaml` explains why).
  Reference for: medical / scientific domains with bounded
  authoritative vocabularies.

- **`adapters/domains/conversational/`** — no gazetteer, 3
  slots (`object`, `alternative`, `frequency`), 28 hierarchical
  inline diversity nodes (~900 example members), 6-archetype
  starter set.  Reference for: open-vocabulary domains where the
  `object` slot is any noun the user cares about and GLiNER
  handles the long tail.

Pick the closest reference, copy its structure, and work from
there.
