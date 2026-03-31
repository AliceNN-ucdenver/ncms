![Multi-Agent Software Delivery Pipeline](assets/banner.svg)

---

One message. Four documents. Fourteen minutes.

A single research prompt enters the pipeline and triggers a deterministic LangGraph chain across six specialized agents. The Researcher runs 5 parallel web searches and synthesizes an 11KB market research report. The Product Owner reads that report, consults the Architect and Security experts in parallel, and produces a 16KB PRD grounded in ADR decisions, STRIDE threat models, CALM governance, OWASP ASVS, NIST 800-63B, PKCE flows, and RS256 signing. The Builder reads the PRD, consults the same experts, and produces a 21KB implementation design. It submits for structured review -- and on Round 1, Architect scored 88%, Security scored 78%, APPROVED at 83% average with no revision needed. During reviews, the Architect retrieved 4 memories (4,792 chars) and Security retrieved 3 memories (2,133 chars) from the shared knowledge store, grounding their structured feedback in actual ADRs and threat models. A 6KB Design Review Report cites ADR-001 (SOA with CALM), ADR-002 (MongoDB), and ADR-003 (JWT with inline RBAC) -- all verified as correctly implemented. Everything runs on a single DGX Spark with 3B active parameters.

The memory layer underneath is NCMS -- vector-free hybrid retrieval combining BM25 (Tantivy/Rust), SPLADE v3 sparse neural expansion, and graph spreading activation. nDCG@10 = 0.7206 on SciFact, exceeding published ColBERTv2 and SPLADE++ baselines. On 850 real GitHub issues from SWE-bench Django, NCMS delivers 6.3x better temporal reasoning than Mem0 and 2.8x better cross-document association than Letta. Zero dense embeddings. Zero external API calls. Everything runs locally.

> **NCMS vs. the field -- SWE-bench Django (850 real GitHub issues)**
>
> | Metric | NCMS | Mem0 | Letta | What it measures |
> |--------|------|------|-------|------------------|
> | **Temporal Reasoning** (nDCG@10) | **0.1217** | 0.0194 | 0.0421 | Finding the right version of a fact over time |
> | **Cross-Document Association** (nDCG@10) | **0.2031** | 0.0614 | 0.0718 | Connecting related information across files |
> | **Recall AR** (nDCG@10) | **0.2031** | 0.0614 | 0.0718 | Overall structured recall quality |
> | **SciFact Retrieval** (nDCG@10) | **0.7206** | -- | -- | Raw retrieval accuracy on scientific claims |
>
> Zero vector databases. Zero embedding API calls. Everything runs locally.

### The Two Insights That Changed Everything

> ***"Don't just tell the agent what to do -- tell it what it knows."***

When we added knowledge-aware prompts that describe what each agent has access to (STRIDE threat models with specific threat IDs for Security, ADRs and CALM specifications for the Architect), the same 3B-active-parameter model went from producing generic responses to citing THR-001, NIST IA-2(1), and OWASP ASVS v5.0 control sections. No model change. No fine-tuning. Just better prompts. The agents always had the capability; they just didn't know they had the knowledge to cite.

> ***"Don't let the LLM decide the workflow -- let the graph enforce it."***

When we replaced open-ended ReAct loops with deterministic LangGraph pipelines, the agents stopped exploring and started executing. Each node in the graph has exactly one job. The LLM generates content; the graph enforces sequence, parallelism, and handoffs. The result: one message produces 4 documents (11KB research, 16KB PRD, 21KB implementation design approved at 83% on round 1), every time, with no dead loops.

## What You Are Building

Six specialized AI agents coordinate through a shared knowledge bus to execute an auto-chaining research-to-design pipeline with a built-in review loop. LangGraph enforces the deterministic workflow for all agents. Bus announcements to `trigger-{agent_id}` domains trigger downstream agents automatically -- each agent's SSE listener detects the trigger and self-calls `/generate` inside its sandbox, with no port forward dependency. A real-time dashboard gives you full visibility into every agent interaction, document artifact, and LLM trace.

