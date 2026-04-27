# CTLG Implementation Plan

**Status:** Phase 0-4 implemented; Phase 5 shadow/adapter harness ready  
**Companion docs:** `ctlg-design.md`, `ctlg-grammar.md`, `ctlg-cue-guidelines.md`

## Decision

CTLG will be trained and validated as a dedicated cue-tagger adapter before it is
allowed to affect memory search.

The v9 content SLM remains a 5-head adapter:

- intent
- topic
- admission
- state_change
- role

The CTLG adapter is a sibling model with one job:

- BIO cue tagging for causal, temporal, ordinal, modal, referent, subject, and
  scope cues

The two adapters may share tokenizer/backbone family and deployment conventions,
but they do not share LoRA weights or training loss.

## Phase 0 - Boundary Guardrails

Goal: make it hard to accidentally rebuild the failed joint 6-head design.

Done in this pass:

- `MemoryService` accepts a dedicated `ctlg_cue_tagger`.
- Query-side `retrieve_lg()` uses the CTLG cue tagger, not `intent_slot`.
- Ingest-side cue tags serialize to `memory.structured["ctlg"]`.
- `intent_slot` payloads no longer own CTLG cue tags.
- Causal-edge extraction reads `structured["ctlg"]["cue_tags"]`, with legacy
  fallback for old v8 experiment rows.
- Architecture fitness test blocks runtime CTLG cue heads/label fields in the
  v9 joint adapter.

## Phase 1 - Corpus Contract

Deliverables:

- [x] `CTLGExample` schema: `text`, `tokens`, `cue_tags`, `char_offsets`,
  `voice`, `domain`, `split`, `source`, `note`.
- [x] Dedicated CTLG corpus loader. Do not reuse the v9 five-head loader except
  for shared validation primitives.
- [x] BIO legality validator:
  - every token has one label
  - `I-X` cannot start a span
  - character offsets round-trip to source text, or derive deterministically from
    surface tokens when omitted
- [x] Diagnostics:
  - domain counts
  - voice counts
  - split counts
  - cue-label counts
  - cue-family counts
- [x] Generator and judge prompts for:
  - query-voice examples
  - memory-voice examples
  - counterfactual/modal examples
- [x] `ncms adapters generate-ctlg` command that calls an LLM, validates rows
  through the CTLG corpus contract, and refuses to write invalid output.
- [x] Wordpiece expansion helper for the training collator.

Exit gate:

- 100 percent corpus schema validity.
- Held-out split is natural-language paraphrase, not template neighbor rows.
- Per-family label counts are visible in diagnostics.

## Phase 2 - Independent CTLG Adapter

Deliverables:

- [x] `ncms.application.adapters.methods.cue_tagger`
- [x] `CTLGAdapterManifest` with `cue_labels`, `encoder`, LoRA config, corpus hash,
  training metrics, and gate metrics.
- [x] Per-token CE training loop only.
- [x] Inference wrapper implementing `CTLGCueTagger.extract_cues()`.

Offline gates before runtime integration:

- Overall token micro F1 >= 0.90.
- Non-`O` macro F1 >= 0.80.
- Causal/temporal/modal family F1 reported separately.
- Query-voice and memory-voice metrics reported separately.
- Confusion matrix includes `O` over-prediction rate.
- Latency measured on CPU and MPS/CUDA where available.

## Phase 3 - Parser And Dispatcher Validation

Deliverables:

- [ ] Golden cue-tag fixtures for every supported `TLGQuery` relation.
- [x] Synthesizer tests using gold cue tags, independent of the adapter.
- [x] Dispatcher/composition shadow tests using gold `TLGQuery`, independent
  of the adapter.
- [x] End-to-end query tests using a stub `CTLGCueTagger`.
- [x] Harness modes for `gold_cues`, `adapter_only`, and `ctlg_shadow` that
  can run before CTLG is allowed to mutate live ranking.

Exit gate:

- Synthesizer hit rate >= 0.80 on held-out gold cue tags.
- Synthesizer exact logical-form accuracy >= 0.85 where it hits.
- Dispatcher never mutates BM25 ordering unless confidence is HIGH or MEDIUM.

## Phase 4 - Ingest Causal Edges

Deliverables:

- [x] Run CTLG cue tagging on memory content only when temporal stack is enabled and
  a CTLG adapter is wired.
- [x] Store `structured["ctlg"]` with `schema_version`, `method`, `voice`,
  `latency_ms`, and `cue_tags`.
- [x] Persist `CAUSED_BY` / `ENABLES` edges only when cue pairs resolve to L2 state
  nodes above confidence threshold.
- [x] Add provenance metadata to causal edges.

Exit gate:

- Curated memory narratives produce expected causal edges.
- Unresolved or low-confidence cue pairs abstain without ingest failure.
- CTLG-off ingest behavior is byte-for-byte compatible except timestamps/logs.

## Phase 5 - Benchmark Harness

CTLG must be testable before merge without relying on live production search.

Required harness modes:

- [x] `gold_cues`: bypass model, test synthesizer + dispatcher.
- [x] `adapter_only`: model produces cue tags, no retrieval mutation.
- [x] `ctlg_shadow`: full CTLG trace emitted, BM25 result order unchanged.
- [ ] `ctlg_on`: grammar answer composes with search only at HIGH/MEDIUM confidence.

Implementation note: the first three modes live in
`ncms.application.adapters.ctlg.harness` and are intentionally usable from
unit tests or benchmark code without routing through production `search()`.
`ctlg_on` should stay pending until shadow traces show useful rank movement.
The deployed cue tagger is loaded through `ncms.application.ctlg_cue_tagger`;
MSEB opts in with `--ctlg-adapter-domain` and optional
`--ctlg-adapter-version`.

Diagnostics per query:

- [x] cue tags
- [x] synthesized `TLGQuery`
- [x] synthesizer rule id
- [x] grammar confidence
- [x] candidate containment (`gold_in_candidates`)
- [x] rank before CTLG
- [x] rank after CTLG
- [x] causal edges traversed
- [x] abstention reason

MSEB writes these diagnostics under `predictions.jsonl` →
`head_outputs.ctlg_shadow` when the backend exposes CTLG shadow data. The
scored `ranked_mids` remain the unmodified search results.

Merge gate:

- CTLG-off MSEB/LongMemEval/SciFact regressions: zero.
- `gold_cues` proves dispatcher can answer the target class.
- `adapter_only` proves cue tagging generalizes on held-out natural queries.
- `ctlg_shadow` shows expected rank movement before mutation is enabled.
- `ctlg_on` improves targeted CTLG slice without lowering aggregate recall.

## Recommended Build Order

1. Keep Phase 0 merged first.
2. Build corpus schema, loader, validator, and diagnostics.
3. Generate a small pilot set and manually inspect it.
4. Train `software_dev/ctlg-v1` independently.
5. Run `gold_cues` and `adapter_only` gates.
6. Wire CTLG into query path in shadow mode.
7. Wire memory-voice ingest and causal-edge persistence.
8. Enable `ctlg_on` only after shadow-mode traces show useful movement.

## Answer To The Merge Question

Yes, CTLG can and should be tested independently before merge.

The adapter can be trained and evaluated offline, the synthesizer can be tested
with gold cue tags, the dispatcher can be tested with gold `TLGQuery` values,
and the full retrieval path can run in shadow mode before it is allowed to
change ranking.
