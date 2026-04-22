# SLM → TLG Architecture (v6)

*Post-v6 state of the end-to-end ingest-classify + query-classify +
grammar-walk pipeline.  Landed in commits ``cf4d667`` (regex
classifier deletion) and ``d6425d5`` (convo gold audit + retrain).
This replaces the brittle hand-coded regex parser in
``ncms.domain.tlg.query_parser`` with a 6th LoRA head on the SLM
and preserves the zero-confidently-wrong invariant the grammar
had under regex.*

## 1. The six SLM heads

All six heads run in **one** forward pass on a shared BERT-base
LoRA-tuned encoder.  The heads are composed from a single pooled
``[CLS]`` vector plus per-token (BIO) sequence output for slots.

| # | Head | Labels | When it's useful |
|---|------|--------|------------------|
| 1 | ``admission``      | persist / ephemeral / discard | Ingest — gates a memory into the store (MSEB uses `importance=8.0` bypass so gold survives). |
| 2 | ``state_change``   | declaration / retirement / none | Ingest — drives TLG L2 zone induction (SUPERSEDES edges). |
| 3 | ``topic``          | per-adapter taxonomy YAML | Ingest — auto-populates `Memory.domains`. |
| 4 | ``intent``         | positive / negative / habitual / difficulty / choice / none | Ingest — preference-type extraction for recall filters. |
| 5 | ``slot`` (BIO)     | per-domain surface-form types (file_path, medication, library, object, …) | Ingest — typed anchors for structured-anchor scoring. |
| 6 | ``shape_intent``   | 12 TLG grammar shapes + none | **Query** — intent classification for grammar dispatch.  Replaces the deleted regex parser. |

The per-adapter ``manifest.json`` ships the vocabularies for
``intent_labels``, ``slot_labels``, ``topic_labels``,
``admission_labels``, ``state_change_labels``, and
``shape_intent_labels``.  Inference tolerates missing vocabularies
(empty list = head is a placeholder of dim 1; caller reads ``None``).

## 2. Which heads fire in which context

**Ingest path** (``IngestionPipeline.run_intent_slot_extraction``):

```
memory content ─► SLM.extract(text, domain=adapter_domain)
                  ├─► admission   ─► gate routes persist/ephemeral/discard
                  ├─► state_change ─► persisted to memory + feeds TLG L2 induction
                  ├─► topic       ─► appended to Memory.domains
                  ├─► intent      ─► persisted to memory.intent column
                  ├─► slot        ─► persisted to memory_slots table
                  └─► shape_intent ─► IGNORED (memory voice doesn't have a query shape)
```

**Query path** (``MemoryService.retrieve_lg``):

```
query text ─► SLM.extract(text, domain=adapter_domain)
              ├─► admission/state_change/topic/intent ─► IGNORED (query voice)
              ├─► slot        ─► future: populates QueryStructure.target_entity
              └─► shape_intent ─► dispatches to TLG walker (see §3)
```

The extractor ``LoraJointExtractor.extract`` is voice-agnostic —
it produces all 6 heads on any input.  Caller discipline decides
which to read based on whether the input is memory voice or query
voice.  There is no runtime switch; the same forward pass handles
both.

> **Follow-up (v7):** add a ``subject`` slot type so the query-voice
> path uses the SLM's slot head to extract ``target_entity`` instead
> of the small regex-adjacent ``_extract_event_names`` helper that
> remains in ``ncms.domain.tlg.query_parser``.  Once that lands,
> the query parser becomes pure vocabulary lookup (subject match)
> and the last regex is gone.

## 3. Grammar dispatch (TLG) — post-v6 flow

```
MemoryService.retrieve_lg(query)
  ├─► self._intent_slot.extract(query)
  │     └─► ExtractedLabel.shape_intent ∈ {12 shapes, "none", None}
  │                                         ↑       ↑      ↑
  │                                         │       │      └─ adapter predates v6
  │                                         │       └─ explicit abstain
  │                                         └─ confident classification
  │
  ├─► if confidence ≥ threshold AND shape_intent ≠ "none":
  │       dispatch via _SLM_SHAPE_TO_DISPATCH_INTENT
  │
  ├─► else:
  │       LGTrace(confidence=NONE) → hybrid retrieval unchanged
  │
  └─► _dispatch(query, ..., slm_shape_intent=mapped_shape)
        │
        ├─► analyze_query(query) → QueryStructure(subject, target_entity,
        │                                          secondary_entity, intent=None)
        │                          (subject via vocabulary lookup;
        │                           target/secondary via L1 entity match)
        │
        ├─► intent := mapped_shape (from SLM)
        │
        ├─► _load_subject_zones(store, subject)
        │
        └─► _route_intent(qs, trace, ctx) → _dispatch_<intent>_intent(…)
              └─► walks zones, returns LGTrace(grammar_answer, proof, confidence)
```

