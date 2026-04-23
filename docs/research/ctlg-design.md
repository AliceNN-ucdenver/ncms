# Causal-Temporal Linguistic Geometry (CTLG)

**Status:** design — post-v7.2 pivot
**Supersedes:** v6 BIO slot-tagging experiment, v7 span-role slot extractor, v7.1/v7.2 shape_intent classification experiment
**Owner:** NCMS core
**Last revised:** 2026-04-23

---

## 0. Executive summary

CTLG replaces the query-side `shape_intent` classifier with a **compositional semantic parser** built on two primitives:

1. A **sequence-labeled cue tagger** (the repurposed 6th SLM head) that marks tokens with typed linguistic cues — causal, temporal, ordinal, modal, referent — based on the PDTB/AltLex/TempEval discourse literature rather than ad-hoc template labels.
2. A **rules-first synthesizer** that composes tagged cues into a structured `TLGQuery` logical form (axis + relation + referent + subject + scope + scenario), with LLM fallback for queries whose cue pattern doesn't match a known rule.

The ingest side stays on the already-working 5-head SLM (intent / topic / admission / state_change / role) + authoritative catalog-driven gazetteer. The **catalog becomes self-evolving**: novel surfaces that force an LLM fallback are captured, classified by the LLM, reviewed, and merged back into the catalog so next time the gazetteer handles them natively.

The zone graph (L1 atomic → L2 entity_state → L3 episode → L4 abstract) gains one new edge type, `CAUSED_BY`, populated by an ingest-time causal cue tagger. This enables the dispatcher to walk causal chains directly instead of inferring them from co-occurrence.

Counterfactual reasoning is a first-class axis of the query form, dispatched as a skip-edge graph traversal over the existing supersedes chain — no new infrastructure, just a `scenario` parameter.

End-to-end benchmarks (new CTLG suite + existing MSEB + LongMemEval) validate the pipeline before adoption.

---

## 1. Retrospective: what worked, what didn't, why

### 1.1 What landed cleanly

| Component | Evidence |
|-----------|----------|
| 5 content heads (intent/topic/admission/state_change/role) on memory content | v7.1: intent F1=1.00, state_change F1=1.00, role macro F1=0.79 |
| Authoritative catalog + gazetteer (92 patterns, 567 software_dev entries with Wikidata/SO/Fowler sources) | Pure gazetteer slot F1=0.589, combined with role head = 0.807 |
| Catalog-driven SDG pools (derived via `pool_values()`) | No pool/catalog drift; reproducible generation |
| Domain manifest registry (single source of truth for adapter ↔ corpus ↔ taxonomy) | Adapter ↔ data mismatches caught at import time |
| Bitemporal state nodes + reconciliation scaffolding | L1/L2 creation wired; edge types defined |
| TLG dispatcher + zone walkers (after Fix 2 in v7.2) | 3/10 walkers return HIGH confidence on explicit-shape input; subject extraction works 6/10 |

### 1.2 What failed and why

| Experiment | Failure mode | Root cause |
|-----------|--------------|------------|
| v6 BIO slot tagger | macro slot F1 capped at 0.464; under-predicted non-O tokens | Multiple primary-role spans per memory break BIO chain assumption |
| v7 span-role classifier (pre-fix) | slot F1=0.662 — first-wins slot reconstruction + gold under-labeled alternatives | Reconstruction bug + labeler omission |
| v7.1/v7.2 shape_intent classifier | Training accuracy 1.00 / held-out 0.26 on natural queries | **Task-shape mismatch**: semantic parsing dressed up as 13-class classification |

The shape_intent failure is the critical lesson: **query semantics is compositional, not categorical**. A 13-class classifier over 181 (later 485) templated examples can only memorize prefix scaffolds — the model has no mechanism to compose novel phrasings like "What did we use before Postgres?" (before_named) out of seen examples like "What problem existed before the decision was made in: <ADR>?" (before_named). 

This pattern doesn't replicate for the 5 content heads because memory content **is** surface-feature classifiable (sentiment, vocabulary cluster, trigger phrases), whereas query intent **is not** — tense, reference structure, focus, and modality all matter.

### 1.3 Architectural conclusion

- The SLM encoder + LoRA is the **right substrate** for classifying memory content surface features.
- The 6th head's task was misaligned. Shape_intent needed **semantic parsing**, not classification.
- Cue-tagging (sequence labeling) + compositional synthesis is the **canonical architecture** for this class of task in the NLP literature (PDTB, CATENA, MAVEN-ERE, TORQUE).

---

## 2. CTLG architecture

### 2.1 Contract (end-to-end)

```
[query]
  ↓  SLM encoder + 6th head (shape_cue_head)
  ↓
[sequence of (token, cue_label) pairs]
  ↓  Compositional synthesizer (rules, explainable)
  ↓
[TLGQuery logical form]
  ↓  Dispatcher (existing TLG code, extended)
  ↓
[grammar answer walking zone graph]
  ↓  ∨ BM25 (existing invariant)
  ↓
[retrieval result]
```

