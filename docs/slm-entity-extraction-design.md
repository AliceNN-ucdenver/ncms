# SLM-First Entity Extraction + TLG Vocabulary — Design (Option D')

Status: **design complete, under implementation**
Target: land in full before MSEB full-12 overnight run
Owner: shawnmccarthy
Related: `docs/slm-tlg-architecture.md`, `docs/p2-plan.md`, `docs/mseb-results.md`

---

## Motivation

The forensic TLG trajectory trace on MSEB-softwaredev mini surfaced that **100% of queries abstained** after Phases A–D landed. Proof traces said:

```
intent='entity_state_current': no subject inferred from 'what is the
  current status of ADR-0001?'
```

Meaning the query-time SLM shape classification was fine, but the
**subject-vocabulary induction at ingest time was producing garbage**.
Two independent bugs combined to cause this:

### Bug 1 — UUID/name mismatch in `vocabulary_cache._rebuild` (production)

`_rebuild()` (and the parallel `_rebuild_aliases` + `_compute_domain_nouns`)
calls `store.get_memory_entities(memory_id)` which returns **entity UUIDs**
(primary keys in the `entities` table). It then passes those UUIDs
straight into `SubjectMemory(entities=frozenset(linked))` and feeds them
into `induce_vocabulary()` — which treats them as surface-form strings.

Result: the L1 entity vocabulary is polluted with UUIDs like
`"b7e3a91c-…"` that will never match a human query. Even in non-MSEB
production workloads where subjects DO get induced, the query-side entity
lookup is crippled because the entity token space is full of UUIDs.

This is a **production bug** that ships in every NCMS deployment using
TLG. It has nothing to do with MSEB; MSEB just made it visible because
the forensic trace prints the vocabulary.

### Bug 2 — MSEB `subject` field never makes it into NCMS (benchmark design gap)

The MSEB corpus schema has a first-class `subject: str` field on every
memory — it's the ground-truth entity-subject of the state trajectory.
The MSEB NCMS backend passes it as a tag (`subject:<id>`):

```python
await svc.store_memory(
    content=m.content,
    tags=["mseb", f"subject:{m.subject}", f"mid:{m.mid}"],
    ...
)
```

`MemoryService.store_memory()` **stores the tag but never reads it**.
The ingestion pipeline has no concept of a caller-provided subject.
L2 ENTITY_STATE nodes only get created when
`extract_entity_state_meta()`'s regex zoo matches the content OR the SLM
`state_change_head` fires — neither of which is reliable on prose corpora.

Net effect on MSEB: even after Bug 1 is fixed, we still get ~0 L2 nodes
and the L1 vocabulary cache has nothing to induce over.

### The architectural insight

These two bugs forced the question: **why is GLiNER (209 MB, zero-shot,
60–75% F1) still primary for entity extraction when we have an SLM
(2.4 MB, fine-tuned, 96–98% F1) with a slot head specifically trained
on the domain vocabulary?**

Answer: we shipped the slot head but only wired its output to the
`memory_slots` table (a flat surface-form log). The `memory_entities`
table — which feeds the knowledge graph, TLG vocabulary, and retrieval
entity linking — is still fed exclusively by GLiNER.

**Six heads trained, five wired.** The sixth (slot) is the missing link.

---

## Option D' — Four Parts

Option D' fixes both production bugs AND promotes the slot head to
primary entity extractor. Four parts in impact order:

### Part 1 — UUID → name resolution in vocabulary_cache (production bug fix)

Add a new store method `get_memory_entity_names(memory_id) -> list[str]`
that does the SQL JOIN against the `entities` table:

```sql
SELECT e.name FROM memory_entities me
JOIN entities e ON me.entity_id = e.id
WHERE me.memory_id = ?
```

Update `_rebuild`, `_rebuild_aliases`, `_compute_domain_nouns` in
`vocabulary_cache.py` to call `get_memory_entity_names` instead of
`get_memory_entities`. One-line substitution per call site; three call
sites total.

**Scope:** production bug fix, not benchmark-specific. Every TLG-enabled
deployment gets this.

