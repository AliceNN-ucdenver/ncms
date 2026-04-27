# Subject-Centered Graph Design

**Status:** research proposal — v0.2 (rewrite grounded in current code)
**Owner:** NCMS core
**Last revised:** 2026-04-26
**Related:** `ctlg-design.md`, `ctlg-implementation-plan.md`, `ctlg-subject-binding-plan.md`, `ctlg-grammar-contract.md`
**Phase claims (verifiable, codex-reviewable):**
- `phases/phase-a-claims.md` — Subject payload + canonicalization
- `phases/phase-b-claims.md` — Subject index + lookup helpers
- `phases/phase-c-claims.md` — `SubjectBindingContext` at query time
- `phases/claims-format.md` — verification protocol

---

## 0. TL;DR

The CTLG pilot has shown that **relation parsing is a solved-enough problem and
subject binding is the gating constraint**. The 2026-04-27 stress mini:

```text
oracle subject grounding:        9 improved / 3 worse / 22 composed
naive subject anchor:            0 improved / 8 worse
conservative subject resolver:   0 improved / 0 worse  (abstained on ambiguous)
live retrieval (no CTLG):        r@1 = 0.0625, r@5 = 0.1458, mrr = 0.0961
```

CTLG already has a working cue tagger, a synthesizer that composes
`TLGQuery` from cue tags + SLM signals, walkers that read
`trace.intent.subject`, and an L2 ENTITY\_STATE store with bitemporal
metadata. The missing layer is a **durable, indexed, multi-subject
spine** plus **query-time subject binding context** so CTLG knows
which timeline to walk.

This document specifies that layer. It is **a small extension to
existing infrastructure**, not a rewrite — most of the substrate
already exists in code. Three early phases (A/B/C) produce the data
needed to decide whether the later phases (resolver, walker rewrites,
SPLADE deprecation) are worth doing.

---

## 1. Where We Are (Code-Verified)

This section was rewritten to match the actual codebase. Every claim
has a citation; the original v0.1 doc over-credited what was missing
and under-credited what already exists.

### 1.1 What's already wired

| Capability | Where | Notes |
|---|---|---|
| `store_memory(subject=str)` API | `memory_service.py:331,513-529` | Single-string subject; creates an Entity with `type="subject"` and links to L2. |
| L2 ENTITY\_STATE node with `metadata.entity_id` | `domain/models.py:256-280`, `ingestion/l2_detection.py:140-177` | Schema carries entity\_id, state\_key, state\_value, state\_previous, state\_alternative, source, slm\_state\_change. |
| Bitemporal fields on L2 | `domain/models.py:274-277` | `valid_from`, `valid_to`, `observed_at`, `ingested_at` all populated. |
| Reconciliation (supersedes / supersedes / refines / supports / conflicts) | `application/reconciliation_service.py:339-388` | Flips `is_current=False`, sets `valid_to`, emits `SUPERSEDES`+`SUPERSEDED_BY` with `retires_entities`. |
| Subject-isolated zone walks | `domain/tlg/zones.py:167-234` (`build_subject_graph`, `compute_zones`) + `application/tlg/dispatch.py::_load_subject_zones` | Computes a per-subject zone graph on demand from existing `SUPERSEDES`/`REFINES` edges. |
| 6 subject→state query helpers | `infrastructure/storage/sqlite_memory_nodes.py:105-230` | `get_current_entity_states`, `get_entity_states_by_entity`, `get_current_state`, `get_state_at_time`, `get_state_changes_since`, `get_state_history`. |
| TLGQuery has `subject` field | `domain/tlg/semantic_parser.py:111-121` | Plus `referent`, `secondary`, `scope`, `temporal_anchor`, `depth`, `scenario`. |
| Synthesizer takes SLM signals | `domain/tlg/semantic_parser.py:124-157` (`SLMQuerySignals`) + `synthesize(tagged, slm_signals)` | Combines CTLG cue tags with five-head signals (intent, topic, state\_change, slots, role\_spans). |
| CTLG cue tagger has `B-SUBJECT` / `I-SUBJECT` labels | `domain/tlg/cue_taxonomy.py:72-73,110-111` | Synthesizer reads via `cues.subject` index; populates `TLGQuery.subject`. |
| Walkers read `trace.intent.subject` | `application/tlg/walkers.py` (sequence:144, predecessor:181, interval:217, range:257, before\_named:290, transitive\_cause:387) | Subject is already plumbed end-to-end on the dispatch path. |
| SLM `role_spans` on memory | `application/adapters/schemas.py:468-486` (`RoleSpan`), persisted via `store_helpers.py:61-88` | Each span: char offsets, surface, canonical, slot, role ∈ {primary, alternative, casual, not\_relevant}. |
| Causal edge ingest from cue tags | `application/ingestion/causal_edges.py:1-186` | Reads `memory.structured["ctlg"].cue_tags`; persists `CAUSED_BY`/`ENABLES` with cue\_type, surface, char offsets, confidence, ingest\_memory\_id. |
| MSEB shadow diagnostics | `benchmarks/mseb/backends/ncms_backend.py` (`_subject_anchor_candidates`, `_oracle_subject_candidate`, `ctlg_shadow_query`) | Diagnostic-only; never affect live ranking. |

