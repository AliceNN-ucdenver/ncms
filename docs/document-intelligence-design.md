# Document Intelligence Design

This document covers the complete Document Intelligence feature set: the implemented foundation (Phases 1, 2, 2.5), the agent consolidation (6 → 5), and the audit/provenance features designed from a Chief Architect + CIRO review.

For future phases (Coding Agent, Learning System), see [dashboard-evolution-design.md](dashboard-evolution-design.md).

---

## Implemented Foundation

> The following phases are complete and operational. See the feature tables for implementation details.

### Phase 1 (Foundation) — Complete

| # | Feature | Status |
|---|---------|--------|
| 1 | Project/Epic View | Done |
| 2 | Live Pipeline Progress | Done |
| 5 | Spec Quality (Completeness + Contracts) | Done |
| 6 | NemoGuardrails | Done |
| 15 | Prompt Library | Done |

### Phase 2 (Document Intelligence + Archeologist) — Complete

| # | Feature | Status |
|---|---------|--------|
| 19 | Document-Memory Integration (GLiNER entities, doc-by-reference) | Done |
| 20 | Archeologist Agent (GitHub repo analysis) | Done |
| 21 | Pipeline Interrupt | Done |
| 22 | Expert Classify Fix (deterministic [NCMS:review] tags) | Done |
| 23 | Bug Fixes (Phase 2) | Done |
| 24 | Bus-Based Agent Triggering | Done |
| 25 | Entity Recall Validation (A/B comparison) | Deferred |
| 26 | Semi-Formal Reasoning Prompts (Meta arXiv:2603.01896, +29%) | Done |
| 4 | Document Diff View (version comparison in viewer) | Done |
| 13 | Template Library (reusable design fragments) | Future |

### Phase 2.5 (Document Intelligence Persistence) — Complete

