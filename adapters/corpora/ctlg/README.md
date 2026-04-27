# CTLG training corpora

BIO cue-tagged queries and memories for the dedicated CTLG sequence
tagger.  Schema: one JSON object per line with fields:

```
{
  "text":    "<original text>",
  "domain":  "software_dev" | "clinical" | "conversational" | "swe_diff",
  "voice":   "query" | "memory",
  "tokens":  ["What", "happened", "before", "Postgres", "?"],
  "cue_tags": ["O", "O", "B-TEMPORAL_BEFORE", "B-REFERENT", "O"],
  "char_offsets": [[0, 4], [5, 13], [14, 20], [21, 29], [29, 30]],
  "source":  "hand-labeled" | "spark_llm" | "gazetteer_bootstrap",
  "split":   "train" | "dev" | "test" | "gold" | "llm" | "sdg" | "adversarial"
}
```

- `text` — the original query or memory content
- `voice` — `"query"` rows feed the query-side CTLG head;
  `"memory"` rows feed the ingest-side causal-edge tagger
- `tokens` — whole-word-level surface tokens; BERT wordpieces get
  propagated-label at training time, not stored here
- `split` — training/evaluation partition. Use `"llm"` for raw generated rows
  before review, then promote reviewed rows into `"gold"` / `"train"` / `"test"`.

## Files

| File | Purpose |
|------|---------|
| `pilot_cues_software_dev.jsonl` | 100 hand-labeled queries, two annotators + consolidated gold |
| `gold_cues_software_dev.jsonl` | Production training gold (LLM-labeled + human-reviewed) |
| `gold_memory_cues_software_dev.jsonl` | Memory-voice cue gold for ingest-side causal tagger |
| `gold_counterfactual_software_dev.jsonl` | Counterfactual queries for modal axis |
| `held_out_cues_software_dev.jsonl` | Held-out eval set (distinct LLM generator, hand-verified) |
| `llm_*_software_dev_pilot.jsonl` | Validation-first Nemotron pilot rows generated before SDG scale-out |
| `sdg_mixed_software_dev_ctlg.jsonl` | Deterministic BIO-controlled SDG rows for cue-family coverage |

## Targets

Per [CTLG design §5.5](../../../docs/research/ctlg-design.md):

- ~3000 query-voice cue-tagged rows
- ~2000 memory-voice cue-tagged rows
- ~300 counterfactual queries
- Inter-annotator κ ≥ 0.8 on pilot before scale-out

See `docs/research/ctlg-cue-guidelines.md` for the annotator contract.

## Generation

Use the validation-first generator command for raw LLM rows:

```bash
ncms adapters generate-ctlg \
  --domain software_dev \
  --voice query \
  --n-rows 25 \
  --model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  --api-base http://spark-ee7d.local:8000/v1 \
  --output adapters/corpora/ctlg/llm_query_software_dev.jsonl \
  --report-path /tmp/ctlg_generation_report.json
```

`--voice counterfactual` produces query-voice rows with modal cues. The command
validates every row with the canonical CTLG loader and refuses to write a corpus
when any row fails schema, BIO legality, or offset checks. Use `--print-prompt`
for prompt review without spending model calls.

Use deterministic SDG for balanced training coverage:

```bash
ncms adapters generate-ctlg-sdg \
  --domain software_dev \
  --voice mixed \
  --n-rows 500 \
  --output adapters/corpora/ctlg/sdg_mixed_software_dev_ctlg.jsonl \
  --report-path /tmp/ctlg_sdg_report.json
```

SDG rows are template-backed and locally tokenized, so they should be the
coverage backbone for rare cue families and compositions. LLM rows are the
style/diversity layer and should be reviewed or mixed in after validation.