| Agent | Type | Pipeline |
|-------|------|----------|
| **Researcher** | LangGraph | plan > search (5x parallel) > synthesize > publish > trigger PO |
| **Product Owner** | LangGraph | read_document > ask_experts (parallel) > synthesize_prd > publish > trigger Builder |
| **Builder** | LangGraph | read_document > ask_experts > synthesize > publish > review loop (revise until 80%+) |
| **Architect** | LangGraph | classify > search_memory > [synthesize_answer \| structured_review] |
| **Security** | LangGraph | classify > search_memory > [synthesize_answer \| structured_review] |
| **Human** | Dashboard UI | Header badge with approval count |

![Multi-Agent Pipeline](assets/multi-agent-pipeline.svg)

### Built With

| Layer | Technology | Detail |
|-------|-----------|--------|
| **Memory** | NCMS | BM25 (Tantivy/Rust) + SPLADE v3 + NetworkX graph -- nDCG@10 = 0.7206 |
| **Orchestration** | LangGraph | Deterministic pipelines for Researcher, PO, Builder |
| **Experts** | LangGraph (dual-mode) | Architect + Security: classify > search_memory > answer or structured_review |
| **LLM** | Nemotron Nano 30B | 256 experts, 3B active params, 512K context on DGX Spark (128GB) |
| **Isolation** | NemoClaw | Kernel-level sandboxes, explicit network policies per agent |
| **Observability** | Phoenix OpenTelemetry | Per-agent tracing of every LLM call and tool invocation |
| **Research** | Tavily | Live web search for Researcher (5 parallel queries) |
| **Dashboard** | NCMS SPA | SSE event feeds, document publishing, agent chat, trace links |

### Four Documents, One Prompt

| Document | Agent | Size | Key References |
|----------|-------|------|---------------|
| Market Research Report | Researcher | 11KB | NIST (3), OAuth, live web sources |
| PRD | Product Owner | 16KB | CALM (7), ADR (4), OWASP (3), NIST (3), JWT (10), RBAC (4), RS256 (2) |
| Implementation Design | Builder | 21KB | JWT (24), TypeScript (3), interface (7), bcrypt |
| Design Review Report | Builder | 6KB | ADR (10), CALM (3), STRIDE, SCORE 88%/78% |

---

## How the Pipeline Works

This is not a chatbot. It is a deterministic software delivery pipeline where LangGraph enforces the workflow for all six agents and the LLM generates content within that structure. Each agent runs in its own kernel-isolated sandbox, communicates through a shared knowledge bus, and produces artifacts that downstream agents consume automatically via bus announcements to `trigger-{agent_id}` domains.

![Pipeline Phases](assets/pipeline-phases.svg)

### Phase 1: Knowledge Seeding

Before any work begins, expert agents load domain knowledge into the NCMS memory store:

- **Architect** seeds ADRs (Architecture Decision Records), CALM model specifications, and quality attribute scenarios
- **Security** seeds STRIDE threat models with specific threat IDs (THR-001, THR-002), OWASP control mappings, and compliance matrices

This knowledge becomes searchable by any agent through the knowledge bus. When downstream agents issue `bus_ask` calls, the experts search the shared store with hybrid retrieval and ground their LLM responses in retrieved facts. This is the foundation that turns generic LLM reasoning into domain-grounded expert responses.

### Phase 2: Research (Researcher LangGraph)

The Researcher agent runs a deterministic LangGraph pipeline: **plan > search (5 parallel) > synthesize > publish > trigger PO**.

When prompted with a research topic, the Researcher:

1. **Plan** -- Generates 5 parallel search queries covering different angles of the topic
2. **Search** -- Executes all 5 Tavily web searches in parallel, collecting results
3. **Synthesize** -- Compiles all search results into a structured market research report
4. **Publish** -- Publishes the report to the hub's document store
5. **Trigger** -- Fires a bus announcement that automatically triggers the Product Owner