### 1.2 CTLG runtime status (revised after codex round-2 audit)

The earlier framing said CTLG was "shadow-only end-to-end." That
overstated. The accurate picture:

- CTLG composition (`tlg/composition.py::compose_grammar_with_results`)
  IS wired to mutate `ranked_mids` when called.
- It is gated by `config.temporal_enabled` (default: `False`) AND
  the grammar trace's HIGH/MEDIUM confidence threshold.
- In default deployments (`temporal_enabled=False`) CTLG does not
  run.
- Production hub deployments (NemoClaw) may set
  `temporal_enabled=True`. At that point CTLG composition runs
  live, not shadow.
- The benchmark harness has separate shadow modes
  (`ctlg-implementation-plan.md` §5: `gold_cues`, `adapter_only`,
  `ctlg_shadow`) that capture diagnostics without routing through
  live ranking. The `ctlg_on` mode (live composition during
  benchmarks) is checklisted as not-done.

So: CTLG is **off-by-default in production, opt-in via config flag,
and shadow-only in benchmarks.** It has not been deployed against
production traffic with `temporal_enabled=True` AND validated to
improve rather than harm ranking, which is what the design doc's
section §0 stress-mini results address.

The subject-binding gap is the reason that gate hasn't been
crossed: with naive subject anchor (the only resolver shipped
without oracle context), CTLG worsens 8 queries and improves 0.
That's why subject binding is the unblock.

### 1.3 What's actually missing

After auditing the code, the gaps are smaller than v0.1 implied. Five
concrete items:

1. **No index on `memory_nodes.metadata->entity_id`.** Every
   subject→state query is a full-table scan filtered by
   `node_type='entity_state'` plus `json_extract(...) = ?`. Confirmed
   in `infrastructure/storage/migrations.py:176-178` — only
   `idx_mnodes_memory`, `idx_mnodes_type`, `idx_mnodes_parent` exist.
   At ~10K L2 nodes this is cheap; at 100K+ it dominates query latency.

2. **No multi-subject support.** `MemoryService.store_memory(subject:
   str | None)`, `IndexTask.subject` (`index_worker.py:57-60`), and L2
   `metadata.entity_id` are all singular. A memory like *"We migrated
   app:xyz and app:abc both to Postgres"* loses one of the timelines
   silently.

3. **No subject canonicalization at write time.** Two ingests of the
   same subject under "app xyz" vs "application xyz" vs "the xyz
   service" produce three Entity rows with disjoint timelines.
   Aliasing is supposed to live somewhere; today it lives nowhere.

4. **No `SubjectBindingContext` at query time.** `search()` and
   `recall()` accept a `query` string and `domain` filter. There is no
   way to pass `subject_hint`, `active_document_id`,
   `active_episode_id`, or `active_project`. Production has this
   context (the agent knows which document is open, which episode is
   active) but the API throws it away.

5. **No deterministic subject resolver.** The `_subject_anchor_*`
   machinery in `benchmarks/mseb/backends/ncms_backend.py` is
   shadow-diagnostic only; nothing wires it into `retrieve_lg` or
   `search`. The `ctlg-subject-binding-plan.md` doc captures the rule
   resolver design; it has not been built.

The four edge types defined-but-unused (`CURRENT_STATE_OF`,
`PRECEDES`) and "document describes subject" / "episode contains
subject" linkage are *deferrable* — they may or may not be needed
once the indexed L2 path proves out.

