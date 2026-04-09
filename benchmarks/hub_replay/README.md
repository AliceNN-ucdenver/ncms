# Hub Replay Benchmark

## What It Tests

Replays the exact 67-memory ingest sequence from a live multi-agent hub to provide deterministic before/after comparison for resilience improvements. Measures:

- **Ingest latency** -- p50/p95/p99 per-memory timing
- **Search latency** -- p50 query response time
- **Data integrity** -- Duplicate detection, junk entity filtering
- **Entity quality** -- Total entities, junk entity rate, graph connectivity

This is not an IR benchmark with ground-truth relevance judgments; it is an operational health check for the NCMS pipeline on realistic multi-agent architecture data.

## Dataset

| Component  | Size        | Source |
|-----------|-------------|--------|
| Memories   | 67          | Hub fixture (ADRs, CALM models, threat models) |
| Queries    | ~10         | Architecture-specific test queries |

Data is embedded in `benchmarks/hub_replay/fixtures.py` as Python literals.

## GLiNER Topic Labels (Replace Mode)

**Domain**: `architecture`

Labels (10, NemoClaw blueprint):
`framework`, `database`, `protocol`, `standard`, `threat`, `pattern`, `security_control`, `api_endpoint`, `data_model`, `architecture_decision`

Rationale: Hub content consists of Architecture Decision Records, CALM architecture models, threat models, and compliance checklists. These labels match the NemoClaw blueprint's software architecture domain, capturing the entity types that matter for multi-agent coordination on architecture artifacts.

## How to Run

```bash
# Full replay
uv run python -m benchmarks.hub_replay.run_hub_replay

# With verbose logging
uv run python -m benchmarks.hub_replay.run_hub_replay --verbose

# Custom output directory
uv run python -m benchmarks.hub_replay.run_hub_replay --output-dir /tmp/results
```

## Expected Metrics

| Metric               | Target        |
|---------------------|---------------|
| Ingest p50          | < 100 ms      |
| Ingest p95          | < 500 ms      |
| Search p50          | < 50 ms       |
| Junk entity rate    | < 10%         |
| Duplicate count     | 0             |