**What we observed:** 25 results from 5 parallel Tavily searches. The Researcher produced an 11KB market research report covering NIST standards, OAuth 2.0 PKCE flows, passkey adoption trends, and zero-trust authentication patterns. Completed in approximately 2 minutes. The report included specific citations to NIST SP 800-63B, OAuth 2.0 for Browser-Based Apps, and FIDO2/WebAuthn specifications found via live web search.

### Phase 3: PRD Creation (Product Owner LangGraph)

The Product Owner runs a deterministic LangGraph pipeline: **read_document > ask_experts (parallel) > synthesize_prd > publish > trigger Builder**.

Triggered automatically by the Researcher's bus announcement, the Product Owner:

1. **Read Document** -- Reads the 11KB research report from the document store
2. **Ask Experts (parallel)** -- Issues parallel `bus_ask` calls to the Architect and Security agents simultaneously
3. **Synthesize PRD** -- Compiles research findings and expert input into a structured PRD
4. **Publish** -- Publishes the PRD to the hub's document store
5. **Trigger** -- Fires a bus announcement that automatically triggers the Builder

**What we observed:** The Product Owner read the full 11KB research document, then received expert input grounded in retrieved memories. The Architect provided a 5.6KB answer grounded in 4 retrieved memories, and Security provided a 7.7KB answer grounded in 3 retrieved memories. The resulting 16KB PRD included CALM (7 references), ADR (4), OWASP (3), NIST (3), JWT (10), RBAC (4), and RS256 (2) -- explicit references to ADR-001 through ADR-003, all six STRIDE threat categories with mitigations, CALM model governance patterns, OWASP ASVS v5.0 controls, NIST SP 800-63B authentication assurance levels, PKCE authorization code flows, and RS256 asymmetric JWT signing. This is not generic LLM output -- every reference traces back to seeded knowledge or live web research.

### Phase 4: Implementation Design (Builder LangGraph)

The Builder runs a deterministic LangGraph pipeline: **read_document > ask_experts > synthesize > publish > review loop (revise until 80%+)**.

Triggered automatically by the Product Owner's bus announcement, the Builder:

1. **Read Document** -- Reads the 16KB PRD from the document store
2. **Ask Experts** -- Issues `bus_ask` calls to the Architect and Security agents
3. **Synthesize Design** -- Compiles the PRD and expert input into a TypeScript implementation design
4. **Publish** -- Publishes the initial design document to the hub's document store
5. **Review Loop** -- Sends the design to Architect and Security for structured review. If average score < 80%, revises using explicit feedback and resubmits. Max 5 iterations, then accepts as-is.

**What we observed:** The Builder read the full 16KB PRD, consulted experts (Architect provided a 4KB answer grounded in 2 memories, Security provided a 9.9KB answer grounded in 3 memories), and produced a 21KB implementation design with JWT (24 references), TypeScript (3), interface (7), and bcrypt. It then submitted the design for structured review:

- **Round 1:** Architect scored 88%, Security scored 78% -- average 83%, APPROVED on the first round with no revision needed. During review, Architect retrieved 4 memories (4,792 chars) and Security retrieved 3 memories (2,133 chars), grounding their feedback in actual ADRs and threat models.

The 21KB TypeScript implementation design included actual project structure with `jwt.strategy.ts`, `auth.middleware.ts`, `token.service.ts`, interface definitions, NestJS module organization, per-STRIDE-category mitigations, RS256 signing configuration, PKCE flow implementation, and a three-phase delivery plan. A separate 6KB Design Review Report was published citing ADR-001 (SOA with CALM), ADR-002 (MongoDB), ADR-003 (JWT with inline RBAC), with SCORE/COVERED/MISSING/CHANGES structure, all verified as correctly implemented in the design.

### Expert Agent Pipeline (Architect & Security)

The Architect and Security agents are now LangGraph pipelines with a dual-mode design based on the Looking Glass governance framework from maintainability.ai (Oraculum architecture review):