### 2.2 The `TLGQuery` form

```python
@dataclass(frozen=True)
class TLGQuery:
    """Structured query produced by the CTLG semantic parser.

    Replaces the flat `shape_intent: Literal[12 + none]` enum with a
    compositional logical form.  The dispatcher reads this directly
    — no more enum → intent map.
    """

    axis: Literal["temporal", "causal", "ordinal", "modal", "state"]
    relation: Literal[
        # temporal axis
        "state_at", "before_named", "after_named", "between",
        "concurrent_with", "during_interval",
        # causal axis
        "cause_of", "effect_of", "chain_cause_of", "trigger_of",
        "contributing_factor",
        # ordinal axis
        "first", "last", "nth",
        # modal axis — counterfactual branch
        "would_be_current_if", "could_have_been",
        # state axis
        "current", "retired", "declared",
    ]
    referent: str | None = None       # named entity anchor ("Postgres")
    subject: str | None = None        # what-changed entity ("auth-service")
    scope: str | None = None          # catalog slot ("database", "framework")
    depth: int = 1                    # 1=direct, ≥2=transitive chain
    scenario: str | None = None       # None=actual history, else branch id
    temporal_anchor: datetime | None = None  # "during 2023", "as of last Q"
    confidence: float = 0.0           # synthesizer's confidence in this form
```

A single enum can't capture queries like "what would have been our current database if we'd stayed with CockroachDB?" — but a compositional form can: `axis=modal, relation=would_be_current_if, referent=CockroachDB, scenario=preserve_crdb_supersession`.

### 2.3 The cue tagger (6th head, v8 shape)

**Model shape:** the same BERT+LoRA encoder. The 6th head changes from `Linear(hidden, 13)` pooled-classification to `Linear(hidden, n_cue_labels)` applied per-token over the full sequence output `(B, L, H)`. **This is exactly the v6 BIO slot_head machinery** — just pointed at cue labels instead of slot labels. No new infrastructure.

**Label vocabulary** (~30 BIO labels, anchored in PDTB 3.0 + AltLex + TLG needs):

```
O                                     # outside any cue
B-CAUSAL_EXPLICIT / I-CAUSAL_EXPLICIT         # "because", "due to", "since" [causal]
B-CAUSAL_ALTLEX   / I-CAUSAL_ALTLEX           # "which led to", "one reason", "the driver"
B-TEMPORAL_BEFORE / I-TEMPORAL_BEFORE         # "before", "prior to", "ahead of"
B-TEMPORAL_AFTER  / I-TEMPORAL_AFTER          # "after", "following", "once"
B-TEMPORAL_DURING / I-TEMPORAL_DURING         # "during", "while", "amid"
B-TEMPORAL_SINCE  / I-TEMPORAL_SINCE          # "since", "as of", "ever since"
B-TEMPORAL_ANCHOR / I-TEMPORAL_ANCHOR         # date/time expressions: "2023", "Q2", "last sprint"
B-ORDINAL_FIRST   / I-ORDINAL_FIRST           # "first", "initial", "earliest"
B-ORDINAL_LAST    / I-ORDINAL_LAST            # "last", "final", "most recent", "latest"
B-ORDINAL_NTH     / I-ORDINAL_NTH             # "second", "third"
B-MODAL_HYPOTHETICAL / I-MODAL_HYPOTHETICAL   # "would have", "could have", "if not for"
B-ASK_CHANGE      / I-ASK_CHANGE              # "what changed", "what happened"
B-ASK_CURRENT     / I-ASK_CURRENT             # "now", "currently", "today", "at present"
B-REFERENT        / I-REFERENT                # catalog entity ("Postgres", "auth-service")
B-SUBJECT         / I-SUBJECT                 # subject whose state evolves
B-SCOPE           / I-SCOPE                   # slot word ("database", "framework", "tool")
```

Sources grounding each family:
- **CAUSAL_EXPLICIT / CAUSAL_ALTLEX** → PDTB 3.0 connective lexicon, Hidey & McKeown's AltLex
- **TEMPORAL_\*** → TempEval 3 / ISO-TimeML guidelines, TimeBank-EVENT
- **ORDINAL_\*** → simple lexical class — shipped with a hand-curated list per surface form
- **MODAL_HYPOTHETICAL** → SemEval 2020 Task 5 counterfactual detection
- **ASK_CHANGE / ASK_CURRENT** → TORQUE temporal QA
- **REFERENT / SUBJECT / SCOPE** → gazetteer + role-head primary signal (bootstrap from existing v7.2 outputs)

**Why this vocabulary works where 13 flat classes didn't:**

