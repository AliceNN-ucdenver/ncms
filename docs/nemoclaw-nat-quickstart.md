![Multi-Agent Software Delivery Pipeline](assets/banner.svg)

---

We asked a 3-billion-parameter model to do the job of a software delivery team. It did.

**One message. Four documents. Fourteen minutes.**

A single research prompt enters the pipeline and triggers a deterministic LangGraph chain across five specialized agents. The Archeologist runs five parallel web searches and synthesizes a market research report (or, for archaeology projects, analyzes a GitHub repository). The Product Owner reads that report, consults the Architect and Security experts in parallel, and produces a PRD grounded in ADR decisions, STRIDE threat models, CALM governance, OWASP ASVS, NIST 800-63B, PKCE flows, and RS256 signing. The Designer reads the PRD, consults the same experts, and produces a TypeScript implementation design. It submits the design for structured review with a quality gate that iterates until the average score meets the threshold.

During reviews, the Architect and Security experts retrieve governance knowledge from the shared NCMS memory store — ADRs, STRIDE threat models, CALM quality attributes — and produce structured reviews with SCORE/SEVERITY/COVERED/MISSING/CHANGES. Every review score, LLM call, grounding citation, and bus conversation is persisted in the audit database. This is not a demo. These are grounded, auditable engineering artifacts produced by a model that fits on a desk.

The memory layer underneath is NCMS: vector-free hybrid retrieval combining BM25 (Tantivy/Rust), SPLADE v3 sparse neural expansion, and graph spreading activation. It achieves nDCG@10 = 0.7206 on SciFact, exceeding published ColBERTv2 and SPLADE++ baselines. On 850 real GitHub issues from SWE-bench Django, NCMS delivers 6.3x better temporal reasoning than Mem0 and 2.8x better cross-document association than Letta. Zero dense embeddings. Zero external API calls. Everything runs locally.

> **NCMS vs. the field: SWE-bench Django (850 real GitHub issues)**
>
> | Metric | NCMS | Mem0 | Letta | What it measures |
> |--------|------|------|-------|------------------|
> | **Temporal Reasoning** (nDCG@10) | **0.1217** | 0.0194 | 0.0421 | Finding the right version of a fact over time |
> | **Cross-Document Association** (nDCG@10) | **0.2031** | 0.0614 | 0.0718 | Connecting related information across files |
> | **Recall AR** (nDCG@10) | **0.2031** | 0.0614 | 0.0718 | Overall structured recall quality |
> | **SciFact Retrieval** (nDCG@10) | **0.7206** | — | — | Raw retrieval accuracy on scientific claims |
>
> Zero vector databases. Zero embedding API calls. Everything runs locally.

### The Three Insights That Changed Everything

> ***"Don't just tell the agent what to do. Tell it what it knows."***

When we added knowledge-aware prompts describing what each agent has access to (STRIDE threat models with specific threat IDs for Security, ADRs and CALM specifications for the Architect), the same 3B-active-parameter model went from producing generic responses to citing THR-001, NIST IA-2(1), and OWASP ASVS v5.0 control sections. No model change. No fine-tuning. Just better prompts. The agents always had the capability. They just didn't know they had the knowledge to cite.

> ***"Don't let the LLM decide the workflow. Let the graph enforce it."***

When we replaced open-ended ReAct loops with deterministic LangGraph pipelines, the agents stopped exploring and started executing. Each node in the graph has exactly one job. The LLM generates content. The graph enforces sequence, parallelism, and handoffs. One message produces four documents, every time, with no retries and no dead loops.

> ***"Don't let the LLM skip steps. Make it fill in a certificate."***