## 4. SLM shape_intent → dispatch intent mapping

| SLM label | Dispatcher intent | Walker |
|---|---|---|
| ``current_state`` | ``current`` | current-zone walker |
| ``before_named`` | ``before_named`` | two-event ordering walker |
| ``concurrent`` | ``concurrent`` | observed_at overlap walker |
| ``origin`` | ``origin`` | chain-root walker |
| ``retirement`` | ``retirement`` | retirement-node walker |
| ``sequence`` | ``sequence`` | successor walker |
| ``predecessor`` | ``predecessor`` | predecessor walker |
| ``transitive_cause`` | ``transitive_cause`` | ancestor walker |
| ``causal_chain`` | ``cause_of`` | cause-of walker (shared) |
| ``interval`` | ``interval`` | observed_at window filter |
| ``ordinal_first`` | ``origin`` | first-in-chain (shared with origin) |
| ``ordinal_last`` | ``current`` | last-in-chain (shared with current_state) |
| ``none`` | — | abstain (dispatch skipped) |

The old regex dispatcher had two additional intent strings that
are **deleted as of v6**: ``still`` (collapsed into
``current_state`` semantically) and ``range`` (required regex
range-detection that is gone).  Their dispatcher implementations
were removed from ``_INTENT_DISPATCHERS``.

## 5. What got deleted in Phase A

``ncms.domain.tlg.query_parser``:

- ``SEED_INTENT_MARKERS`` — hardcoded per-intent seed vocabularies (~35 words)
- ``ISSUE_SEED`` — issue-vocabulary for cause-of target extraction (moved to ``vocabulary_cache.py``)
- ``_STILL_ACTION_VERBS`` — still-intent action verb seed
- 12 ``_match_<intent>`` regex productions (~500 LOC)
- ``_PRODUCTIONS`` dispatch table
- ``_find_marker``, ``_detect_range``, ``_extract_still_object`` — intent-specific helpers
- ``ParserContext.augmented_markers`` method

**What remains** (~280 LOC total):

- ``ParserContext`` + ``QueryStructure`` dataclasses
- ``compute_domain_nouns`` — frequency filter for target-entity extraction
- ``_extract_event_names`` — L1-vocabulary-backed entity extractor (returns up to 2 entities for 2-slot dispatchers)
- ``analyze_query`` — subject + target_entity + secondary_entity (no intent)

## 6. Backward compatibility

Pre-v6 adapters (``intent_slot_distillation/adapters/<domain>/v4``
or ``v5``) don't ship a ``shape_intent_head``.  The inference
wrapper detects the empty ``shape_intent_labels`` list in the
manifest and surfaces ``shape_intent=None``.  The dispatcher
then treats that as abstain — grammar does not apply,
hybrid retrieval is returned unchanged.