---

## 2. Why Subject Binding Is The Bottleneck

### 2.1 The diagnosis (unchanged from v0.1, code-validated)

NCMS's graph confounds two distinct relations:

- **Mention** — entities that appear in a memory. Encoded as
  `MENTIONS_ENTITY` edges, populated by GLiNER and SLM slot output.
- **Aboutness** — the subject *whose state evolves* as memories
  accumulate. Currently encoded only on L2 ENTITY\_STATE nodes via
  `metadata.entity_id`, with no fast lookup and no multi-subject
  support.

A query like *"What database did we decide on application xyz?"* is
not asking for a fuzzy match around the word *database*. It is
asking:

```text
subject  = application:xyz
scope    = database
relation = current
```

CTLG's synthesizer can produce that `TLGQuery` already
(`domain/tlg/semantic_parser.py::synthesize`). The walker
`_dispatch_current_intent` can answer it — *if and only if*
`trace.intent.subject` is set to a real timeline that has L2 state
nodes. Today that resolution happens via:

1. CTLG cue tagger emits `B-SUBJECT` if the surface text contains a
   subject mention (`cue_taxonomy.py:72`).
2. Synthesizer's `_RuleCtx.subject` falls back to the parser's
   heuristic if the cue is missing (`semantic_parser.py:649`).
3. The walker calls `_load_subject_zones(store, subject)` which calls
   `store.get_entity_states_by_entity(subject)` — a JSON-extract
   scan.

The chain breaks at step 1/2 for **deictic queries** ("this decision",
"the current choice", "the framework"). The CTLG corpus + grammar are
not the bottleneck; *which subject the query is about* is.

### 2.2 Why "topic" doesn't substitute for "subject"

Topic classifies content area: `database`, `framework`, `auth`. It
answers *what kind of thing is this memory about*. Subject identifies
the durable owner: `application:xyz`, `service:auth-api`, `adr:004`.
It answers *whose timeline does this state event belong to*.