Adapted from Meta's "Agentic Code Reasoning" (arXiv:2603.01896), we replaced free-form RCTRO prompts with **semi-formal certificate templates** that force the LLM to state explicit source premises, trace evidence through cross-source analysis with confidence ratings, identify evidence gaps, and derive formal conclusions before making recommendations. In an 8-way experiment (standard/semi-formal x CoT-on/off x 1-stage/2-stage) with real Tavily web search and ArXiv academic papers, the semi-formal+CoT configuration scored **53/60** vs the standard baseline at **41/60** — a 29% improvement in traceability, coverage, and grounding. The Archeologist (research path) and Product Owner now use semi-formal certificates with CoT reasoning enabled. The Designer keeps standard prompts where implementation actionability matters more than audit traceability.

## What You Are Building

Five specialized AI agents coordinate through a shared knowledge bus to execute an auto-chaining research-to-design pipeline with a built-in quality review loop. The **Archeologist** is the dual-path entry point: for research projects it searches the web (Tavily + ArXiv); for archaeology projects it analyzes an existing GitHub repository. Both paths produce a grounded research report that the Product Owner consumes to generate a PRD.

LangGraph enforces the deterministic workflow for all agents. Bus announcements to `trigger-{agent_id}` domains trigger downstream agents automatically. Each agent's SSE listener detects the trigger and self-calls `/generate` inside its sandbox, with no port forward dependency and no orchestrator.

A "New Project" button in the dashboard creates a `PRJ-XXXXXXXX` identifier that propagates through every agent in the chain. A "+ Start Archaeology" option lets you browse GitHub repositories and start from existing code. Each document, telemetry event, and review score links back to the originating project. Published documents are enriched with GLiNER entity extraction at publish time, and entity metadata persists across hub restarts via JSON sidecar files.

| Agent | Type | Pipeline |
|-------|------|----------|
| **Archeologist** | LangGraph (dual-path) | check_guardrails → [research: plan → search → arxiv → patents → community → synthesize \| archaeology: clone → analyze → gaps → web_research → synthesize] → publish → trigger PO |
| **Product Owner** | LangGraph | check_guardrails → read_doc → ask_experts (parallel) → synthesize_prd → generate_manifest → publish → trigger Designer |
| **Designer** | LangGraph | check_guardrails → read_doc → ask_experts → synthesize → validate → output_guard → publish → review ⟲ → verify |
| **Architect** | LangGraph | classify → search_memory → [synthesize_answer \| structured_review] |
| **Security** | LangGraph | classify → search_memory → [synthesize_answer \| structured_review] |

![Multi-Agent Pipeline](assets/multi-agent-pipeline.svg)

### Built With

| Layer | Technology | Detail |
|-------|-----------|--------|
| **Memory** | NCMS | BM25 (Tantivy/Rust) + SPLADE v3 + NetworkX graph, nDCG@10 = 0.7206 |
| **Orchestration** | LangGraph | Deterministic pipelines for Archeologist, PO, Designer, Experts |
| **Experts** | LangGraph (dual-mode) | Architect + Security: classify → search_memory → answer or structured_review |
| **Codebase Analysis** | GitHub REST API | Archeologist agent: repo tree, file content, dependencies, commits, issues via PAT |
| **Document Intelligence** | GLiNER NER | Entity extraction at publish time, JSON sidecar persistence, entity-enriched expert search |
| **LLM** | Nemotron Nano 30B | 256 experts, 3B active params, 512K context on DGX Spark (128GB). Dual LLM: standard + thinking-enabled. |
| **Isolation** | NemoClaw | Kernel-level sandboxes, explicit network policies per agent |
| **Observability** | Phoenix OpenTelemetry | Per-agent tracing with `_type: nim` (ChatNVIDIA) for full LLM span capture |
| **Research** | Tavily + ArXiv + USPTO + HN | Web search (5 parallel) + academic papers + patent landscape + community sentiment |
| **Dashboard** | NCMS SPA | D3 doc flow graph, audit timeline, login page, guardrail strips, compliance scores |
| **Guardrails** | NemoGuardrails | Policy enforcement with human-in-the-loop approval gates (block/reject → pause for approve/deny) |
| **Audit** | SQLite (V8) | 13 tables, tamper-evident hash chains, JWT auth, provenance certificates, compliance scoring |
| **Spec Quality** | Python + LLM | Completeness validator (10 checks), requirements manifest, deterministic [NCMS:review]/[NCMS:question] routing |
| **Project Management** | NCMS Hub | Project lifecycle, pipeline progress, interrupt, prompt editor, policy editor |