| # | Feature | Status |
|---|---------|--------|
| 27-32 | Core persistence (docs, traceability, reviews, projects, pipeline, search API) | Done |
| 33 | Phoenix spans for agent operations | Partial — NAT workflow spans work; LangGraph node-level spans need instrumentor fix (see audit design #5) |
| 34 | Auditor-grade project view (D3 doc flow graph) | Done |
| 35-36 | Approval decision log + guardrail violation persistence | Done |
| 37 | Knowledge grounding log | Done |
| 38 | LLM call metadata + Phoenix trace link | Done |
| 39 | Document content hashing (SHA-256) | Done |
| 40 | Agent config snapshots | Done |
| 41 | Bus conversation log | Done |
| 42 | Guardrail approval gate (human-in-the-loop) | Done |

### Agent Consolidation (6 → 5 agents) — Complete

| Before | After | Change |
|--------|-------|--------|
| Researcher | — | Merged into Archeologist |
| Archeologist | **Archeologist** (dual-path) | Research (web) + Archaeology (GitHub) |
| Builder | **Designer** | Renamed |
| Product Owner | Product Owner | Unchanged |
| Architect | Architect | Unchanged |
| Security | Security | Unchanged |

---

## Audit & Provenance Features

Phase 2.5 delivered the data foundation. The features below transform that foundation into an enterprise-grade audit and provenance system, reviewed from the perspective of a Chief Architect and Chief Information Risk Officer.

---

## 1. Tamper-Evident Audit Log (Hash Chain)

### Problem

Every audit table uses standard SQLite rows with no integrity protection. Any process with write access can retroactively modify approval decisions, change review scores, or delete guardrail violations. SOC 2 CC7.2 requires tamper detection on audit logs. ISO 27001 A.12.4 requires protection of log information.

### Design

Add a `prev_hash` column to all audit tables. Each new record computes `SHA256(prev_hash || record_json)` and stores it. The first record in a chain uses a well-known genesis hash.

```sql
ALTER TABLE pipeline_events ADD COLUMN prev_hash TEXT;
ALTER TABLE approval_decisions ADD COLUMN prev_hash TEXT;
ALTER TABLE guardrail_violations ADD COLUMN prev_hash TEXT;
ALTER TABLE llm_calls ADD COLUMN prev_hash TEXT;
ALTER TABLE bus_conversations ADD COLUMN prev_hash TEXT;
```

**Insert logic:** Before each INSERT, query the most recent `prev_hash` for the table (or per-project chain), compute `SHA256(prev_hash || json(new_record))`, and store as the new record's `prev_hash`.

**Verification endpoint:** `GET /api/v1/projects/{id}/verify-integrity` walks the chain for all audit tables and reports any breaks. Returns `{verified: true, records_checked: N}` or `{verified: false, break_at: record_id, table: "approval_decisions"}`.

**Cost:** One SHA-256 computation per write (negligible). Verification is a linear scan — O(N) where N is total audit records for a project.

### Effort: 1-2 days

---

## 2. Authentication on Approval Endpoints

### Problem

The `decide_approval` endpoint accepts any caller with no authentication. The `decided_by` field is a free-text string from the request body. Anyone on the network can approve or deny with any identity. For a human-in-the-loop gate that controls code generation, this is a critical control weakness.

### Design

**Phase 1: Local auth with JWT (immediate)**

V8 migration adds a `users` table with bcrypt-hashed passwords:

```sql
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'reviewer',  -- reviewer | admin
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);
```

Seeded at hub startup with a default user:
- username: `shawn`, password: `ncms` (bcrypt hashed), role: `admin`

**API endpoints:**

- `POST /api/v1/auth/login` — accepts `{username, password}`, validates against bcrypt hash, returns `{token: "<JWT>", expires_in: 86400}`. JWT payload: `{sub: "shawn", role: "admin", exp: ...}`. Signed with a hub-generated secret (or `NCMS_JWT_SECRET` env var).
- `GET /api/v1/auth/me` — returns current user from JWT token.

**Dashboard login page:**

- On first load (no token in localStorage), show a login form instead of the dashboard.
- On successful login, store the JWT in localStorage and redirect to the dashboard.
- All API calls include `Authorization: Bearer <token>` header.
- On 401 response, redirect to login.

**Server-side middleware:**

- Starlette middleware validates JWT on all mutation endpoints.
- `decided_by` on approval decisions is set **server-side** from `request.state.user.username`, not from the request body. The client cannot forge the approver identity.
- Read-only endpoints (GET) can remain unauthenticated for now (dashboard viewing).

**Protected endpoints (require JWT):**
- `POST /api/v1/approvals/{id}/decide` — approver identity from token
- `POST /api/v1/projects` — creator identity from token
- `POST /api/v1/documents` — publisher identity from token
- `POST /api/v1/pipeline/interrupt/{agent_id}` — interrupter identity from token

**Phase 2 (future): OIDC provider swap.**

Replace the local `users` table with an OIDC redirect flow. The JWT validation stays the same — only the token issuer changes. The `users` table becomes a cache/profile store. No dashboard or middleware changes needed beyond the login page redirect.

### Effort: 2-3 days (Phase 1)

---

## 3. Unified Audit Timeline

### Problem

Audit data is collected across 7 tables but the project detail view only shows the D3 document flow graph and a pipeline progress bar. An auditor cannot see the chronological sequence of all actions without querying the database directly. The collected data is substantially richer than what the frontend exposes.

### Design

**New API endpoint:** `GET /api/v1/projects/{id}/audit-timeline`

Returns a chronological union of ALL events for a project:

```json
[
  {"timestamp": "...", "type": "pipeline", "agent": "archeologist", "node": "check_guardrails", "status": "started"},
  {"timestamp": "...", "type": "pipeline", "agent": "archeologist", "node": "plan_queries", "status": "completed", "detail": "5 queries"},
  {"timestamp": "...", "type": "llm_call", "agent": "archeologist", "node": "synthesize_research", "prompt_size": 12000, "response_size": 11687, "duration_ms": 45000, "model": "Nemotron-3-Nano"},
  {"timestamp": "...", "type": "document", "agent": "archeologist", "doc_type": "research", "title": "Market Research Report", "size_bytes": 15520, "content_hash": "5202c5c9..."},
  {"timestamp": "...", "type": "bus_conversation", "from": "designer", "to": "architect", "question": "[NCMS:question] What architectural patterns...", "confidence": 0.9, "duration_ms": 107925},
  {"timestamp": "...", "type": "review_score", "reviewer": "architect", "score": 88, "round": 1},
  {"timestamp": "...", "type": "guardrail_violation", "policy": "compliance", "rule": "secret_detection", "escalation": "warn", "message": "Possible API key"},
  {"timestamp": "...", "type": "grounding", "document_id": "...", "memory_id": "...", "score": 0.87, "domain": "architecture"},
  {"timestamp": "...", "type": "approval", "decision": "approved", "decided_by": "human", "comment": "Looks good"},
  {"timestamp": "...", "type": "config_snapshot", "agent": "designer", "model": "Nemotron-3-Nano", "thinking": false, "max_tokens": 131072}
]
```

**Backend:** SQL UNION across `pipeline_events`, `approval_decisions`, `guardrail_violations`, `llm_calls`, `bus_conversations`, `review_scores`, `grounding_log`, and `agent_config_snapshots`, all filtered by `project_id` and ordered by timestamp.

**Frontend:** A sortable, filterable table in the project detail view. Columns: timestamp, type (color-coded chip), agent, detail. Filters by event type, agent, time range. Replaces the D3 graph as the **primary audit interface**. The D3 graph remains as a supplementary visualization accessible via a toggle.

**Each row is expandable:** clicking a pipeline event shows the full detail. Clicking an LLM call shows prompt/response sizes and links to Phoenix trace. Clicking a review score shows SCORE/SEVERITY/COVERED/MISSING/CHANGES. Clicking a grounding entry shows the cited memory content.

### Effort: 2-3 days

---

## 4. Provenance Certificate (Single Document Drill-Down)

### Problem

For any single artifact, an auditor needs to answer: "Prove this document is legitimate." The D3 graph shows relationships between documents but does not provide the detailed provenance chain for a specific artifact.

### Design

**New API endpoint:** `GET /api/v1/documents/{id}/provenance`

Returns the complete provenance chain for one document:

```json
{
  "document": { "id": "...", "title": "...", "content_hash": "...", "from_agent": "designer", "created_at": "..." },
  "lineage": [
    { "doc_id": "...", "title": "Market Research Report", "link_type": "derived_from", "agent": "archeologist" },
    { "doc_id": "...", "title": "PRD", "link_type": "derived_from", "agent": "product_owner" }
  ],
  "reviews": [
    { "reviewer": "architect", "score": 88, "round": 1, "severity": "Low", "covered": "..." },
    { "reviewer": "security", "score": 85, "round": 1, "severity": "Low", "covered": "..." }
  ],
  "guardrail_findings": [
    { "policy": "compliance", "rule": "secret_detection", "escalation": "warn", "message": "..." }
  ],
  "approvals": [
    { "decision": "approved", "decided_by": "human", "comment": "...", "timestamp": "..." }
  ],
  "llm_calls": [
    { "node": "synthesize_design", "prompt_size": 29000, "response_size": 24000, "duration_ms": 120000, "model": "..." }
  ],
  "grounding": [
    { "memory_id": "...", "retrieval_score": 0.87, "entity_query": "ADR architecture decisions..." }
  ],
  "config_at_creation": {
    "model": "Nemotron-3-Nano-30B", "thinking": false, "max_tokens": 131072
  },
  "integrity": {
    "content_hash_verified": true,
    "hash_chain_verified": true
  }
}
```

**Frontend:** A modal (similar to the document viewer) that shows all provenance data for a single document. Accessed by clicking a document in the D3 graph or audit timeline.

### Effort: 2-3 days

---

## 5. Phoenix LangGraph Tracing (Corrected Design)

### Problem

Phoenix currently shows only NAT's top-level `<workflow>` spans (7 spans total). The per-node graph execution (plan_queries, synthesize, request_review, etc.) and per-LLM-call detail (prompt/response content, token counts) are not captured.

### Root Cause

The current `traced_llm_call()` wrapper in `pipeline_utils.py` calls `phoenix.otel.register()` which **overwrites** NAT's global TracerProvider. NAT's own phoenix exporter stops receiving spans. The LangChainInstrumentor hooks in but conflicts with NAT's instrumentation.

### Correct Design

**Remove `traced_llm_call()` entirely.** The `LangChainInstrumentor` from `openinference-instrumentation-langchain` automatically captures:
- `graph.ainvoke()` as a root span with all node executions as child spans
- Each `llm.ainvoke()` call as an LLM span with full prompt/response content, token counts, model name
- LangGraph node transitions, conditional edges, and state updates

This is what the [Phoenix LangGraph documentation](https://arize.com/docs/phoenix/integrations/python/langgraph/langgraph-tracing) describes: "spans will be created whenever an agent is invoked."

**Implementation:**

1. **Remove `traced_llm_call()`** from `pipeline_utils.py`. Revert all 10 LLM call sites back to direct `self.llm.ainvoke()` calls.

2. **Remove `_ensure_langchain_instrumented()`** and the `register()` call from `pipeline_utils.py`. These conflict with NAT's tracer provider.

3. **Keep the audit metadata recording** (`record_llm_call`, `record_config_snapshot`, etc.) but trigger it from the node methods directly — wrap the `ainvoke()` call with timing:

```python
t0 = time.monotonic()
response = await self.llm.ainvoke(messages)
duration_ms = int((time.monotonic() - t0) * 1000)
# Fire-and-forget audit record
await self.client.record_llm_call(
    project_id=state.get("project_id"), agent=self.from_agent,
    node="synthesize", prompt_size=len(prompt), response_size=len(response.content),
    duration_ms=duration_ms, model=str(getattr(self.llm, "model_name", None)),
)
```

4. **Let NAT handle Phoenix entirely.** NAT's `_type: phoenix` config already starts the exporter. The only thing we need to add is the `LangChainInstrumentor().instrument()` call at agent startup — but **without calling `register()`**. NAT's TracerProvider is already set as the global default by the time our code runs.

```python
def _ensure_langchain_instrumented():
    global _langchain_instrumented
    if _langchain_instrumented:
        return
    _langchain_instrumented = True
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        if not LangChainInstrumentor().is_instrumented_by_opentelemetry:
            LangChainInstrumentor().instrument()
            logger.info("[otel] LangChain/LangGraph instrumented for Phoenix")
    except Exception as e:
        logger.debug("[otel] LangChain instrumentation not available: %s", e)
```

5. **Call `_ensure_langchain_instrumented()` once at agent startup** (in the NAT registration function, after `graph = await agent.build_graph()`), not inside `traced_llm_call`.

**Expected Phoenix traces after this change:**

```
▼ archeologist_agent (CHAIN) — NAT workflow span
  ▼ RunnableSequence (CHAIN) — LangGraph compiled graph
    ▼ check_guardrails (CHAIN)
    ▼ plan_queries (CHAIN)
      ▼ ChatOpenAI (LLM) — full prompt/response/tokens
    ▼ parallel_search (CHAIN)
    ▼ arxiv_search (CHAIN)
    ▼ synthesize_research (CHAIN)
      ▼ ChatOpenAI (LLM) — full prompt/response/tokens
    ▼ publish (CHAIN)
    ▼ verify_and_trigger (CHAIN)
```

Each LLM span includes: `input.value` (full prompt), `output.value` (full response), `llm.token_count.prompt`, `llm.token_count.completion`, `llm.model_name`. This is richer than anything `traced_llm_call()` could provide because it comes from LangChain's native callback system.

### Effort: 1 day (mostly reverting traced_llm_call)

---

## 6. Automated Compliance Score

### Problem

Review scores, guardrail violations, and grounding logs exist per project but there is no composite metric that answers "how compliant is this project?"

### Design

A computed `compliance_score` per project, derived from:

| Signal | Weight | Calculation |
|--------|--------|-------------|
| Review score average | 40% | Mean of all review scores across all rounds |
| Guardrail violations | 20% | Penalty: -10 per warn, -25 per block, -50 per reject |
| Grounding coverage | 15% | % of review citations that have grounding_log entries |
| Approval gate | 15% | +100 if all gates passed, 0 if any denied |
| Document completeness | 10% | % of expected doc_types present (research, prd, design, review) |

The score is computed on-demand and displayed on the project card alongside the quality score. A `GET /api/v1/projects/{id}/compliance` endpoint returns the breakdown.

### Effort: 1 day

---

## 7. Document Diff Between Versions

### Problem

Design documents go through multiple revision rounds (v1 → v2 → v3). The `supersedes` links and `parent_doc_id` chains exist, but there is no way to see what changed between versions.

### Design

**Frontend:** A side-by-side diff view in the document viewer modal. When a document has a `parent_doc_id`, a "Compare with previous" button appears. Clicking it fetches both versions and renders a character-level diff using a client-side library (jsdiff or diff-match-patch).

**No backend changes needed** — both versions are already stored with full content.

### Effort: 1 day

---

## 8. SQLite Foreign Key Enforcement

### Problem

The DDL declares FOREIGN KEY constraints but SQLite does not enforce them unless `PRAGMA foreign_keys = ON` is set per connection. Orphaned records can be created without error.

### Design

Add `PRAGMA foreign_keys = ON` immediately after database connection in the SQLite store initialization, before any queries.

```python
async def _init_db(self):
    await self.db.execute("PRAGMA foreign_keys = ON")
```

### Effort: 15 minutes

---

## 9. Content Hash Verification on Read

### Problem

SHA-256 hashes are computed at publish time but never verified on read. Corruption or tampering goes undetected.

### Design

Add a `verify_integrity` option to `get_document()` that re-computes the hash and compares. Also add a background health check endpoint `GET /api/v1/documents/verify-all` that scans all documents.

### Effort: 0.5 days

---

## 10. Quality Trend Analysis Across Projects

### Problem

Each project has isolated metrics. There is no portfolio-level view of quality trends, common violations, or LLM cost patterns.

### Design

**New dashboard tab: "Analytics"** showing:
- Average review scores over time (line chart)
- Most common guardrail violations (bar chart)
- Most frequently missing design sections from MISSING fields (word cloud or bar chart)
- LLM cost trends: total prompt + response tokens per project, average duration per node
- Pipeline duration trends: time from project creation to design approval

All data already exists in the audit tables. This is purely a frontend aggregation and visualization effort.

### Effort: 3-5 days

---

## Known Issues

### Duplicate review scores

Review scores are saved twice per reviewer per round — once in `request_review` (immediately when parsed) and once in `verify` (at pipeline completion). This results in 4 rows for 2 reviewers instead of 2. Harmless for correctness (scores are identical) but produces duplicates in the audit timeline. Fix: remove the `save_review_score` call from the `verify` node since `request_review` already persists them.

---

## Implementation Priority

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| 1 | Phoenix LangGraph tracing (remove traced_llm_call, just instrument) | 1 day | Full graph visibility in Phoenix |
| 2 | Unified audit timeline API + table view | 2-3 days | Primary auditor interface |
| 3 | SQLite PRAGMA foreign_keys | 15 min | Data integrity |
| 4 | Tamper-evident hash chain | 1-2 days | Compliance proof |
| 5 | Auth on approval endpoints | 1-2 days | Control credibility |
| 6 | Provenance certificate drill-down | 2-3 days | Single-document audit proof |
| 7 | Content hash verification | 0.5 days | Tamper detection |
| 8 | Automated compliance score | 1 day | Portfolio metric |
| 9 | Document version diff | 1 day | Revision visibility |
| 10 | Quality trend analytics | 3-5 days | Portfolio intelligence |

---

## Experiment: Nemotron 3 Super 120B (12B Active)

### Hypothesis

The current pipeline uses Nemotron 3 Nano 30B (3B active parameters). Upgrading to Nemotron 3 Super 120B (12B active, 120B total via MoE) on the DGX Spark 128GB should produce significantly higher quality design documents, more accurate reviews, and better structured certificate reasoning — while fitting in the same GPU memory via NVFP4 quantization.

### Model Details

- **Model:** `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4`
- **Architecture:** MoE — 120B total, 12B active (4x the active parameters of Nano)
- **Quantization:** FP4 (NVFP4) — fits in 128GB with room for KV cache
- **Context:** 1M tokens (via `--max-model-len 1000000`)
- **Reasoning:** `super_v3_reasoning_parser.py` (extends DeepSeek R1 parser for Super's `<think>` tags)
- **Tool calling:** `qwen3_coder` parser (same as Nano)
- **Speculative decoding:** MTP with 3 speculative tokens (faster inference)

### Deployment on DGX Spark

```bash
# Download the reasoning parser plugin
wget https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4/raw/main/super_v3_reasoning_parser.py

# Deploy via vLLM (nightly build with FP4 + Marlin support)
docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-super \
  -e VLLM_NVFP4_GEMM_BACKEND=marlin \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -e VLLM_FLASHINFER_ALLREDUCE_BACKEND=trtllm \
  -e VLLM_USE_FLASHINFER_MOE_FP4=0 \
  -e HF_TOKEN=$HF_TOKEN \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v $(pwd)/super_v3_reasoning_parser.py:/app/super_v3_reasoning_parser.py \
  -p 8000:8000 \
  vllm/vllm-openai:cu130-nightly \
    --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 \
    --served-model-name nemotron-3-super \
    --host 0.0.0.0 \
    --port 8000 \
    --async-scheduling \
    --dtype auto \
    --kv-cache-dtype fp8 \
    --tensor-parallel-size 1 \
    --pipeline-parallel-size 1 \
    --data-parallel-size 1 \
    --trust-remote-code \
    --gpu-memory-utilization 0.90 \
    --enable-chunked-prefill \
    --max-num-seqs 4 \
    --max-model-len 1000000 \
    --moe-backend marlin \
    --mamba_ssm_cache_dtype float32 \
    --quantization fp4 \
    --speculative_config '{"method":"mtp","num_speculative_tokens":3,"moe_backend":"triton"}' \
    --reasoning-parser-plugin /app/super_v3_reasoning_parser.py \
    --reasoning-parser super_v3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
```

### Key vLLM Flags

| Flag | Purpose |
|------|---------|
| `--quantization fp4` | NVFP4 quantization — ~30GB model weight footprint |
| `--kv-cache-dtype fp8` | FP8 KV cache — maximizes context length in 128GB |
| `--moe-backend marlin` | Marlin kernels for MoE layers (faster than default) |
| `--speculative_config ...` | MTP speculative decoding with 3 tokens (lower latency) |
| `--reasoning-parser super_v3` | Separates `<think>` reasoning from output (like Nano's `nano_v3`) |
| `--max-model-len 1000000` | 1M context window |
| `--enable-chunked-prefill` | Handles long prompts without OOM |
| `VLLM_NVFP4_GEMM_BACKEND=marlin` | Use Marlin for FP4 GEMM operations |

### NCMS Config Change

To switch the pipeline to Super, update agent configs and hub env:

```bash
# Agent configs (archeologist.yml, designer.yml, product_owner.yml, etc.)
NCMS_LLM_MODEL=openai/nemotron-3-super
NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1
```

Or per-agent in YAML:
```yaml
llms:
  spark_llm:
    _type: openai
    model_name: nemotron-3-super
    base_url: "http://spark-ee7d.local:8000/v1"
```

### Experiment Design

Run the same "authentication patterns for identity services" project on both models and compare:

| Metric | Nano (3B active) | Super (12B active) | Delta |
|--------|------------------|-------------------|-------|
| Research report quality (manual review) | Baseline | ? | |
| PRD completeness (manifest coverage) | Baseline | ? | |
| Design review scores (architect + security) | ~85% | ? | |
| Number of revision rounds | 1-3 | ? | |
| Semi-formal certificate traceability | Baseline | ? | |
| Pipeline total duration | ~22 min | ? | |
| LLM call cost (tokens) | Baseline | ? | |

### Expected Outcome

With 4x active parameters, Super should produce more detailed designs, higher review scores on first pass (fewer revision rounds), and better structured reasoning in semi-formal certificates. The FP4 quantization + speculative decoding should keep latency comparable to Nano despite the larger model.