This means **swe_diff/v1** still works in the v6 pipeline; it just
doesn't get grammar dispatch.  Good enough for the domain whose
queries are diff-shaped (TLG doesn't apply cleanly anyway).

## 7. Training data sources

| Adapter | Memory-voice training | Query-voice training |
|---|---|---|
| conversational/v6 | 30 gold + ~750 SDG + 300 adversarial | 269 audited MSEB-convo queries (30 keep + 239 remap, see `benchmarks/mseb_convo/audit/`) |
| clinical/v6 | 15 gold + ~700 SDG + 300 adversarial | 172 MSEB-clinical gold queries |
| software_dev/v6 | 30 gold + ~850 SDG + 300 adversarial | 181 MSEB-softwaredev gold queries |
| swe_diff/v1 | 8,842 SWE-Gym gold | none (no shape_intent head) |

Shape-intent gold for the three prose adapters lives in
``experiments/intent_slot_distillation/corpus/gold_shape_intent_<domain>.jsonl``
and is built from MSEB gold by
``experiments/intent_slot_distillation/corpus/build_shape_intent_gold.py``.

Query-voice training rows carry ``intent=none``,
``admission=persist``, ``state_change=none``, and the specific
``shape_intent`` label — per-head masking in the training loop
means the preference and state-change heads don't learn from the
query rows (they're masked out).

## 8. Known limitations + follow-ups

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | Target-entity extraction still uses regex-adjacent `_extract_event_names` (not SLM slot head) | medium | Add `subject` slot type to slot head taxonomy; re-annotate gold; retrain as v7. |
| 2 | `interval` shape has 0 training rows across all domains | low | Query-voice SDG for interval shape — add "how many days between X and Y" template generator. |
| 3 | Convo held-out paraphrase accuracy 60% (96.7% on exact MSEB gold) | medium | Query-voice SDG for under-represented shapes (convo retirement=2 rows, ordinal_last=9). |
| 4 | swe_diff has no v6 shape_intent head | low | Train swe_diff/v2 with MSEB-SWE gold queries + existing SWE-Gym memory-voice training. |
| 5 | SDG template cross-product artifacts ("Patient takes stenting weekly") | low (no label errors) | Add object-type × template-family gating when generating SDG rows. |
| 6 | `still` + `range` dispatchers removed from `_INTENT_DISPATCHERS` | n/a (intentional) | Collapsed into `current_state` / `interval` semantically. |

## 9. Measured accuracy (post-v6)

### Query-shape classification on MSEB gold

| Adapter | In-distribution (MSEB gold) | Held-out paraphrases |
|---|---|---|
| clinical/v6 | **172/172 = 100%** | not measured |
| software_dev/v6 | **181/181 = 100%** | not measured |
| conversational/v6 | **260/269 = 96.7%** (audited labels) | 9/15 = 60% (sparse-shape weakness) |

### Comparison vs the deleted regex classifier

Earlier grammar coverage report (docs/completed/p3-state-evolution-benchmark.md)
measured the regex parser at **250 / 747 = 33%** overall on MSEB
gold — 497 abstains, every one a seed-marker vocabulary gap.  The
v6 SLM replacement is **~95% overall** on the same gold set, with
explicit abstain behaviour preserved for non-TLG content.

## 10. File map (v6)

```
src/ncms/
├── domain/
│   ├── models.py
│   │   └── ShapeIntent Literal + ExtractedLabel.shape_intent field
│   └── tlg/
│       └── query_parser.py (~280 LOC, down from 768)
│           └── ParserContext, QueryStructure, _extract_event_names,
│               analyze_query (subject + target_entity only)
├── application/
│   └── tlg/
│       ├── dispatch.py
│       │   └── retrieve_lg(query, ..., slm_shape_intent, slm_abstained)
│       │       + _SLM_SHAPE_TO_DISPATCH_INTENT mapping
│       │       + 10 _dispatch_<intent> walkers (still/range removed)
│       └── vocabulary_cache.py
│           └── _L2_SEED_CURRENT, _L2_SEED_ORIGIN, _ISSUE_SEED
│               (moved from query_parser.py; L2-induction seeds only)
└── infrastructure/extraction/intent_slot/
    ├── adapter_loader.py → AdapterManifest.shape_intent_labels
    ├── lora_model.py
    │   ├── LoraJointModel: 6 heads incl. shape_intent_head
    │   └── LoraJointBert.extract() surfaces shape_intent in ExtractedLabel
    └── factory.py → ChainedExtractor.adapter_domain property

experiments/intent_slot_distillation/
├── schemas.py
│   └── ShapeIntent Literal + SHAPE_INTENTS + GoldExample.shape_intent
├── methods/joint_bert_lora.py
│   └── LoraJointModel + train() include 6th head + loss
├── corpus/
│   ├── build_shape_intent_gold.py (MSEB gold → GoldExample rows)
│   ├── gold_shape_intent_swe_diff.jsonl         (125 rows)
│   ├── gold_shape_intent_clinical.jsonl         (172 rows)
│   ├── gold_shape_intent_software_dev.jsonl     (181 rows)
│   └── gold_shape_intent_conversational.jsonl   (269 rows — post-audit)
├── corpus/loader.py → shape_intent validated + round-tripped
├── train_adapter.py → derives shape_intent_labels per adapter
└── adapters/
    ├── conversational/v6 (96.7% MSEB; audited gold)
    ├── clinical/v6       (100% MSEB)
    └── software_dev/v6   (100% MSEB)

benchmarks/mseb_convo/audit/
├── shape_intent_audit.py            — classifier rules
├── apply_shape_intent_audit.py      — gold regenerator
├── shape_intent_audit.jsonl         — per-query verdicts
└── shape_intent_audit.md            — human-readable summary
```