### Four Documents, One Prompt

| Document | Agent | Size | Key References |
|----------|-------|------|---------------|
| Market Research Report | Archeologist | 11KB | NIST (3), OAuth, live web sources |
| PRD | Product Owner | 16KB | CALM (7), ADR (4), OWASP (3), NIST (3), JWT (10), RBAC (4), RS256 (2) |
| Implementation Design | Designer | 21KB | JWT (24), TypeScript (3), interface (7), bcrypt |
| Design Review Report | Designer | 6KB | ADR (10), CALM (3), STRIDE, SCORE 88%/78% |

---

## How the Pipeline Works

This is not a chatbot. It is a deterministic software delivery pipeline where LangGraph enforces the workflow for all five agents and the LLM generates content within that structure. Each agent runs in its own kernel-isolated sandbox, communicates through a shared knowledge bus, and produces artifacts that downstream agents consume automatically.

![Pipeline Phases](assets/pipeline-phases.svg)

### Phase 1: Knowledge Seeding

Before any work begins, expert agents load domain knowledge into the NCMS memory store:

- **Architect** seeds ADRs (Architecture Decision Records), CALM model specifications, and quality attribute scenarios
- **Security** seeds STRIDE threat models with specific threat IDs (THR-001, THR-002), OWASP control mappings, and compliance matrices

This knowledge becomes searchable by any agent through the knowledge bus. When downstream agents issue `bus_ask` calls, the experts search the shared store with hybrid retrieval and ground their LLM responses in retrieved facts. This is the foundation that turns generic LLM reasoning into domain-grounded expert responses.

### Phase 2: Research

The Archeologist's research path runs a seven-node LangGraph pipeline: **plan → search (5 parallel) → arxiv → patents (USPTO) → community (HackerNews) → synthesize → publish → trigger PO**.

1. **Plan** generates five parallel search queries covering different angles of the topic
2. **Search** executes all five Tavily web searches concurrently, collecting results
3. **ArXiv** searches academic papers from the last 12 months
4. **Patents** searches USPTO for related patents in the space (free API, no key)
5. **Community** searches HackerNews for developer discussion and sentiment (free API)
6. **Synthesize** compiles all sources into a structured report with Jobs-to-be-Done analysis, patent landscape, whitespace analysis, and semi-formal certificate format
7. **Publish** posts the report to the hub's document store, **Trigger** starts the Product Owner

> **Observed:** 25 results from 5 parallel Tavily searches. The Archeologist produced an 11KB market research report covering NIST standards, OAuth 2.0 PKCE flows, passkey adoption trends, and zero-trust authentication patterns. Completed in approximately 2 minutes, with citations to NIST SP 800-63B, OAuth 2.0 for Browser-Based Apps, and FIDO2/WebAuthn specifications.

### Phase 3: PRD Creation

The Product Owner runs a five-node LangGraph pipeline: **read_document → ask_experts (parallel) → synthesize_prd → publish → trigger Designer**.

Triggered automatically by the Archeologist's bus announcement:

1. **Read Document** fetches the 11KB research report from the document store
2. **Ask Experts** issues parallel `bus_ask` calls to the Architect and Security agents simultaneously
3. **Synthesize PRD** compiles research findings and expert input into a structured PRD
4. **Publish** posts the PRD to the hub's document store
5. **Trigger** fires a bus announcement that automatically starts the Designer