| 13-class shape_intent | CTLG cue tagger |
|----------------------|-----------------|
| Semantically overlapping classes (origin ↔ ordinal_first) | Compositional — overlapping cues compose into different TLGQuery forms |
| ~25 training examples per class | ~50 training examples per cue tag, but cues appear in **most** training queries → every row has ~3-6 labeled cues → effective density is much higher |
| No mechanism to generalize to unseen phrasings | Cue patterns + rules compose to unseen queries as long as their cues were in training |
| Opaque failures | Per-cue F1 + synthesizer hit rate localize failures to a specific cue or rule |

### 2.4 The compositional synthesizer

A ~150-line pure-function rule engine at `src/ncms/domain/tlg/semantic_parser.py`:

```python
def synthesize(tagged: list[TaggedToken]) -> TLGQuery | None:
    """Compose tagged cues → TLGQuery.

    Returns None when no rule matches — caller falls back to LLM.
    """
    spans = _group_bio_spans(tagged)   # [(start, end, tag, text), ...]
    cues = _index_by_family(spans)     # {"causal": [...], "temporal": [...], ...}

    # === TEMPORAL axis rules ===
    if cues["temporal_before"] and cues["referent"]:
        return TLGQuery(
            axis="temporal", relation="before_named",
            referent=cues["referent"][0].text,
            subject=cues["subject"][0].text if cues["subject"] else None,
            confidence=0.9,
        )
    if cues["ask_current"] and cues["scope"]:
        return TLGQuery(
            axis="state", relation="current",
            scope=cues["scope"][0].text,
            subject=cues["subject"][0].text if cues["subject"] else None,
            confidence=0.95,
        )
    # ... more rules

    # === CAUSAL axis rules ===
    if cues["causal_explicit"] or cues["causal_altlex"]:
        depth = 2 if cues["causal_altlex"] and "chain" in [t.text.lower() for t in spans] else 1
        return TLGQuery(
            axis="causal",
            relation="chain_cause_of" if depth > 1 else "cause_of",
            referent=cues["referent"][0].text if cues["referent"] else None,
            depth=depth,
            confidence=0.85,
        )

    # === MODAL / COUNTERFACTUAL axis ===
    if cues["modal_hypothetical"]:
        return TLGQuery(
            axis="modal", relation="would_be_current_if",
            referent=cues["referent"][0].text if cues["referent"] else None,
            scenario=_infer_scenario_from_referent(cues),
            confidence=0.75,
        )

    # ... ordinal axis rules, etc.

    return None  # no rule → LLM fallback
```

**Properties:**
- **Deterministic**: same cue pattern → same TLGQuery. Reproducible failures.
- **Explainable**: emits which rule fired. Every query has a trace.
- **Testable**: pure-function unit tests, one per rule.
- **Incrementally extensible**: new rule added when a known-failing pattern emerges.

### 2.5 LLM fallback (safety net + training data collector)

When `synthesize()` returns `None`, dispatcher invokes an LLM call with a structured prompt:

```python
async def llm_fallback_parse(query: str, tagged: list[TaggedToken]) -> TLGQuery | None:
    prompt = build_llm_parse_prompt(query, tagged)
    result = await call_llm_json(prompt, ...)
    return _validate_and_construct_tlg_query(result)
```

The prompt includes the cue tags the head already produced — the LLM doesn't re-tokenize; it sees the cue structure and fills in the composition. Lightweight call, ~200ms on Spark.

**Training-data capture loop:**
- Every LLM fallback fires a `CueCompositionMiss` event
- Events accumulate in a dedicated log (`tlg_composition_misses` table)
- Periodically, the log is reviewed:
  - Accepted patterns become new synthesizer rules
  - OR used as fresh training examples to push into v8.1
- Over time, the LLM fallback rate drops toward zero as rules cover real traffic

This is the **self-improving loop** — same structural idea as the self-evolving catalog (below).

---

## 3. Self-evolving taxonomy

### 3.1 Current state (v7.2)

The authoritative catalog (`src/ncms/application/adapters/sdg/catalog/`) holds 567 software_dev surface forms across 9 slots, sourced from Wikidata / Wikipedia / SO tags / GitHub Topics / Azure Cloud Design Patterns / Martin Fowler / NIST. The gazetteer (`detect_spans`) is the first and preferred detection path.

### 3.2 The missing loop

When the role head encounters a memory like "We deployed to Render and Fly.io last week", if `fly.io` isn't in the catalog, we currently:
1. Gazetteer finds nothing (miss)
2. GLiNER fallback fires (slower, less accurate)
3. No record of the novel surface — next time we see `fly.io`, same miss

### 3.3 The self-evolving catalog

