# BEIR Retrieval Ablation Benchmark

## What It Tests

Measures the contribution of each NCMS retrieval pipeline component (BM25, SPLADE v3, graph spreading activation, ACT-R) on standard IR benchmarks. Ablation configs systematically disable components to isolate their effect on nDCG@10, MRR@10, and Recall@100.

## Datasets

| Dataset  | Corpus Size | Queries | Domain        | Source |
|----------|-------------|---------|---------------|--------|
| SciFact  | 5,183 docs  | 300     | Science facts | BEIR   |
| NFCorpus | 3,633 docs  | 323     | Biomedical    | BEIR   |
| ArguAna  | 8,674 docs  | 1,406   | Arguments     | BEIR   |

Source: [BEIR Benchmark](https://github.com/beir-cellar/beir)

## GLiNER Topic Labels (Replace Mode)

Domain-specific labels replace universal labels to keep GLiNER extraction focused (~10 labels, ~9 entities/doc).

**SciFact** (domain: `science`):
`medical_condition`, `medication`, `protein`, `gene`, `chemical_compound`, `organism`, `cell_type`, `tissue`, `symptom`, `therapy`

**NFCorpus** (domain: `biomedical`):
`disease`, `nutrient`, `vitamin`, `mineral`, `drug`, `food`, `protein`, `compound`, `symptom`, `treatment`

**ArguAna** (domain: `argument`):
`person`, `organization`, `location`, `nationality`, `event`, `law`

Rationale: Taxonomy test showed 10 domain-specific labels produce 9.1 entities/doc with 181 unique entities. Generic 20-label sets degrade extraction quality.

## How to Run

```bash
# Install benchmark deps
uv sync --group bench

# All datasets (sequential)
./benchmarks/run.sh

# Single dataset
./benchmarks/run.sh scifact

# Parallel execution
./benchmarks/run_parallel.sh
./benchmarks/run_parallel.sh scifact nfcorpus

# Direct Python
uv run python -m benchmarks.beir.run_ablation
uv run python -m benchmarks.beir.run_ablation --datasets scifact --verbose
```

## Expected Metrics

| Config            | SciFact nDCG@10 | NFCorpus nDCG@10 | ArguAna nDCG@10 |
|-------------------|-----------------|------------------|-----------------|
| BM25 only         | ~0.68           | ~0.33            | ~0.40           |
| BM25 + SPLADE     | ~0.70           | ~0.34            | ~0.42           |
| BM25 + SPLADE + G | ~0.72           | ~0.34            | ~0.42           |

Best SciFact config: BM25(0.6) + SPLADE(0.3) + Graph(0.3) = nDCG@10 0.7206, exceeding published ColBERTv2 and SPLADE++ on SciFact.
