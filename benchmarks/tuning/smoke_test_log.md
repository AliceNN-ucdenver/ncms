# Smoke Test Log — 2026-03-14T00:15:28.626949+00:00

Git SHA: `9e3a6dd`

## spark_connectivity
**Status**: PASS
- **model**: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
- **url**: http://spark-ee7d.local:8000/v1/models

## store_pipeline
**Status**: PASS
- **items**:
  - content=The auth service uses OAuth 2.0 for all API endpoints, admission_score=0.369, route=atomic_memory, elapsed_ms=5181.2
  - content=Database migration to PostgreSQL 16 completed on 2026-01-15, admission_score=0.399, route=atomic_memory, elapsed_ms=174.6
  - content=Decided to use Redis for session caching instead of Memcache, admission_score=0.195, route=atomic_memory, elapsed_ms=1445.4

## contradiction_detection
**Status**: PASS
- **admission_score**: 0.074
- **route**: atomic_memory
- **contradictions**:
  - existing_memory_id=67e54317-b502-42d0-84e4-0d3c817c0b03, contradiction_type=factual, explanation=The auth service uses OAuth 2.0 for all API endpoints, which directly contradicts the claim that it uses API keys instead., severity=high
- **elapsed_ms**: 4104.3

## intent_search
**Status**: PASS
- **num_results**: 2
- **elapsed_ms**: 65.7
- **top_results**:
  - score=1.2372012010734499, intent=current_state_lookup, content=The auth service uses API keys, not OAuth
  - score=0.3540574233840949, intent=current_state_lookup, content=The auth service uses OAuth 2.0 for all API endpoints

## routing_distribution
**Status**: 
- **atomic_memory**: 4

## graph_state
**Status**: 
- **entities**: 11
- **relationships**: 0