> **Observed:** The Architect provided a 5.6KB answer grounded in 4 retrieved memories. Security provided a 7.7KB answer grounded in 3 retrieved memories. The resulting 16KB PRD included CALM (7 references), ADR (4), OWASP (3), NIST (3), JWT (10), RBAC (4), and RS256 (2). Every reference traces back to seeded knowledge or live web research.

### Phase 4: Implementation Design with Review Loop

The Designer runs a LangGraph pipeline with a conditional review loop: **read_document → ask_experts → synthesize → validate → output_guard → publish → review → [revise → publish → review ...] → verify**.

Triggered automatically by the Product Owner's bus announcement:

1. **Read Document** fetches the 16KB PRD from the document store
2. **Ask Experts** consults Architect and Security agents
3. **Synthesize Design** produces a TypeScript implementation design from the PRD and expert input
4. **Publish** posts the initial design to the document store
5. **Request Review** sends the design to both experts for structured scoring (SCORE/SEVERITY/COVERED/MISSING/CHANGES)
6. **Conditional:** if average score ≥ 80%, proceed to verify. If below, revise with explicit feedback and resubmit (max 5 iterations)
7. **Verify** publishes a Design Review Report and announces completion

> **Observed:** The Designer produced a 21KB implementation design with JWT (24 references), TypeScript (3), interface (7), and bcrypt. Round 1 review: Architect 88%, Security 78%, approved at 83% average with no revision needed. During review, the Architect retrieved 4 memories (4,792 chars) and Security retrieved 3 memories (2,133 chars), grounding their scores in actual ADRs and threat models. The 6KB Design Review Report cites ADR-001 (SOA with CALM), ADR-002 (MongoDB), and ADR-003 (JWT with inline RBAC) as correctly implemented.

### Expert Agents: Dual-Mode LangGraph

