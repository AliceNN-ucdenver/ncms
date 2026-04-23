# CTLG training corpora

BIO cue-tagged queries and memories for the CTLG 6th-head sequence
tagger.  Schema: one JSON object per line with fields:

```
{
  "text":    "<original text>",
  "domain":  "software_dev" | "clinical" | "conversational" | "swe_diff",
  "voice":   "query" | "memory",
  "tokens":  [{"char_start": 0, "char_end": 4, "surface": "What",
               "cue_label": "O", "confidence": 1.0}, ...],
  "source":  "hand-labeled" | "spark_llm" | "gazetteer_bootstrap",
  "split":   "pilot" | "gold" | "held_out"
}
```

- `text` — the original query or memory content
- `voice` — `"query"` rows feed the query-side CTLG head;
  `"memory"` rows feed the ingest-side causal-edge tagger
- `tokens` — whole-word-level `TaggedToken`s; BERT wordpieces get
  propagated-label at training time, not stored here
- `split` — `"pilot"` for the 100-row annotator-agreement set;
  `"gold"` for the main training corpus; `"held_out"` for eval

## Files

| File | Purpose |
|------|---------|
| `pilot_cues_software_dev.jsonl` | 100 hand-labeled queries, two annotators + consolidated gold |
| `gold_cues_software_dev.jsonl` | Production training gold (LLM-labeled + human-reviewed) |
| `gold_memory_cues_software_dev.jsonl` | Memory-voice cue gold for ingest-side causal tagger |
| `gold_counterfactual_software_dev.jsonl` | Counterfactual queries for modal axis |
| `held_out_cues_software_dev.jsonl` | Held-out eval set (distinct LLM generator, hand-verified) |

## Targets

Per [CTLG design §5.5](../../../docs/research/ctlg-design.md):

- ~3000 query-voice cue-tagged rows
- ~2000 memory-voice cue-tagged rows
- ~300 counterfactual queries
- Inter-annotator κ ≥ 0.8 on pilot before scale-out

See `docs/research/ctlg-cue-guidelines.md` for the annotator contract.