```
    [memory ingest]
         ↓
    [gazetteer pass]
         ↓
    [role head + slots dict produced]
         ↓
    ┌─── SLM has high-confidence typed entity not in catalog? ────┐
    │                         or                                    │
    │ SLM confidence low + GLiNER produced a typed entity?          │
    │                         or                                    │
    │ Synthesizer LLM fallback included a REFERENT not in catalog?  │
    └──────────────────────────────────────────────────────────────┘
         ↓ YES
    [novel_surface_event]
      {surface, proposed_slot, proposed_canonical, context_snippet,
       source, confidence, observed_at}
         ↓
    [novel_surfaces table]
         ↓  periodic batch
    [LLM classifier: "given these N novel surfaces, for each one confirm
     slot, canonical form, aliases, and authoritative source"]
         ↓
    [auto-merge if confidence ≥ threshold AND source identifiable]
    [else → review queue]
         ↓
    [catalog/<domain>.py patch + git commit with citation]
         ↓  next SDG regen picks up the expansion; next v8.x retrain includes it
```

### 3.4 Concrete schema + pipeline

New table: `novel_surfaces`

```sql
CREATE TABLE novel_surfaces (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    surface TEXT NOT NULL,
    surface_lower TEXT NOT NULL,
    proposed_slot TEXT,
    proposed_canonical TEXT,
    context_snippet TEXT,
    source_memory_id TEXT,
    discovery_source TEXT,  -- "role_head_unknown" | "gliner_fallback" | "llm_synth_fallback"
    discovery_confidence REAL,
    n_observations INTEGER DEFAULT 1,
    first_observed_at TEXT,
    last_observed_at TEXT,
    status TEXT DEFAULT 'pending',  -- pending | auto_accepted | review | rejected
    merged_into_catalog_version TEXT
);
CREATE INDEX ix_novel_surfaces_status_domain ON novel_surfaces(status, domain);
```

New CLI:
```bash
ncms catalog review --domain software_dev      # interactive review queue
ncms catalog auto-merge --domain software_dev  # merge all status=auto_accepted
ncms catalog suggest --domain software_dev     # LLM classifier pass over status=pending
```

New event on the observability bus: `catalog.novel_surface_observed` / `catalog.auto_merged` / `catalog.review_pending`. Dashboard renders a "novel surfaces" tile.

### 3.5 Domain-agnostic taxonomy maintenance

Same pipeline works for clinical (ICD-10, SNOMED CT anchoring), conversational (universal persona facets), swe_diff (GitHub API references). Each domain's LLM classifier prompt cites its authoritative source.

---

## 4. Zone graph evolution

### 4.1 Current zone graph

| Layer | Node type | Edges out |
|-------|-----------|-----------|
| L1 | atomic (one per memory) | none |
| L2 | entity_state | DERIVED_FROM→L1, SUPERSEDES↔L2, REFINES→L2, CONFLICTS↔L2, SUPPORTS→L2 |
| L3 | episode | MEMBER_OF←L1 |
| L4 | abstract (insight / strategic_insight) | DERIVED_FROM→L1/L2/L3 |

### 4.2 New edge type: `CAUSED_BY`

Causal reasoning in TLG currently piggybacks on supersession — "what caused current state Y" is answered as "what Y superseded". That's a subset of causal semantics; it misses:
- Lateral causation: "Postgres migration caused us to hire DBAs" (two unrelated L2 nodes)
- External causation: "compliance audit caused Fortify scan adoption" (L2 from non-memory trigger)
- Enabling conditions: "availability of pgvector enabled Postgres decision"

Adding a typed `CAUSED_BY` edge type with directional semantics `(effect_node) CAUSED_BY (cause_node)` handles these cleanly. The edge metadata records the **causal cue** that triggered edge creation ("due to", "as a result") for provenance.

### 4.3 Ingest-time causal cue tagger

The **same 6th head** tags cues on both query-voice queries AND memory-voice content. At ingest, the cue tagger runs on each memory; any span tagged CAUSAL_EXPLICIT / CAUSAL_ALTLEX along with flanking REFERENT spans triggers `CAUSED_BY` edge creation between the corresponding L2 nodes.

```python
# In ingestion pipeline (Fix 2 extension)
cue_tags = await self._slm.extract_cues(memory.content)
causal_pairs = extract_causal_pairs(cue_tags)  # [(cause_referent, effect_referent), ...]
for cause, effect in causal_pairs:
    cause_node = await self._resolve_entity_state(cause)
    effect_node = await self._resolve_entity_state(effect)
    if cause_node and effect_node:
        await self._store.save_graph_edge(GraphEdge(
            source_id=effect_node.id,
            target_id=cause_node.id,
            edge_type=EdgeType.CAUSED_BY,
            metadata={
                "cue_span": ...,
                "cue_type": ...,
                "confidence": ...,
            },
        ))
```

### 4.4 Counterfactual scenario traversal

Counterfactual queries like `axis=modal, relation=would_be_current_if, scenario="preserve_crdb"` dispatch through a walker that:
1. Clones the supersedes chain from the subject's current state backwards
2. Skips the edge(s) specified by `scenario` (e.g. the CRDB → YugabyteDB supersession)
3. Returns the state that would still be current given the skip