1. **Classify** -- String matching determines if the incoming request is a knowledge question or a design review
2. **Search Memory** -- NCMS recall with domain filtering (Architect gets ADRs/CALM, Security gets STRIDE/OWASP)
3. **Two synthesis paths:**
   - **Knowledge answer** -- Cites specific ADRs, threat IDs, NIST controls, OWASP sections
   - **Structured review** -- Returns SCORE (0-100), SEVERITY, COVERED, MISSING, CHANGES

**Architecture reviews evaluate:** CALM model compliance, ADR compliance, fitness functions, quality attributes, component boundaries.

**Security reviews evaluate:** OWASP Top 10, STRIDE threat compliance, security controls, secrets management, transport security.

### Phase 5: Observability

Every agent generates Phoenix OpenTelemetry traces throughout the pipeline. The NCMS dashboard provides full visibility:

- Real-time **SSE event feeds** on each agent card showing bus announcements and document publications
- **Document publishing** notifications in the sidebar with the full chain visible (Research Report > PRD > Implementation Design)
- **Phoenix trace links** for debugging LLM reasoning at each pipeline node
- **Bus announcement logs** showing the fire-and-forget triggers that chain the pipeline
- **Direct chat** with any agent from the dashboard for ad-hoc queries

All agents generated complete traces during the test run -- full visibility into every LLM call, tool invocation, knowledge bus interaction, and inter-agent handoff.

---

## Architecture

![Architecture Context](assets/architecture-context.svg)

### LLM Infrastructure

- **Model:** Nemotron Nano 30B, a mixture-of-experts model with 256 experts and only 3B active parameters per inference pass. The model supports up to 1M context tokens.
- **Serving:** NGC vLLM container on a DGX Spark (128GB memory). Configured with `--max-model-len 524288` (512K context) via `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`. KV cache usage under 0.5%, leaving massive headroom. Model supports up to 1M via RoPE scaling.
- **Tool call parser:** `--tool-call-parser qwen3_coder` is the correct parser for Nemotron Nano's `<tool_call><function=name>` format. Other parsers (including `hermes`) silently fail, causing agents to loop without acting. This was the single most important configuration discovery.
- **Output tokens:** `max_tokens: 32768` in agent configs enables rich, detailed output. The 512K context window provides ample room for the prompt, retrieved knowledge, and generation.
- **Direct Spark URL:** LangGraph agents connect directly to the DGX Spark at `spark-ee7d.local:8000` rather than through the NemoClaw `inference.local` proxy. The proxy imposes a 60-second timeout that is insufficient for large document synthesis. Direct connection eliminates this bottleneck.
- **Thinking mode off:** `enable_thinking: false` keeps thinking-mode tokens from consuming the context budget. Valuable for open-ended reasoning, but wasteful in structured pipelines where the agent's job is to call specific tools in sequence.
- **Pipeline orchestration:** LangGraph enforces deterministic workflows for all six agents. The Architect and Security experts use dual-mode LangGraph pipelines (classify > search_memory > answer or structured_review).
- **Handoff mechanism:** Triggers use `bus_announce` to domain `trigger-{agent_id}` instead of HTTP proxy calls. Each agent's SSE listener detects the trigger announcement and self-calls `/generate` inside its own sandbox. No port forward dependency. No polling. No shared state. No orchestrator.

### Agent Sandboxing

- **Isolation:** Each agent runs in its own NemoClaw kernel-isolated k3s pod, fully network-isolated
- **Proxy:** All outbound traffic routes through the OpenShell proxy at `10.200.0.1:3128`
- **LLM routing:** All agents are LangGraph pipelines. LangGraph agents bypass NemoClaw's `inference.local` proxy to avoid the 60-second timeout, connecting directly to `spark-ee7d.local:8000`.
- **Secrets:** External API keys (e.g. `TAVILY_API_KEY`) are injected via OpenShell providers, not hardcoded in config files. Only the Researcher sandbox receives the Tavily provider.
- **Network policies:** Hub connections, PyPI, and HuggingFace access require explicit rules in `policies/openclaw-sandbox.yaml`. Private IP endpoints require interactive approval via `openshell term`.

### Shared Memory Layer