The five-head SLM produces both: `topic_head` writes
`memory.structured.intent_slot.topic`; `role_head` writes role-tagged
spans. The role with `role="primary"` is the closest existing signal
to a subject — but only one role span is "primary" per memory by
design (open-vocab; there isn't a "subject of this utterance" head).
For multi-subject memories the role head conflates the roles.

---

## 3. The Multi-Subject Question

The user has explicitly called out multi-subject capability. Here's
the disciplined treatment.

### 3.1 What kinds of multi-subject memories exist?

Three categories worth handling, in increasing order of complexity:

| Category | Example | Subjects |
|---|---|---|
| **Single-subject (the common case)** | "auth-service migrated from PostgreSQL to CockroachDB." | `service:auth-api` |
| **Cross-subject relationship** | "ADR-004 supersedes ADR-002 for the auth-service." | `adr:004`, `adr:002`, `service:auth-api` (state event applies to *all three* timelines) |
| **Co-occurrence (not multi-subject)** | "Postgres and Redis both scale horizontally." | None — these are mentioned entities, not subjects-whose-state-evolves. |

The third row is important: not every memory needs subjects, and not
every entity is a subject. The primary diagnostic question is *whose
timeline is updated by this memory?* If the answer is "no one's, this
is a fact / documentation / observation," there are no subjects.

### 3.2 What does the SLM tell us about subjects?

The five-head SLM emits `role_spans` with role ∈ {`primary`,
`alternative`, `casual`, `not_relevant`}. The semantics today
(`adapters/schemas.py:468-486`):

- **`primary`** — the main entity-of-interest the utterance is about.
  Example: in *"I really love sushi and ramen,"* `sushi` and `ramen`
  may both be tagged primary (preference subject). In *"auth-service
  migrated to CockroachDB,"* `CockroachDB` is the primary *value* —
  but the *subject* is `auth-service`, which is typically tagged as a
  separate role (or comes from caller metadata).
- **`alternative`** — the rejected/predecessor option in a
  comparison. Example: `SQLite` in the migration utterance.
- **`casual`** / **`not_relevant`** — mentioned but not central.

**Key observation:** the SLM's `primary` role does not always
correspond to "the subject whose state evolves." For state-change
memories, the primary span is usually the *new state value* (the
thing the subject changed to), and the *subject* is a separate
concern that today must come from caller metadata, document
context, or a later resolver pass.

### 3.3 What does CTLG tell us about subjects?

The CTLG cue tagger does have a dedicated `B-SUBJECT` / `I-SUBJECT`
label (`cue_taxonomy.py:72-73`). When the query (or memory)
explicitly names the subject —

> *"What database did we decide on **application xyz**?"*

— the cue tagger marks the subject span and the synthesizer
populates `TLGQuery.subject`. This is a **strong signal when present**
but absent for deictic queries.

### 3.4 Multi-subject schema choice

Three options, in increasing invasiveness:

**Option A (incremental, recommended for Phase A):**
*Keep L2's singular `entity_id` as the canonical subject. Reuse the
existing `MENTIONS_ENTITY` edge from memory to each subject Entity,
tagging the edge metadata with `role: "primary_subject" |
"co_subject"`.*

```text
memory m1
  MENTIONS_ENTITY (role=primary_subject) -> entity:application:xyz
  MENTIONS_ENTITY (role=co_subject)      -> entity:application:abc

L2 entity_state for m1 → entity_id = application:xyz
```

This keeps the 14-EdgeType set untouched. Subject-typed Entities
already exist (`type="subject"`); `link_memory_entity` already creates
the row; we add metadata.role to distinguish primary/co-subject from
other mentions.

A multi-subject memory may still produce **multiple L2 nodes** when
the state event applies to multiple timelines (e.g. ADR-004
supersedes ADR-002 → two L2 nodes: one declaring ADR-004 active, one
retiring ADR-002). The L2 schema doesn't change; the ingest pipeline
emits one L2 per subject-timeline that's affected.

This is the smallest schema change — **zero new edge types, zero new
node kinds**. It composes cleanly with existing walker code: each
walker still resolves a single subject via `trace.intent.subject`;
multi-subject queries are answered by walking each timeline and
merging.

**Option B (delayed):** Generalize L2 metadata to `entity_ids:
list[str]`. Higher migration cost, breaks the
`get_entity_states_by_entity` shape, and the multi-subject shared-state
case is rare enough that emitting two L2s (Option A) is fine in
practice.

**Option C (out-of-scope):** A separate `SubjectMembership` table
with (`memory_id`, `subject_id`, `role`, `confidence`). Adds a third
storage primitive for a benefit Phase A/B don't need to demonstrate.

**Decision:** Option A for Phase A. Revisit if and only if Option A's
ablation shows multi-subject queries are a meaningful slice and
emitting one-L2-per-subject creates duplication problems.

### 3.5 How signals combine for multi-subject

At ingest time, the subject set for a memory is a **union of
sources**, ordered by trustworthiness:

1. **Caller-provided** (`store_memory(subject=...)`) — explicit; highest
   confidence.
2. **Document/episode metadata** — when ingesting a doc with
   `parent_doc_id` or an episode member, inherit the document/episode
   subject.
3. **SLM `primary` role span** — when the role head is confident *and*
   the span resolves to a known Entity, treat as a candidate subject.
4. **CTLG `B-SUBJECT` cue** — when the cue tagger marks a subject
   surface in memory-voice content, treat as a candidate subject.

The subject set is then **canonicalized** (§4.3) before any L2 is
written. Conflicts (caller says `service:auth`, SLM says
`service:authentication`) resolve by alias lookup or, failing that,
by trusting the higher-priority source.

At query time, subject candidates come from a different ranked list
(§5.1). The two paths are independent.

---

## 4. Proposed Ingest Contract

### 4.1 The Subject payload

Promote the current `subject: str | None` API to a structured payload,
serialized into `memory.structured["subjects"]` as a *list*:

```json
{
  "subjects": [
    {
      "id": "application:xyz",
      "type": "application",
      "primary": true,
      "aliases": ["application xyz", "app xyz", "xyz service"],
      "source": "caller|document|episode|slm_role|ctlg_cue|resolver",
      "confidence": 0.91
    }
  ]
}
```

For backward compatibility, `store_memory(subject="...")` continues
to accept a single string and is canonicalized into a one-element
list with `source="caller"`, `primary=true`. Callers that need
multi-subject pass `subjects=[Subject(...), Subject(...)]`.

The L2 creation logic uses `subjects[0].id` (or, when a state event
applies to a subset of subjects, walks each affected subject and
emits one L2 per timeline). The other subjects get `ABOUT_SUBJECT`
edges from L1.

### 4.2 Subject types as domain-plugin extension

Subject *types* (application, service, adr, document, person, patient,
session, ticket, repo, feature, agent…) are **per-domain**, declared
in domain.yaml under each domain plugin. There is no global enum.
This matches how slots work today (`adapters/domains/<name>/domain.yaml`).

Why: the subject taxonomy is the same trap as the retired
`shape_intent` enum. Different domains have different first-class
subject types. The clinical domain has `patient`, `encounter`,
`condition`; software\_dev has `application`, `service`, `adr`;
conversational has `person`, `session`. Forcing a global enum either
bloats it forever or loses domain-specific signal.

The doc shows a candidate cross-domain table for orientation:

| Type | Common in | Source signals |
|---|---|---|
| `application` | software\_dev | caller, repo metadata, SLM slot=service |
| `service` | software\_dev | caller, path, source\_agent |
| `adr` | software\_dev | document title, parent\_doc\_id |
| `document` | all | parent\_doc\_id |
| `episode` | all | parent\_id from L3 episode node |
| `project` | all | caller, NCMS\_PROJECT env var |
| `person` | conversational, software\_dev | caller, source\_agent, SLM slot=person |
| `patient` | clinical | caller, EHR metadata |
| `ticket` | software\_dev | tracker metadata |

…but the **enforced types come from the domain plugin**, not from this
table.

### 4.3 Subject canonicalization at write time

A `SubjectRegistry` resolves alias variants to a canonical id at
ingest time:

```python
SubjectRegistry.canonicalize(
    surface="application xyz",
    type_hint="application",
    domain="software_dev",
) -> "application:xyz"
```

Lookup order:
1. Exact match against the registry's `aliases` table.
2. Fuzzy match (lowercase + whitespace-norm) against existing subject
   ids in the registry.
3. Optional LLM normalizer (gated behind a feature flag) for the
   long-tail.
4. New canonical id minted from the surface (slugified) + type prefix.

The registry is a **table of `(canonical_id, type, alias)` rows**
seeded from caller-provided aliases and grown by ingest. This is the
piece that prevents silent timeline splits.

### 4.4 What does NOT change

- The 14 existing `EdgeType` values are untouched. No new edge types
  in Phase A.
- L2 ENTITY\_STATE schema is untouched. `metadata.entity_id` still
  carries the (canonicalized) primary subject.
- Reconciliation logic is untouched. SUPERSEDES/REFINES/SUPPORTS/CONFLICTS
  still flow through `ReconciliationService` per
  `(entity_id, state_key)`.
- Bitemporal fields are untouched.

The only schema change in Phase A is adding **one SQLite index**:

```sql
CREATE INDEX IF NOT EXISTS idx_mnodes_subject
  ON memory_nodes (json_extract(metadata, '$.entity_id'))
  WHERE node_type = 'entity_state';
```

This is the single biggest perf unlock and a 5-line migration.

---

## 5. Proposed Query Contract

### 5.1 SubjectBindingContext

Add a new optional parameter to `search()`, `recall()`, and
`retrieve_lg()`:

```python
@dataclass(frozen=True)
class SubjectBindingContext:
    subject_hint: str | None = None        # Explicit caller hint
    active_document_id: str | None = None  # Open document
    active_episode_id: str | None = None   # Active conversation/sprint
    active_project: str | None = None      # Workspace
    active_repo: str | None = None         # Source repo
    active_session_id: str | None = None   # Conversation session
```

Every field is optional; passing `None` is equivalent to today's
behaviour. Most production deployments **have** this context — the
hub knows which document is open, which agent is asking. The API
just doesn't accept it yet. C-style "thread context through" — no
new infrastructure.

### 5.2 Subject candidate ordering at query time

When CTLG dispatch needs a subject, the resolver evaluates these
candidate sources in order and returns the highest-scoring
non-abstain match:

```text
1. SubjectBindingContext.subject_hint                    (caller-explicit)
2. SubjectBindingContext.active_{document,episode,...}_id   (active context)
3. CTLG cue tagger's B-SUBJECT span (synthesizer.subject)   (in-utterance)
4. SLM role_spans + scope match against subject registry    (in-utterance)
5. Candidate-grounded ranking over scored pool subjects     (retrieved-set)
6. Learned ranker (Phase F, if Phase D shows lift)
7. Abstain
```

Abstention is a feature. CTLG must not guess a subject for ambiguous
deictic queries. `conservative subject resolver: 0 improved / 0 worse`
on the stress mini is the correct shape.

### 5.3 Multi-subject queries

A query like *"What databases did we decide for app:xyz and app:abc?"*
binds to two subjects. The dispatcher:

1. Synthesizes a single `TLGQuery(relation=current, scope=database)`.
2. Resolves the subject set `{application:xyz, application:abc}` from
   caller hint or candidate-grounded ranking.
3. Walks each subject's timeline independently
   (`_load_subject_zones` per subject).
4. Merges results; the composition layer
   (`tlg/composition.py::compose_grammar_with_results`) produces a
   ranking that prepends both grammar answers.

The walker code does not change. The change is in the dispatcher's
willingness to call walkers in a loop and the composition layer's
willingness to surface multiple grammar answers.

---

## 6. Phased Plan (revised)

The original v0.1 plan had six phases (A–F) front-loading work that
shouldn't ship until ablation justifies it. The revised plan ships
A/B/C as the *minimum-viable subject spine* and gates D/E/F on
shadow data.

### Phase A — Subject payload + canonicalization (1.5 weeks)

Deliverables:

- New `Subject` dataclass + `memory.structured["subjects"]` payload.
- `store_memory(subjects: list[Subject] | None, subject: str | None)`
  signature; legacy single-string subject promoted into the list.
- `SubjectRegistry` (SQLite-backed): `(canonical_id, type, alias)`
  with O(1) alias lookup.
- Canonicalization at write time: every ingest path (inline ingest,
  async indexer, document publish, reindex, MSEB backend) writes
  canonical subject ids.
- Multi-subject ingest produces one L2 per affected timeline; co-subjects
  get `MENTIONS_ENTITY` edges with `metadata.role="co_subject"`.

Acceptance:

- Round-trip test: ingest "application xyz" + "the xyz service" + "app
  xyz" → all three resolve to one canonical id, one subject entity,
  and one timeline.
- MSEB backend writes canonical subjects; `subject_map` derived from
  canonical ids has zero alias splits.
- Multi-subject test: an ingest like ADR-004 supersedes ADR-002
  produces two L2 nodes, one per ADR timeline.

### Phase B — Subject index + helpers (3 days)

Deliverables:

- SQLite migration: `idx_mnodes_subject` on
  `json_extract(metadata, '$.entity_id')` filtered by
  `node_type='entity_state'`.
- New helpers in `infrastructure/storage/sqlite_memory_nodes.py`:
  `get_subject_states(subject_id, scope=None, as_of=None,
  is_current=None)` — single composable query that the walkers can
  use directly.
- Walker code updated to call the new helper instead of
  `get_entity_states_by_entity` + filter loops.

Acceptance:

- `get_subject_states` query plan uses the index (verify with
  `EXPLAIN QUERY PLAN`).
- 100K-L2-node benchmark: subject→current-state lookup ≤ 5 ms p95.
- Existing `_load_subject_zones` and dispatchers produce identical
  output to before (regression test on CTLG stress mini).

### Phase C — `SubjectBindingContext` plumbing (1 week)

Deliverables:

- `SubjectBindingContext` dataclass.
- `search()`, `recall()`, `retrieve_lg()` accept an optional
  `subject_context` kwarg.
- Dispatcher precedence: `subject_hint` > active-context > cue >
  synthesizer-default.
- MCP tools surface the new kwargs.
- MSEB backend optionally pins gold subject through context
  (oracle-mode shadow diagnostic; never affects live ranking unless
  explicitly enabled).

Acceptance:

- Oracle-context shadow on stress mini reproduces the
  `oracle_subject` 9-improved/3-worse number from §0.
- Active-document hint binds CTLG without candidate guessing on at
  least one production-shaped scenario.
- Ambiguous deictic queries with no context abstain (no live
  ranking change).

### Decision gate (after C)

After A/B/C ship, run the §7 ablation. Three possible outcomes:

| Outcome | Action |
|---|---|
| **Subject graph + active context lifts CTLG queries materially over BM25+SPLADE.** | Proceed to D (resolver) and E (subject-walker rewrites). |
| **Subject graph helps when subject is known but active-context is rarely populated in production.** | Proceed to D (resolver only); skip E unless walkers have a separate problem. |
| **Subject graph is no better than current sparse retrieval.** | Stop. Keep A/B/C as plumbing wins (faster subject queries, multi-subject support, cleaner docs); CTLG remains a diagnostic-only augmentation. |

This is the honest decision point. v0.1 didn't have one.

### Phase D — Deterministic subject resolver (1 week)

Only proceeds if the gate after C is positive.

Deliverables:

- Feature ranker over the candidate pool, signals from
  `ctlg-subject-binding-plan.md` Phase 2:
  `harmonic_rank`, `subject_count`, `inverse_best_rank`,
  `relation_supported`, `answer_in_pool`, `alias_match`,
  `subject_type_prior`, `current_context_match`, `act_r_subject`,
  `pmi_subject`.
- Deterministic weights; live in shadow first.
- Per-query log of top-K candidates, scores, selected subject,
  margin, abstain reason.

Acceptance:

- Beats naive subject anchor (`0 improved / 8 worse`) on the stress
  mini.
- Conservative gates produce zero net regression vs baseline.
- Logs explain every composed and abstained query.

### Phase E — Walker subject-graph optimizations

Only proceeds if D lift is meaningful but walker latency is the
bottleneck.

Most walkers already resolve a subject via `_load_subject_zones`.
Phase E moves hot paths from "load all entity states for subject,
filter in Python" to "single SQL query with the new index."
Specifically:

- `_dispatch_current_intent`: `get_subject_states(subject, scope,
  is_current=True)` — single row.
- `_dispatch_origin_intent`: `get_subject_states(subject, scope)
  ORDER BY observed_at ASC LIMIT 1`.
- `_dispatch_predecessor`: index lookup + chronological seek.
- `_dispatch_transitive_cause`: keep the current causal-chain walker;
  no change needed.

This is performance work, not a redesign. May not be needed if the
Phase B index alone is fast enough.

### Phase F — Learned resolver

Only proceeds if D's deterministic resolver has clear ceiling.

Cold-start: train against MSEB gold + shadow-oracle disagreements
(the only labels that exist on day one). Sibling adapter to the SLM
+ CTLG cue tagger, not a sixth head. Per `ctlg-subject-binding-plan.md`
Phase 3.

### Phase G — SPLADE per-intent gating ablation

The honest ablation question: *does the subject graph make SPLADE
unnecessary for CTLG-class queries?*

Candidate intent gates (same shape as the existing reranker
intent-gating, per CLAUDE.md decision #17):

```text
SPLADE keeps:  fact_lookup, pattern_lookup, strategic_reflection
SPLADE off:    current_state_lookup, historical_lookup, change_detection,
               event_reconstruction
```

Run the §7 matrix. If the off-cells beat or match the on-cells, ship
the gate. **Do not propose a system-wide SPLADE deprecation;** the
MSEB headline (NCMS hybrid +0.14 to +0.45 r@1 over mem0 dense across
4 domains) was measured *with SPLADE on*. SPLADE earns its weight on
the queries the subject graph doesn't address.

---

## 7. Ablation We Need

Run CTLG-class query sets with these cells. Subject context is
*passed as oracle when needed* so we measure the ceiling, not the
resolver's accuracy.

| Cell | BM25 | SPLADE | Graph | CTLG | Subject ctx | Resolver |
|---|---|---|---|---|---|---|
| 1. baseline | ✓ | — | — | — | — | — |
| 2. +SPLADE | ✓ | ✓ | — | — | — | — |
| 3. +current graph | ✓ | ✓ | current | — | — | — |
| 4. +subject index (Phase B) | ✓ | ✓ | indexed L2 | — | — | — |
| 5. +CTLG (no resolver) | ✓ | ✓ | indexed L2 | shadow→on | — | — |
| 6. +oracle subject context | ✓ | ✓ | indexed L2 | on | oracle | — |
| 7. +deterministic resolver | ✓ | ✓ | indexed L2 | on | — | rule |
| 8. SPLADE gated off CTLG-class | ✓ | gated | indexed L2 | on | — | rule |

Primary metrics:
- right recall @1/5/10
- MRR
- CTLG composition rate
- subject binding accuracy (cells 7, 8)
- abstention precision
- regression count on non-temporal queries
- p95 latency with and without SPLADE

Read this ablation as a sequence: cell 4 vs 2 isolates the index
win; 6 vs 5 isolates the active-context win; 7 vs 6 measures
resolver quality; 8 vs 7 answers the SPLADE question per-intent.

The headline question (revised, narrower than v0.1):

> **Does subject-first graph retrieval, with active-context
> grounding, beat sparse expansion *for CTLG-class temporal/causal/
> state queries* — and is it neutral or better on non-CTLG queries?**

If yes for both halves: ship it, gate SPLADE per-intent. If yes only
for CTLG queries with regression elsewhere: ship the subject graph,
keep SPLADE everywhere, leave §6.G unbuilt.

---

## 8. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Subject canonicalization splits silently across alias variants | Phase A registry + a `subject_alias_collision` event when fuzzy lookup picks an existing canonical with low confidence; review queue. |
| Multi-subject memory creates duplicate L2 reconciliation work | One L2 per affected timeline is by design; reconciliation already keys on `(entity_id, state_key)` so timelines don't cross-contaminate. |
| Subject taxonomy drifts into another brittle classifier | Subject types live in domain.yaml plugins, not a global enum; the resolver is a feature ranker, not a classifier. |
| Active context binds incorrectly in production (e.g. wrong document open) | Resolver assigns lower weight to active-context than to in-utterance cues; abstention is the default for ambiguous queries. |
| Phase B index migration on a large prod DB | Migration is a `CREATE INDEX IF NOT EXISTS` on memory\_nodes; index build is O(N) once, then incremental. Schedule it during a maintenance window. |
| SPLADE removal regresses non-CTLG queries | Per-intent gating, not system-wide deprecation. The §7 ablation must measure non-CTLG queries explicitly. |
| Resolver overfits to MSEB stress mini | Phase F training set cold-starts from MSEB but expands to LongMemEval, NCMS hub traffic logs, and explicit corrections. |
| Phase A/B/C ship and the gate fails (subject graph doesn't lift) | A/B/C are still wins on their own — multi-subject support, perf index, active-context API. Stopping at C is a valid outcome. |

---

## 9. Decision

The v0.1 doc was right that subject binding is the bottleneck and
right that a subject-centered spine is the architectural fix. It was
wrong about scope: most of the substrate already exists, and the
remaining work is **promote-and-extend**, not a rewrite.

The revised bet:

```text
canonicalized subject payload at ingest
  + indexed L2 lookup
  + SubjectBindingContext at query
  → measure
  → resolver / walker / SPLADE-gating only if measurement says yes
```

Three weeks of work to the decision gate after Phase C. Honest stop
conditions if the data doesn't justify continuing. No new edge
types, no new node kinds, no neural resolver, no SPLADE
deprecation in the first cut.

---

## 10. Open Questions

These are real and unresolved. Flagging so a reviewer can push back
before code lands.

1. **Subject identity across episodes.** If the same `application:xyz`
   appears in multiple episodes / sessions / sprints, is its
   timeline shared (one subject across episodes) or scoped (one
   subject per episode)? The current code answers "shared" by
   default. Multi-tenant deployments may want the opposite.

2. **Subject ownership of entities.** A subject `service:auth-api`
   *uses* `entity:postgres`. That's `MENTIONS_ENTITY` today. Does it
   need a separate `SUBJECT_USES_ENTITY` edge, or is the L2's
   `(state_key=database, state_value=postgres)` already that
   relationship?

3. **Person and session subjects in conversational memory.** The
   conversational SLM already extracts preferences; the v9 conv
   adapter has no concept of "this conversation is about person X."
   Should `person:shawn` and `session:<id>` both be subjects? Or
   only one?

4. **Subjects that don't have state events.** A subject mentioned
   in many memories but never the focus of a state change has no
   L2 nodes and therefore no timeline to walk. Should the resolver
   bind to it anyway (because the user is clearly working with it)?
   Or only resolve to subjects that have at least one L2?

5. **Cold-start canonicalization.** The first time `application:xyz`
   is mentioned, the registry has no aliases. Heuristics or LLM
   normalization? Same problem as entity canonicalization, which is
   currently unsolved.

These questions don't block Phase A. They become unavoidable in
Phase D and beyond.