No new infrastructure — pure graph traversal with a skip-filter. The existing `is_current` flag flips via a projected view, not by mutating stored state.

### 4.5 Impact on existing code

- `domain/models.py` — add `EdgeType.CAUSED_BY` and `EdgeType.ENABLES` (enabling conditions)
- `domain/tlg/dispatch.py` — add walkers for the new axis/relation combinations, including modal/counterfactual
- `application/ingestion/pipeline.py` — causal edge creation step after L2 creation
- `infrastructure/storage/migrations.py` — no schema change (edges are already typed strings)
- `infrastructure/storage/sqlite_store.py` — ensure `list_graph_edges_by_type("caused_by")` works

---

## 5. Training-data plan

### 5.1 Query-voice cue gold (for the 6th head, query side)

**Target**: ~3000 rows across 12 TLG shape families, each tagged with per-token BIO cue labels.

**Generation**:
- Start from the existing 485 gold shape_intent queries — LLM-tag each with cue labels
- Spark Nemotron prompt: "Given this query, tag each token with BIO cue labels from the following list: ..." Output is `{"text": "...", "tokens": [...], "cue_tags": [...]}`
- Generate another ~2000 queries across underrepresented cue combinations (especially causal and modal)
- Manual review pass — sample 200 for precision verification

**Stored at**: `adapters/corpora/gold_cue_tagging_software_dev.jsonl`
**Schema**: one object per row with `text`, `tokens`, `cue_tags`, `domain`, `split`, `source`, `note`

### 5.2 Memory-voice cue gold (for the 6th head, ingest side)

**Target**: ~2000 memories tagged with per-token BIO cue labels, specifically flagging causal and temporal cues that would populate CAUSED_BY edges.

**Generation**:
- Seed from existing 186 gold_software_dev rows
- LLM-tag causal + temporal cues
- Include narratives with explicit causation ("because", "led to", "caused", "resulting in")
- Include "enabling" narratives ("now that X exists, we can Y")

### 5.3 Counterfactual examples

**Target**: ~300 counterfactual queries.

**Pattern**: `"What would we be using if we hadn't switched to X?"` / `"Had Y been rejected, what would be current?"` / `"If not for the audit, would we still use Z?"`

### 5.4 Self-evolving catalog seeds

**Target**: ~300 novel-surface examples from live-ingest data (from existing NCMS corpora) for which the role head has already tagged slot + canonical. These bootstrap the auto-merge threshold calibration.

### 5.5 Volume summary

| Dataset | Rows | Generation cost |
|---------|------|-----------------|
| Query-voice cue gold | 3000 | ~30min Spark |
| Memory-voice cue gold | 2000 | ~30min Spark |
| Counterfactual queries | 300 | ~10min Spark |
| Novel-surface seeds | 300 | extracted from ingest logs |
| **Total new gold** | **5600** | **~80min Spark** |

---

## 6. Cleanup + archival

### 6.1 What to archive (move to `adapters/_archive/pre_ctlg/`)

- `adapters/corpora/gold_shape_intent_software_dev.jsonl.pre_v7.2.bak` → already renamed, move to archive
- `adapters/checkpoints/software_dev/v7_initial/` → already preserved, move to archive
- Old SDG retirement templates (pre-corrected-direction) — snapshot as `templates_v7.1.snapshot.py`

### 6.2 What to delete outright