- **NCMS** provides persistent shared memory across all agents via an HTTP API on `:9080`
- **Hybrid retrieval:** BM25 (Tantivy/Rust) for lexical precision + SPLADE v3 for semantic expansion + NetworkX graph spreading activation. No dense vectors, no embedding API calls.
- **Integration:** Expert agents use NCMS recall with domain filtering. When a pipeline agent issues a `bus_ask`, the domain expert searches the shared store with hybrid retrieval and grounds its LLM response in retrieved facts.
- **Knowledge seeding:** Expert agents load curated domain knowledge at startup:
  - `knowledge/architecture/` -- ADRs, CALM model specifications
  - `knowledge/security/` -- STRIDE threat models, OWASP control mappings
  - Researcher, Product Owner, and Builder have no knowledge files; they learn by querying experts and consuming documents

---

## Getting Started

For the complete step-by-step setup, configuration reference, testing guide, and troubleshooting, see the **[Setup & Configuration Guide](nemoclaw-nat-step-by-step.md)**.

Quick summary: configure your DGX Spark inference provider, deploy vLLM with 512K context and `qwen3_coder` tool-call parser, run `./setup_nemoclaw.sh`, and send your first research prompt. The full pipeline runs autonomously from there.

---


## What's Next

The auto-chaining pipeline runs end-to-end with a review loop. These are the next areas of investment:

### 1M Context

The pipeline currently uses 512K context (KV cache under 0.5%). Nemotron Nano supports up to 1M tokens via `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` with RoPE scaling. Pushing to 1M would enable feeding the full research report, PRD, all expert consultations, and multiple revision rounds into a single Builder context without any truncation.

### Nemotron Super 120B

**Nemotron Super 120B-A12B-NVFP4** is the next model experiment. At 120B total / 12B active parameters (4x Nano's active compute) with NVIDIA's FP4 quantization, it fits on the DGX Spark (~60GB model, ~68GB for KV cache). Expected improvements: richer expert consultations, more detailed implementation designs, and more reliable multi-step reasoning. Requires NVIDIA driver 590+ for NVFP4 support. The vLLM command:

```bash
sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-super \
  -p 8000:8000 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --max-model-len 262144 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
```

| Model | Active Params | Memory (FP4/FP8) | KV Cache | Fit on 128GB Spark? |
|-------|--------------|-------------------|----------|-------------------|
| Nemotron Nano 30B (current) | 3B | ~15GB | ~113GB | Easy |
| **Nemotron Super 120B-A12B** | **12B** | **~60GB** | **~68GB** | **Yes -- next experiment** |
| Qwen 3.5 Coder 32B | 32B (dense) | ~32GB (FP8) | ~96GB | Yes |

### Coding Agent

A seventh agent that takes the Builder's 21KB TypeScript implementation design and generates actual source code, tests, and Dockerfiles. The design documents are already structured for this -- the coding agent would consume them as specifications. The project structure, interface contracts, and module organization are explicit enough to drive code generation directly.

### Looking Glass Full Oraculum Integration

The current expert review pipeline uses a simplified version of the Looking Glass governance framework. The next step is full Oraculum integration for 4-pillar governance reviews (architecture, security, operations, compliance) with richer scoring rubrics and cross-pillar dependency analysis.

### Platform Capabilities

- **Human approval workflows** -- The dashboard supports agent chat, but structured approve/reject/modify flows for PRDs and designs are not yet wired up. This is the path to human-in-the-loop governance.
- **CrewAI StorageBackend integration** -- CrewAI defines 14 storage methods vs NAT's 3. An NCMS StorageBackend would let CrewAI agents use the same shared memory, opening the platform to a second agent framework.
- **Automated proxy approval** -- NemoClaw issue #326 tracks the request for static policy to work with private IPs without interactive approval. When that lands, the setup becomes truly one-command with zero manual steps.
- **Multi-pipeline orchestration** -- Running multiple pipelines concurrently (e.g., identity service + payment service) with cross-pipeline knowledge sharing through the bus.
- **Production hardening** -- mTLS between agents and hub, secret rotation for API keys, pod-level resource limits, and horizontal scaling of the hub.