**Risk:** trivial. The new method is additive; old method stays for
callers that legitimately want IDs (dispatch's zone lookup).

### Part 2 — Wire SLM slot head → `memory_entities` (architectural improvement)

In `ingestion/pipeline.py::run_inline_indexing` the current entity
linking loop iterates `all_entities = manual + gliner_auto`. Change to:

1. **Collect SLM slot entities first** — `intent_slot_label.slots` maps
   slot label → surface forms. Every surface form with confidence ≥
   threshold becomes an entity candidate with typed label
   (e.g. `library`, `medication`, `service`).
2. **Collect GLiNER entities as fallback** — open-vocabulary catch-all
   for entities outside the SLM's trained slot schema.
3. **De-dupe by lowercase surface form** — SLM wins on tie (typed label
   > generic GLiNER type).
4. **Link via `_add_entity` + `link_memory_entity`** — same code path,
   just richer source.

Behaviour when SLM is off or extractor lacks a slot head: falls through
to GLiNER-only, identical to today. No regression.

**Scope:** every ingest path benefits. Knowledge graph entity quality,
TLG vocabulary quality, retrieval entity-overlap scoring all get more
typed, higher-confidence entities.

**Risk:** moderate. The slot head's confidence threshold needs to be
conservative enough that we don't flood the entity graph with
low-precision picks. Configurable via `slm_confidence_threshold`
(already exists, defaults to 0.7).

### Part 3 — MSEB subject injection (benchmark-only workaround — REPLACED BY PART 4)

**Originally planned:** in `benchmarks/mseb/backends/ncms_backend.py::ingest`,
after each `store_memory()` call, explicitly create an L2 ENTITY_STATE
MemoryNode with `metadata["entity_id"] = m.subject`. This forces the
vocabulary cache to see the MSEB subject as a first-class subject even
when the ingest pipeline's regex / SLM state-change detection doesn't
fire.

**Status:** superseded by Part 4. With `subject` as a first-class
parameter on `store_memory()`, MSEB passes `subject=m.subject` directly
and the ingest pipeline handles the ENTITY_STATE node creation
generically. No MSEB-specific code needed.

### Part 4 — `subject` kwarg on `store_memory()` (production API generalization)

**This is the part the design doc exists to capture, because it's the
deepest change and most easily lost in context pressure.**

#### Motivation

The research prototype had every memory tagged with a static `subject`
field (entity-state-root). NCMS dropped that field on the grounds that
"NCMS derives subject from entity_id in ENTITY_STATE metadata." That
derivation only works when ingest produces ENTITY_STATE nodes — which
is unreliable on prose corpora (regex misses, SLM state_change_head
not confident, etc.).

Two classes of callers legitimately know the subject at ingest time:
1. **Benchmark backends** (MSEB, LongMemEval) where corpus carries
   explicit subject metadata.
2. **Agent applications** (a ticket system reporting on ticket ABC-123,
   a monitoring agent reporting on service X, a patient record system)
   where the calling code knows exactly what entity the memory is
   about.

Forcing both to go through regex/SLM state-change detection is fragile
and throws away information the caller already has.

#### Design invariant — NO REGEX in the caller-asserted path

When `subject` is provided, the L2 ENTITY_STATE node is built from
three inputs and no regex:

- `entity_id` ← the caller-asserted subject (ground truth).
- `state_key` ← the SLM topic-head label when confident, otherwise
  the constant `"status"`. The SLM-v6 topic head is trained per-
  adapter on the domain taxonomy; its output is the right semantic
  key for state reconciliation in that domain.
- `state_value` ← the content stripped + truncated to 200 chars.
  Reconciliation compares values as a change-detection anchor, not
  as a parse target — the raw snippet is enough signal to
  distinguish "new state" from "restatement of prior state."

The old `extract_entity_state_meta` regex zoo is NOT called on this
path. It remains reachable only through the legacy (no-subject,
no-SLM-confident) code path as a final-fallback for cold-start
deployments without an adapter. Any future work that drifts back
into calling the regex when a caller-asserted subject is present is
a regression and should be rejected in review.

#### Design

Add a `subject: str | None = None` parameter to `MemoryService.store_memory()`:

```python
async def store_memory(
    self,
    content: str,
    *,
    subject: str | None = None,        # NEW — caller-asserted entity-subject
    memory_type: str = "fact",
    domains: list[str] | None = None,
    tags: list[str] | None = None,
    ...
) -> Memory:
```

Thread `subject` through to `ingestion.create_memory_nodes()` (already
receives `memory`, `content`, `all_entities`, `linked_entity_ids`,
`admission_features`). Inside `create_memory_nodes`:

1. If `subject` is provided AND the SLM/regex fork did NOT already
   produce an ENTITY_STATE node: create one explicitly with
   `metadata["entity_id"] = subject`. Link the memory's indexed entities
   as the entity set for vocabulary induction.
2. If `subject` is provided AND the SLM/regex fork DID produce an
   ENTITY_STATE node: reconcile — prefer the caller-provided subject
   (they know their domain; our regex is guessing).
3. If `subject` is None: existing behaviour unchanged — regex/SLM fork
   decides whether to create ENTITY_STATE.

#### MCP + HTTP surface

Add `subject` as an optional field to:
- `store_memory` MCP tool input schema
- `POST /memory` HTTP endpoint body
- `ncms load` CLI (already accepts `--subject`? verify; if not, add)
- `publish_document` gets `subject` too (documents about a specific
  system/service/project)

#### Domain model

No change to `Memory` model — subject stays derived from the L2 node's
`entity_id`. The `subject` kwarg is an **ingest-time hint** that
determines how the L2 node is built, not a new field on the row. This
preserves the architectural invariant that "subject = entity_id of the
ENTITY_STATE node that anchors this memory's state chain."

#### Migration

- MSEB backend calls switch from `tags=["subject:X"]` + no kwarg to
  `subject="X"` + no tag. Benchmark-specific Part 3 workaround deleted
  in the same commit.
- LongMemEval backend: same migration pattern.
- Existing production callers (agents, MCP clients): no change required
  — `subject` defaults to None, regex/SLM path unchanged.

**Risk:** API surface change. Mitigated by keyword-only argument +
backwards-compatible default. No existing caller breaks.

#### Why Part 4 matters

Without Part 4, every caller with explicit subject knowledge has to
either (a) hope regex catches it, (b) tag it and lose the tag in
ingest, or (c) pre-create ENTITY_STATE nodes out-of-band. Part 4 makes
"caller knows the subject" a first-class ingest concept.

---

## Execution Order

Parts 1, 2, 3, 4 land together in one commit for MSEB full-12 readiness:

1. **Part 1** (~15 LOC): add `get_memory_entity_names`, swap 3 call
   sites in `vocabulary_cache.py`.
2. **Part 2** (~60 LOC): augment the entity collection loop in
   `ingestion/pipeline.py::run_inline_indexing` to prefer slot-head
   output.
3. **Part 4** (~40 LOC): add `subject` kwarg to `store_memory` signature;
   thread through `_ingestion.create_memory_nodes`; ENTITY_STATE
   creation branch uses it.
4. **Part 3 (workaround) → superseded**: MSEB backend switches to
   `subject=m.subject` kwarg. No separate Part-3 code.
5. Run unit + integration tests.
6. Re-run `benchmarks/tlg_trajectory_trace.py` — expect non-UUID
   vocab, MSEB subject IDs as subjects, grammar_answer hits gold_mid
   on HIGH/MEDIUM confidence queries.
7. Commit. Kick off MSEB full-12 overnight.

---

## Verification

**Part 1:** vocabulary trace prints entity names, not UUIDs. Unit test
that `get_memory_entity_names` returns strings matching the
`entities.name` column.

**Part 2:** with SLM enabled, a memory with slot-head output
`{"library": ["pandas"]}` produces a `memory_entities` row for
"pandas". An entity with `kind="library"` is written to the graph.

**Part 4 / MSEB integration:** TLG trajectory trace over
softwaredev mini prints:
- Induced L1 vocab: 60+ subjects (not 0), entity tokens include ADR
  titles + referenced frameworks (not UUIDs).
- HIGH-confidence queries: grammar_answer matches gold_mid on ≥50% of
  `entity_state_current` shape queries.

**Regression:** full `uv run pytest tests/` clean.

---

## What We're Explicitly NOT Doing

- **Not** removing GLiNER — it's the open-vocabulary fallback for
  entities outside the SLM's trained schema. Keep it in the chain,
  demote to secondary.
- **Not** changing the `Memory` schema. Subject remains derived, not
  stored on the row.
- **Not** touching the reconciliation pipeline. State reconciliation
  continues to compare ENTITY_STATE nodes by `entity_id`; Part 4 just
  gives callers a way to seed that id.
- **Not** adding subject inference (LLM or otherwise). Part 4 is
  explicit caller hint; if the caller doesn't know, we fall back to
  the existing regex/SLM fork.