The Architect and Security agents run LangGraph pipelines with a dual-mode design based on the Looking Glass governance framework from [maintainability.ai](https://github.com/AliceNN-ucdenver/MaintainabilityAI) (Oraculum architecture review):

1. **Classify** determines if the incoming request is a knowledge question or a design review
2. **Search Memory** retrieves relevant knowledge from NCMS with domain filtering (Architect gets ADRs and CALM specs, Security gets STRIDE and OWASP controls)
3. **Two synthesis paths:**
   - **Knowledge answer** cites specific ADRs, threat IDs, NIST controls, and OWASP sections
   - **Structured review** returns SCORE (0-100), SEVERITY, COVERED, MISSING, and CHANGES

Architecture reviews evaluate CALM model compliance, ADR compliance, fitness functions, quality attributes, and component boundaries. Security reviews evaluate OWASP Top 10, STRIDE threat compliance, security controls, secrets management, and transport security.

### Phase 5: Observability

Every agent generates Phoenix OpenTelemetry traces throughout the pipeline:

- Real-time SSE event feeds on each agent card showing bus announcements and document publications
- Document publishing notifications in the sidebar with the full chain visible
- Phoenix trace links for debugging LLM reasoning at each pipeline node
- Bus announcement logs showing the fire-and-forget triggers that chain the pipeline
- Direct chat with any agent from the dashboard for ad-hoc queries

All agents generated complete traces during the test run. Full visibility into every LLM call, tool invocation, knowledge bus interaction, and inter-agent handoff.

---

## Architecture

![Architecture Context](assets/architecture-context.svg)

### LLM Infrastructure

- **Model:** Nemotron Nano 30B, a mixture-of-experts model with 256 experts and only 3B active parameters per inference pass. Supports up to 1M context tokens.
- **Serving:** NGC vLLM container on a DGX Spark (128GB memory), configured with 512K context via `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`. KV cache usage under 0.5%, with massive headroom.
- **Tool call parser:** `qwen3_coder` is the correct parser for Nemotron Nano's `<tool_call><function=name>` format. Other parsers (including `hermes`) silently fail, causing agents to loop without acting. This was the single most important configuration discovery.
- **Reasoning parser:** `nano_v3_reasoning_parser.py` plugin (from the model's HuggingFace repo) mounted into the container and activated via `--reasoning-parser-plugin`. Handles `<think>` tags properly so thinking tokens don't leak into output content. Enables the 4-way experiment matrix: standard/semi-formal prompts x thinking-on/thinking-off.
- **Output tokens:** `max_tokens: 131072` in agent configs provides ample room for CoT reasoning + content.
- **Direct Spark URL:** LangGraph agents connect directly to the DGX Spark at `spark-ee7d.local:8000`, bypassing the NemoClaw `inference.local` proxy which imposes a 60-second timeout insufficient for large document synthesis.
- **Dual LLM config:** Each agent defines `spark_llm` (thinking off) and `spark_llm_thinking` (thinking on). The Archeologist's synthesis and PO's PRD synthesis use the thinking LLM for chain-of-thought reasoning. All other nodes use the standard LLM. `_type: nim` uses ChatNVIDIA which natively handles `chat_template_kwargs` and preserves `reasoning_content`.
- **Pipeline orchestration:** LangGraph enforces deterministic workflows for all five agents. The Architect and Security experts use dual-mode pipelines (classify → search_memory → answer or structured_review).
- **Handoff mechanism:** Triggers use `bus_announce` to domain `trigger-{agent_id}`. Each agent's SSE listener detects the trigger and self-calls `/generate` inside its own sandbox. No port forward dependency. No polling. No shared state. No orchestrator.

### Agent Sandboxing

- **Isolation:** Each agent runs in its own NemoClaw kernel-isolated k3s pod, fully network-isolated
- **Proxy:** All outbound traffic routes through the OpenShell proxy at `10.200.0.1:3128`
- **LLM routing:** LangGraph agents bypass the NemoClaw inference proxy to avoid the 60-second timeout, connecting directly to `spark-ee7d.local:8000`
- **Secrets:** External API keys (e.g., `TAVILY_API_KEY`) are injected via OpenShell providers, not hardcoded. Only the Archeologist sandbox receives the Tavily provider (for both research and archaeology paths).
- **Network policies:** Hub connections, PyPI, and HuggingFace access require explicit rules in `policies/openclaw-sandbox.yaml`

### Shared Memory Layer

- **NCMS** provides persistent shared memory across all agents via an HTTP API on `:9080`
- **Hybrid retrieval:** BM25 (Tantivy/Rust) for lexical precision + SPLADE v3 for semantic expansion + NetworkX graph spreading activation. No dense vectors, no embedding API calls.
- **Integration:** Expert agents use NCMS recall with domain filtering. When a pipeline agent issues a `bus_ask`, the domain expert searches the shared store with hybrid retrieval and grounds its LLM response in retrieved facts.
- **Knowledge seeding:** Expert agents load curated domain knowledge at startup from `knowledge/architecture/` (ADRs, CALM specs) and `knowledge/security/` (STRIDE threat models, OWASP controls). The Archeologist, Product Owner, and Designer have no knowledge files. They learn by querying experts and consuming documents.

### Pipeline Infrastructure

- **Lightweight telemetry channel.** A dedicated `POST /api/v1/pipeline/events` endpoint relays per-node status events via SSE without storing them as memories. Each LangGraph node calls this endpoint at entry and exit. The dashboard subscribes to `pipeline.node` events for real-time progress visualization. This is deliberately separate from the knowledge bus, which is reserved for meaningful events like document publications and review scores.
- **Pipeline interrupt.** Every pipeline node checks for interrupt signals between steps. The dashboard progress bar has clickable interrupt buttons. When interrupted, remaining nodes skip and the progress bar shows amber stop icons. The interrupt flag is consumed on read (single-fire).
- **Document intelligence.** Every published document is enriched with GLiNER entity extraction (20 entities per document). The hub seeds a `software` domain with 10 labels tuned for software delivery: `framework`, `database`, `protocol`, `standard`, `threat`, `pattern`, `security_control`, `api_endpoint`, `data_model`, `architecture_decision`. These layer on top of 10 universal labels for 20 total extraction targets. Metadata persists as JSON sidecar files alongside the markdown, surviving hub restarts. Expert agents use entity keywords from document sidecars for targeted NCMS memory retrieval instead of raw text excerpts.
- **Guardrails as pipeline bookends.** Every pipeline agent starts with a `check_guardrails` node that validates topics against domain and technology policies stored in the hub. The Designer additionally runs `check_output_guardrails` before publishing, scanning for hardcoded secrets and prohibited patterns. Policies are versioned documents editable from the dashboard's policy editor.
- **Prompt extraction.** Agent prompts are extracted into separate `*_prompts.py` modules (`research_prompts.py`, `prd_prompts.py`, `design_prompts.py`, `expert_prompts.py`, `archeologist_prompts.py`). The hub provides a prompt store API and the dashboard includes a prompt editor for versioning and editing prompts without rebuilding sandboxes.
- **Policy storage.** Guardrails policies (domain scope, technology scope, compliance requirements) are stored via the hub's policy API and managed from the dashboard policy editor. Agents load policies at pipeline start.

---

## Getting Started

For the complete step-by-step setup, configuration reference, testing guide, and troubleshooting, see the **[Setup & Configuration Guide](nemoclaw-nat-step-by-step.md)**.

Quick summary: configure your DGX Spark inference provider, deploy vLLM with 512K context and `qwen3_coder` tool-call parser, run `./setup_nemoclaw.sh`, and send your first research prompt. The full pipeline runs autonomously from there.

### Important Configuration Notes

**NAT auto_memory_wrapper**: Agents that use the NAT auto_memory_wrapper (archeologist, designer, product_owner) must set `save_ai_messages_to_memory: false` and `save_user_messages_to_memory: false` in their config. Otherwise the wrapper stores every conversation turn as a `default_user` memory, creating duplicates of content already stored by the agent's own structured pipeline. The expert agents (architect, security) use custom LangGraph workflows and are not affected.

**Document ingestion with domains**: All `publish_document()` calls must include a `domains` parameter. Documents stored without domains are invisible to domain-scoped search (agents search with `domain=security`, `domain=architecture`, etc.). Knowledge files loaded via `register.py` and agent-published documents (PRD, design, research reports) all pass domains. If you add a new agent or document type, ensure domains are included.

**Content-hash deduplication**: NCMS deduplicates at the content level via SHA-256 hash. Storing the same content twice returns the existing memory instead of creating a new one. This is expected behavior, not an error.

---

## Document Intelligence (Phase 2.5)

The pipeline is backed by a full audit and provenance system. Every artifact has a persistent identity, a version history, a quality score, and a traceability chain back to the original research.

### What's Auditable

| Audit Surface | How It's Captured |
|---------------|------------------|
| **Document provenance** | SHA-256 content hashes, typed links (derived_from, reviews, supersedes), version chains via parent_doc_id |
| **Review scores** | Per-reviewer, per-round scores persisted in review_scores table. Quality score on project card. |
| **LLM calls** | Every LLM invocation: prompt/response sizes, model, duration, prompt hash. Queryable per project. |
| **Agent configurations** | Model name, thinking mode, max_tokens captured at pipeline start per agent. |
| **Bus conversations** | Every ask/respond pair: from/to agent, question/answer previews, confidence, duration. |
| **Knowledge grounding** | Every memory citation used in expert reviews: document → memory_id with retrieval score and entity query. |
| **Guardrail violations** | Every policy finding: type, rule, message, escalation level, overridden status. |
| **Approval decisions** | Human approve/deny with authenticated identity (JWT), comment, timestamp. |
| **Tamper evidence** | SHA-256 hash chain on all audit tables — each record references the prior record's hash. |

### Dashboard Features

- **D3 document flow graph** — left-to-right DAG showing Research → PRD → Design → Review with typed arrows, per-reviewer score bars, and version stacking
- **Audit timeline tab** — chronological table of all events (pipeline, LLM calls, reviews, guardrails, approvals, bus conversations) filterable by type and agent
- **Guardrail violation strip** — auto-visible below pipeline progress when violations are flagged
- **Quality score bar** — aggregate review score at top of project detail
- **Human-in-the-loop approval gates** — agents pause at guardrail blocks, dashboard shows approve/deny
- **Pipeline interrupt** — clickable stop on any running node, marks project as interrupted
- **Document version diff** — compare previous version in the document viewer
- **Provenance certificate** — GET /documents/{id}/provenance returns complete lineage, reviews, grounding, LLM calls, integrity check
- **Compliance score** — composite from review averages, violation penalties, grounding coverage, approval gates, document completeness
- **Login page** — JWT authentication with bcrypt passwords, approver identity from token

### Thinking Mode (Chain-of-Thought)

The Archeologist's research synthesis and the Product Owner's PRD synthesis use **Chain-of-Thought reasoning** via a dedicated thinking-enabled LLM (`spark_llm_thinking` with `enable_thinking: true`). The `_type: nim` config uses ChatNVIDIA which natively handles `chat_template_kwargs` and preserves `reasoning_content`. The Designer and expert agents use the standard LLM without thinking for structured output.

Pipeline progress nodes with thinking enabled show a 🧠 icon.

---

## What's Next

For the complete feature roadmap, see [dashboard-evolution-design.md](dashboard-evolution-design.md) and [document-intelligence-design.md](document-intelligence-design.md).

### Immediate (Designed, Ready to Implement)

- **Project lifecycle completion.** Auto-transition projects from "active" to "completed" when the Designer's verify passes. Currently only interrupt/denial update status.
- **Audit export.** Download a project's full provenance chain as a portable markdown report for compliance handoffs and offline review.
- **Quality trend analytics.** Portfolio-level dashboard: review scores over time, common violations, LLM cost trends across projects.

### Phase 3: Coding Agent and Governance

- **Coding agent (Claude Code).** A sixth agent receives the approved design and produces working implementations in a NemoClaw sandbox with test-fix-retry loops.
- **Code-to-design feedback loop.** When the coding agent discovers the design is unimplementable, structured feedback flows back to the Designer.
- **Compliance dashboard.** Aggregate governance visibility. ADR matrix, STRIDE heat map, drift scores.

### LLM Upgrade Experiment

| Model | Active Params | Quantization | Fit on 128GB Spark? |
|-------|--------------|-------------|-------------------|
| Nemotron Nano 30B (current) | 3B | FP16 | Easy |
| **Nemotron Super 120B-A12B** | **12B** | **NVFP4** | **Yes — next experiment** |

The Super model (4x active parameters) with NVFP4 quantization, MTP speculative decoding, and 1M context is designed and ready to deploy. See the [experiment design](document-intelligence-design.md#experiment-nemotron-3-super-120b-12b-active) for full vLLM deployment commands.

### Looking Glass Governance Mesh

The full [Oraculum](https://github.com/AliceNN-ucdenver/MaintainabilityAI) integration would connect to the governance mesh via MCP servers, pulling BAR artifacts for each application: CALM architecture models, STRIDE threat models, ADRs, fitness functions, compliance checklists. This transforms reviews from "does the design look reasonable" to "does the design comply with the documented governance baseline."

### Platform

- **Template library.** Reusable design fragments from high-scoring runs.
- **Knowledge lifecycle management.** Hot-reload knowledge without rebuilding sandboxes. Versioned knowledge with reconciliation.
- **Production hardening.** mTLS, secret rotation, pod resource limits, horizontal hub scaling.