Nothing yet — we don't delete code paths we might still learn from. Preserve the v6 BIO slot_head pattern (it's the basis of the cue-tagger head). Preserve the v7 role_head (it's still active on the ingest side).

### 6.3 What to rename / relabel

- `shape_intent` → `shape_cue` in all new code
- `manifest.json` keeps `shape_intent_labels` as a deprecated field during transition (v8 writes both `shape_intent_labels=[]` and `shape_cue_labels=[...]`)
- `ExtractedLabel.shape_intent` → `ExtractedLabel.cue_tags: tuple[TaggedToken, ...]`
- Keep `ExtractedLabel.shape_intent` as a `@property` computed from cue_tags + synthesizer for one release cycle, with a deprecation warning

### 6.4 Docs to update

| Doc | Update |
|-----|--------|
| `CLAUDE.md` | Replace head-6 description; note CTLG pivot |
| `docs/slm-entity-extraction-deep-audit.md` | Append section 13 — shape_intent retrospective |
| `docs/mseb-results.md` §5 | Note: v7.1/v7.2 shape_intent results are superseded |
| `docs/forensics/v7.1-tlg-forensics.md` | Keep as-is — it's the failure artifact that motivated CTLG |
| `docs/research/ctlg-design.md` | THIS doc |
| `docs/completed/failed-experiments/shape-intent-classification.md` | NEW — retrospective |

### 6.5 Schema changes

```python
# src/ncms/application/adapters/schemas.py

# Keep Role / RoleSpan / DetectedSpan unchanged — they're still active

# NEW:
CueLabel = Literal[
    "O",
    "B-CAUSAL_EXPLICIT", "I-CAUSAL_EXPLICIT",
    # ... full list from §2.3
]

@dataclass(frozen=True)
class TaggedToken:
    token_start: int       # character offset start
    token_end: int         # character offset end
    surface: str
    cue_label: CueLabel    # BIO-tagged
    confidence: float

# DEPRECATED (kept for read-path back-compat until v9):
ShapeIntent = Literal[...]  # annotate with @deprecated

# ExtractedLabel gets `cue_tags` field (tuple), `shape_intent` becomes @property
```

### 6.6 Config flag changes

Before: `NCMS_SLM_ENABLED` + `NCMS_TEMPORAL_ENABLED` (two masters)
After: **unchanged**. CTLG lives under `NCMS_TEMPORAL_ENABLED`. Sub-knobs:
- `NCMS_TLG_LLM_FALLBACK_ENABLED` (default False initially, flip after v8 stable)
- `NCMS_TLG_LLM_FALLBACK_MODEL` / `NCMS_TLG_LLM_FALLBACK_API_BASE`
- `NCMS_CATALOG_AUTOMERGE_ENABLED` (default False — always review first)
- `NCMS_CATALOG_AUTOMERGE_CONFIDENCE_THRESHOLD=0.9`

---

## 7. Benchmarks

### 7.1 CTLG-specific suite (NEW)

**Metric stack per domain:**

| Metric | Definition | Target (v8 initial) | Aspirational |
|--------|------------|---------------------|--------------|
| Cue-tagging F1 (per cue family) | Standard BIO F1 on held-out labeled queries | ≥ 0.80 | ≥ 0.90 |
| Synthesizer hit rate | Fraction of held-out queries where synthesize() returns non-None | ≥ 0.65 | ≥ 0.85 |
| Synthesizer accuracy | Fraction of hits whose TLGQuery matches gold | ≥ 0.85 | ≥ 0.95 |
| LLM fallback acceptance | Fraction of LLM fallbacks that produce a valid TLGQuery | ≥ 0.70 | ≥ 0.90 |
| End-to-end TLG dispatch accuracy | Is the dispatcher's answer correct given a gold narrative + gold query? | ≥ 0.60 | ≥ 0.85 |
| Counterfactual dispatch accuracy | Same, specifically on counterfactual queries | ≥ 0.50 | ≥ 0.80 |

**Held-out set design:**
- Queries generated by a DIFFERENT LLM / different prompting than the training generator (adversarial separation)
- Explicitly include paraphrases of training queries — test that the model treats them as the same TLGQuery
- Include cross-shape hard negatives (same surface words, different cue structure)

### 7.2 Existing retrieval benchmarks — unchanged but revalidated

| Benchmark | Relevance to CTLG |
|-----------|-------------------|
| MSEB softwaredev / clinical / convo / swe_diff | r@1 / nDCG@10 on state-evolution queries — a CTLG regression should show up here |
| LongMemEval | conversational r@1 — tests 5-head SLM ingest-side quality |
| SciFact / NFCorpus / ArguAna ablations | hybrid retrieval sanity — CTLG layer should be feature-flag-off by default; flipping on should not regress these |

**Regression invariant:** with `NCMS_TEMPORAL_ENABLED=false`, CTLG code paths short-circuit. The existing benchmarks MUST not regress relative to the pre-CTLG baseline.

### 7.3 Self-evolving catalog benchmark (NEW)

Measure whether the catalog-learning loop actually reduces the LLM fallback rate over time:

```
Phase 1 (day 0):       novel_surface rate per 1000 ingested memories
Phase 2 (after 10K):   novel_surface rate per 1000 ingested memories
Phase 3 (after 100K):  novel_surface rate per 1000 ingested memories
```

Target: ≥30% reduction in novel_surface rate between phase 1 and phase 3 on realistic (enterprise) ingest streams.

### 7.4 Benchmark harness organization

```
benchmarks/
├── ctlg/                           # NEW
│   ├── cue_tagging.py              # cue F1 eval
│   ├── synthesizer.py              # rule hit rate + accuracy
│   ├── end_to_end.py               # narrative → query → answer eval
│   ├── counterfactual.py           # counterfactual suite
│   └── gold/
│       ├── cue_tagging_held_out.jsonl
│       ├── counterfactual_held_out.jsonl
│       └── end_to_end_narratives.jsonl
├── mseb/                           # EXISTING — re-run post-v8
├── intent_slot_adapter.py          # EXISTING — path resolver
└── ...
```

---

## 8. Phased roadmap

### Phase 0 — v7.2 completion + cleanup (this sprint)

**Deliverables:**
- [ ] Let v7.2 finish; capture its gate metrics as the pre-pivot baseline
- [ ] Write retrospective: `docs/completed/failed-experiments/shape-intent-classification.md`
- [ ] Archive old shape_intent corpus + v7_initial checkpoint
- [ ] Update `CLAUDE.md` with pivot note
- [ ] Commit this design doc

**Exit criterion:** all existing tests still pass; v7.2 metrics recorded; pivot is documented.

### Phase 1 — Cue label set finalization (week 1)

**Deliverables:**
- [ ] Write the cue-labeling guidelines doc: `docs/research/ctlg-cue-guidelines.md`
- [ ] Hand-label ~50 example queries per cue family (pilot gold)
- [ ] LLM-tag the existing 485 shape_intent queries → `gold_cue_tagging_software_dev_llm.jsonl`
- [ ] Human review pass on 100 LLM-tagged rows; calibrate prompt

**Exit criterion:** pilot gold is inter-annotator-agreement-clean (Cohen's κ ≥ 0.8 on hand-labeled subset).

### Phase 2 — Training data generation (week 1-2)

**Deliverables:**
- [ ] Generate 3000 query-voice cue-tagged rows via Spark
- [ ] Generate 2000 memory-voice cue-tagged rows via Spark
- [ ] Generate 300 counterfactual queries
- [ ] Spot-check + regeneration on low-quality shards

**Exit criterion:** 5.3K labeled rows, balanced across cue families.

### Phase 3 — Model changes (week 2)

**Deliverables:**
- [ ] Add `shape_cue_head` to `LoraJointModel` (alongside — not replacing — v7.x `role_head`)
- [ ] Update `AdapterManifest` with `cue_labels` field
- [ ] Update training loop: per-token CE loss for cue tagging
- [ ] Update inference: produce `cue_tags: tuple[TaggedToken, ...]`
- [ ] Wire deprecation shim: `shape_intent` computed from `cue_tags + synthesize()`

**Exit criterion:** v8 training kicks off; model saves + loads correctly.

### Phase 4 — Synthesizer implementation (week 2-3)

**Deliverables:**
- [ ] `src/ncms/domain/tlg/semantic_parser.py` — ~12 rules, covers 80%+ of target shapes
- [ ] Unit tests per rule
- [ ] LLM fallback wired
- [ ] `tlg_composition_misses` table + event log

**Exit criterion:** synthesizer hit rate ≥ 0.65 on pilot held-out.

### Phase 5 — Zone graph evolution (week 3)

**Deliverables:**
- [ ] Add `EdgeType.CAUSED_BY` + `EdgeType.ENABLES`
- [ ] Ingest-time causal extraction step
- [ ] Counterfactual walker in dispatcher
- [ ] Regression tests

**Exit criterion:** existing zone-graph tests pass; new causal edges land on curated narratives.

### Phase 6 — Self-evolving catalog loop (week 3-4)

**Deliverables:**
- [ ] `novel_surfaces` table + migration
- [ ] Novel-surface capture at ingest
- [ ] LLM classifier batch job
- [ ] `ncms catalog review / auto-merge / suggest` CLI
- [ ] Dashboard tile

**Exit criterion:** end-to-end dry run: surface not in catalog → observed → classified → merged.

### Phase 7 — Train v8, forensics, benchmarks (week 4-5)

**Deliverables:**
- [ ] Train v8 software_dev adapter (cue head + all other heads)
- [ ] Re-run CTLG benchmark suite
- [ ] Re-run MSEB + LongMemEval regressions
- [ ] Update `docs/forensics/v8-ctlg-forensics.md`
- [ ] Ship docs + dashboard updates

**Exit criterion:** CTLG targets met (§7.1); no MSEB regression (§7.2).

### Phase 8 — Roll out to remaining domains (week 5+)

**Deliverables:**
- [ ] Replicate cue-labeling pipeline for clinical / conversational / swe_diff
- [ ] Catalogs expanded per domain (continue self-evolving loop on live traffic)
- [ ] Deploy v8 adapters to `~/.ncms/adapters/`
- [ ] Enable `NCMS_TLG_LLM_FALLBACK_ENABLED=true` as default once hit rate stabilizes

---

## 9. Risks + mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM-generated cue tags are systematically biased (e.g. overtagging CAUSAL_EXPLICIT) | **HIGH** | Human review 10% sample per batch; inter-annotator check on 200-row pilot; adversarial prompt variation |
| Synthesizer rules don't compose to real traffic | **MEDIUM** | LLM fallback is the safety net; hit rate target is 65% (not 95%); rules grow incrementally |
| Counterfactual traversal semantics ambiguous (which edge to skip?) | **MEDIUM** | Start with single-skip queries only; multi-skip is Phase 9+ |
| Catalog auto-merge introduces bad entries | **MEDIUM** | `NCMS_CATALOG_AUTOMERGE_ENABLED=False` default; always review initially; threshold 0.9 when turned on |
| v8 training regresses on 5 content heads because of new loss term | **LOW** | Cue loss weighted 0.3 initially; monitor intent/role F1 on gate; rollback hatch via manifest versioning |
| CAUSED_BY edges pollute zone graph with spurious cue matches | **MEDIUM** | Confidence threshold ≥ 0.7 at edge creation; provenance metadata on every edge; audit tooling |
| Dashboard event volume explodes with novel_surface_observed events | **LOW** | Batch emit + dedupe by (domain, surface_lower) |

---

## 10. Open questions (pre-kickoff)

1. **Cue label granularity** — do we need two flavors of causal (proximate vs transitive)? Or handle via synthesizer `depth` field? Lean toward (b) for smaller label set.
2. **Scope cue** — should "database" / "framework" be a `SCOPE` cue or pulled from the role head's slot? Lean toward reusing role head output → cleaner separation of concerns.
3. **Counterfactual serialization** — how does `scenario` identify which edge to skip? Proposal: `scenario = "skip_supersedes:<edge_id>"`. Feedback invited.
4. **Back-compat window** — keep `ExtractedLabel.shape_intent` @property for how long? Proposal: one minor version (through v8.x), removed in v9.
5. **Training-data review volume** — can we get away with reviewing 5% of LLM-generated cue gold, or do we need 20%? Start 20% for pilot, relax once inter-annotator agreement is stable.

---

## 11. Why this pivot is worth it

The v7.1 → v7.2 evolution showed us the ingest-side architecture is **solid**: catalog + role head + reconciliation pipeline scale gracefully, and each fix is bounded. The query-side `shape_intent` classifier was a **structural mismatch** — no amount of training data was going to close the gap between "semantic parsing" and "25-example-per-class classification".

CTLG reframes the query side as **sequence labeling + compositional synthesis**, which is the canonical architecture for this family of tasks in the discourse-parsing literature. It reuses the existing SLM encoder + LoRA machinery (v6 had exactly this structure for slot tagging — we're just giving it the right task), adds ~30 new labels, adds ~150 lines of rule synthesizer, and gains:
- **Generalization** — cue patterns compose to unseen phrasings
- **Explainability** — every TLGQuery has a trace back to cue tags + matched rule
- **Counterfactual reasoning** — first-class axis, falls out of the query form
- **Self-improving** — LLM fallback + review loop feeds v8.1/v8.2 naturally
- **Correct task shape** — the 6th head finally does what the other 5 do: SLM work that matches the SLM's capabilities

And critically, the pivot is **incremental**. The 5 content heads are unchanged. The ingest pipeline is extended, not rewritten. The zone graph gains an edge type, not a layer. The dispatcher gets new walkers, not a new dispatcher. Every phase produces a shippable artifact.

## Appendix A — Bibliography

1. Prasad et al. 2019 — *The Penn Discourse Treebank 3.0 Annotation Manual*
2. Hidey & McKeown 2016 — *Identifying Causal Relations Using Parallel Wikipedia Articles* (AltLex)
3. Mirza & Tonelli 2016 — *CATENA: CAusal and TEmporal relation extraction from NAtural language texts*
4. Wang et al. 2022 — *MAVEN-ERE: A Unified Large-scale Dataset for Event Coreference, Temporal, Causal, and Subevent Relation Extraction*
5. Ning et al. 2020 — *TORQUE: A Reading Comprehension Dataset of Temporal Ordering Questions*
6. Veitch, Sridhar, Blei 2020 — *Adapting Text Embeddings for Causal Inference*
7. SemEval 2020 Task 5 — *Modelling Causal Reasoning in Language: Detecting Counterfactuals*
8. DARPA KAIROS Program — *Knowledge-directed Artificial Intelligence Reasoning Over Schemas*
9. Jin et al. 2024 — *CLadder: Assessing Causal Reasoning in Language Models*
10. Gottschalk & Demidova 2018 — *EventKG: A Multilingual Event-Centric Temporal Knowledge Graph*

## Appendix B — Glossary

- **CTLG** — Causal-Temporal Linguistic Geometry (this project)
- **TLG** — Temporal Linguistic Geometry (existing NCMS dispatcher + grammar composition layer)
- **Cue** — a typed linguistic signal in text (causal / temporal / ordinal / modal / referent / subject / scope)
- **TLGQuery** — structured logical form produced by the semantic parser
- **Scenario** — counterfactual branch identifier on a TLGQuery (None = actual history)
- **Zone** — a coherent subgraph in the HTMG (L1/L2/L3/L4 nodes + edges)
- **CAUSED_BY** — new edge type linking an effect node to its cause node, with cue provenance metadata
- **Novel surface** — a text surface form observed during ingest that's absent from the authoritative catalog
- **Self-evolving catalog** — the loop by which novel surfaces get LLM-classified and merged back into the catalog
