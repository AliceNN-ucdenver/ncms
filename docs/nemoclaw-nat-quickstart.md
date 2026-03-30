![Multi-Agent Software Delivery Pipeline](assets/banner.svg)

---

One message. Four documents. Ten minutes.

A single research prompt enters the pipeline and triggers a deterministic LangGraph chain across six specialized agents. The Researcher runs 5 parallel web searches and synthesizes an 11KB market research report. The Product Owner reads that report, consults the Architect and Security experts in parallel, and produces a 7.6KB PRD grounded in ADR decisions, STRIDE threat models, CALM governance, OWASP ASVS, NIST 800-63B, PKCE flows, and RS256 signing. The Builder reads the PRD, consults the same experts, produces an initial implementation design, then submits it for structured review. Round 1: Architect scored 78%, Security scored 72% -- below the 80% threshold. The Builder revised, addressing each reviewer's feedback explicitly. Round 2: Architect 85%, Security 92% -- APPROVED at 89% average. The final 27.8KB TypeScript implementation design includes actual project structure, `jwt.strategy.ts`, `auth.middleware.ts`, interface contracts, per-STRIDE-category mitigations, and revision markers. Everything runs on a single DGX Spark with 3B active parameters.

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

When we replaced open-ended ReAct loops with deterministic LangGraph pipelines, the agents stopped exploring and started executing. Each node in the graph has exactly one job. The LLM generates content; the graph enforces sequence, parallelism, and handoffs. The result: 4 LLM calls produce 42KB of grounded documentation, every time, with no retries and no dead loops.

## What You Are Building

Five specialized AI agents coordinate through a shared knowledge bus to execute an auto-chaining research-to-design pipeline. LangGraph enforces the deterministic workflow. Fire-and-forget bus announcements trigger downstream agents automatically. A real-time dashboard gives you full visibility into every agent interaction, document artifact, and LLM trace.

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
| **LLM** | Nemotron Nano 30B | 256 experts, 3B active params, 256K context on DGX Spark (128GB) |
| **Isolation** | NemoClaw | Kernel-level sandboxes, explicit network policies per agent |
| **Observability** | Phoenix OpenTelemetry | Per-agent tracing of every LLM call and tool invocation |
| **Research** | Tavily | Live web search for Researcher (5 parallel queries) |
| **Dashboard** | NCMS SPA | SSE event feeds, document publishing, agent chat, trace links |

### Four Documents, One Prompt

| Document | Agent | Size | Key Details |
|----------|-------|------|-------------|
| Market Research Report | Researcher | 11KB | 5 parallel Tavily searches, 25 results synthesized |
| PRD | Product Owner | 7.6KB | Grounded in research + architect/security expert input |
| Implementation Design v1 | Builder | 11.6KB | Initial design from PRD + expert consultation |
| Implementation Design v2 | Builder | 27.8KB | Revised after review, approved at 89% (Architect 85%, Security 92%) |

A separate Design Review Report is also published with both reviewers' scores and structured feedback.

---

## How the Pipeline Works

This is not a chatbot. It is a deterministic software delivery pipeline where LangGraph enforces the workflow and the LLM generates content within that structure. Each agent runs in its own kernel-isolated sandbox, communicates through a shared knowledge bus, and produces artifacts that downstream agents consume automatically via fire-and-forget bus announcements.

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

**What we observed:** The Product Owner read the full 11KB research document, then received architecture input (ADR decisions, CALM governance patterns, system design principles) and security input (STRIDE threat categories with specific threat IDs, OWASP ASVS control mappings, NIST compliance requirements). The resulting 7.6KB PRD included explicit references to ADR-001 through ADR-003, all six STRIDE threat categories with mitigations, CALM model governance patterns, OWASP ASVS v5.0 controls, NIST SP 800-63B authentication assurance levels, PKCE authorization code flows, and RS256 asymmetric JWT signing. This is not generic LLM output -- every reference traces back to seeded knowledge or live web research.

### Phase 4: Implementation Design (Builder LangGraph)

The Builder runs a deterministic LangGraph pipeline: **read_document > ask_experts > synthesize > publish > review loop (revise until 80%+)**.

Triggered automatically by the Product Owner's bus announcement, the Builder:

1. **Read Document** -- Reads the 7.6KB PRD from the document store
2. **Ask Experts** -- Issues `bus_ask` calls to the Architect and Security agents
3. **Synthesize Design** -- Compiles the PRD and expert input into a TypeScript implementation design
4. **Publish** -- Publishes the initial design document to the hub's document store
5. **Review Loop** -- Sends the design to Architect and Security for structured review. If average score < 80%, revises using explicit feedback and resubmits. Max 5 iterations, then accepts as-is.

**What we observed:** The Builder read the full 7.6KB PRD, consulted experts, and produced an initial 11.6KB implementation design. It then submitted the design for structured review:

- **Round 1:** Architect scored 78%, Security scored 72% -- average 75%, below the 80% threshold. The Builder revised, addressing each reviewer's missing items separately with `<!-- Rev 1: Addressed change -->` markers.
- **Round 2:** Architect scored 85%, Security scored 92% -- average 89%, APPROVED.

The final 27.8KB TypeScript implementation design included actual project structure with `jwt.strategy.ts`, `auth.middleware.ts`, `token.service.ts`, interface definitions, NestJS module organization, per-STRIDE-category mitigations, RS256 signing configuration, PKCE flow implementation, and a three-phase delivery plan. A separate Design Review Report was published with both rounds of scores, severity ratings, covered items, missing items, and required changes.

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
- **Serving:** NGC vLLM container on a DGX Spark (128GB memory). Configured with `--max-model-len 262144` (256K context window). At 256K, the model uses less than 1% of available KV cache on the 128GB Spark, leaving substantial headroom.
- **Tool call parser:** `--tool-call-parser qwen3_coder` is the correct parser for Nemotron Nano's `<tool_call><function=name>` format. Other parsers (including `hermes`) silently fail, causing agents to loop without acting. This was the single most important configuration discovery.
- **Output tokens:** `max_tokens: 32768` in agent configs enables rich, detailed output. The 256K context window provides ample room for the prompt, retrieved knowledge, and generation.
- **Direct Spark URL:** LangGraph agents connect directly to the DGX Spark at `spark-ee7d.local:8000` rather than through the NemoClaw `inference.local` proxy. The proxy imposes a 60-second timeout that is insufficient for large document synthesis. Direct connection eliminates this bottleneck.
- **Thinking mode off:** `enable_thinking: false` keeps thinking-mode tokens from consuming the context budget. Valuable for open-ended reasoning, but wasteful in structured pipelines where the agent's job is to call specific tools in sequence.
- **Pipeline orchestration:** LangGraph enforces deterministic workflows for the three pipeline agents (Researcher, Product Owner, Builder). The Architect and Security experts use NAT `tool_calling_agent` since they respond to queries rather than driving workflows.
- **Handoff mechanism:** Fire-and-forget bus announcements trigger downstream agents. When the Researcher publishes a document, the bus announcement includes the document ID. The Product Owner's LangGraph pipeline picks up the trigger and begins its own deterministic sequence. No polling. No shared state. No orchestrator.

### Agent Sandboxing

- **Isolation:** Each agent runs in its own NemoClaw kernel-isolated k3s pod, fully network-isolated
- **Proxy:** All outbound traffic routes through the OpenShell proxy at `10.200.0.1:3128`
- **LLM routing:** Expert agents (Architect, Security) can use NemoClaw's built-in `inference.local` proxy. LangGraph pipeline agents bypass this to avoid the 60-second timeout.
- **Secrets:** External API keys (e.g. `TAVILY_API_KEY`) are injected via OpenShell providers, not hardcoded in config files. Only the Researcher sandbox receives the Tavily provider.
- **Network policies:** Hub connections, PyPI, and HuggingFace access require explicit rules in `policies/openclaw-sandbox.yaml`. Private IP endpoints require interactive approval via `openshell term`.

### Shared Memory Layer

- **NCMS** provides persistent shared memory across all agents via an HTTP API on `:9080`
- **Hybrid retrieval:** BM25 (Tantivy/Rust) for lexical precision + SPLADE v3 for semantic expansion + NetworkX graph spreading activation. No dense vectors, no embedding API calls.
- **Integration:** Expert agents use `auto_memory_agent` to connect to NCMS. When a pipeline agent issues a `bus_ask`, the domain expert searches the shared store with hybrid retrieval and grounds its LLM response in retrieved facts.
- **Knowledge seeding:** Expert agents load curated domain knowledge at startup:
  - `knowledge/architecture/` -- ADRs, CALM model specifications
  - `knowledge/security/` -- STRIDE threat models, OWASP control mappings
  - Researcher, Product Owner, and Builder have no knowledge files; they learn by querying experts and consuming documents

---

## Prerequisites

Before you start:

1. **macOS with Docker Desktop** -- running and healthy.
2. **NemoClaw installed and onboarded:**
   ```bash
   curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
   openshell gateway start
   ```
3. **DGX Spark or other vLLM endpoint** -- serving a model accessible from your network. This guide uses a DGX Spark at `spark-ee7d.local:8000` running Nemotron-3-Nano-30B via the NGC vLLM container.
4. **HF_TOKEN** -- a HuggingFace token with access to gated models (SPLADE v3 requires it).
5. **TAVILY_API_KEY** -- a Tavily API key for the Researcher's web search. Get one at tavily.com.
6. **A `.env` file** in the project root (`~/ncms/.env`) with your keys:
   ```bash
   HF_TOKEN=hf_your_token_here
   TAVILY_API_KEY=tvly-your_key_here
   ```
   The setup script auto-loads this file. No need to export variables manually.
7. **Python 3.12+ with uv** -- the NCMS build toolchain:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

---

## Step-by-Step Setup

### Step 1: Configure the Inference Provider

Tell NemoClaw where your LLM lives. This creates a named provider that sandboxes can use through `inference.local`:

```bash
openshell provider create --name dgx-spark --type openai \
  --credential "OPENAI_API_KEY=dummy" \
  --config "OPENAI_BASE_URL=http://spark-ee7d.local:8000/v1"

openshell inference set --no-verify --provider dgx-spark \
  --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
```

After this, any sandbox can reach the LLM at `https://inference.local/v1` and NemoClaw handles the routing transparently. The `--no-verify` flag skips TLS verification for the local endpoint.

**Important:** LangGraph pipeline agents (Researcher, Product Owner, Builder) bypass `inference.local` and connect directly to `spark-ee7d.local:8000` to avoid the proxy's 60-second timeout. The inference provider is still needed for the expert agents (Architect, Security) that use NAT's `tool_calling_agent`.

### Step 2: Deploy vLLM with 256K Context

```bash
sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-nano \
  -p 8000:8000 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --max-model-len 262144 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
```

The model supports up to 1M context tokens. At 256K (`--max-model-len 262144`), the model (~15GB) uses less than 1% of available KV cache on the 128GB Spark. The `--tool-call-parser qwen3_coder` flag is critical -- Nemotron Nano emits tool calls in the `<tool_call><function=name>` format, and `qwen3_coder` is the only vLLM parser that handles this correctly.

### Step 3: One-Command Deploy

```bash
cd deployment/nemoclaw-blueprint
./setup_nemoclaw.sh
```

That is the happy path. Here is what happens under the hood:

**Step 0 -- Environment and Providers**

- Loads `~/ncms/.env` (API keys, endpoint overrides).
- Creates the `tavily` OpenShell provider from `TAVILY_API_KEY` (idempotent -- skips if already exists). This injects the key into the Researcher's sandbox as an environment variable.

**Step 1/5 -- NCMS Hub and Phoenix (Docker)**

- Builds the `ncms-hub` Docker image: Python 3.12 + NCMS + pre-downloaded models (GLiNER 209MB, cross-encoder 80MB, SPLADE 500MB if HF_TOKEN provided).
- Starts two containers via `docker-compose.hub.yaml`:
  - `ncms-hub` -- NCMS HTTP API on `:9080` + Dashboard on `:8420`
  - `phoenix` -- Arize Phoenix tracing UI on `:6006`
- Waits up to 180 seconds for the hub health endpoint to respond.

**Steps 2-5 -- Agent Sandboxes (NemoClaw)**

Each agent gets the same treatment:

- Creates a NemoClaw sandbox from the `openclaw` template with the network policy.
- Attaches providers as needed (the Researcher gets the `tavily` provider).
- Uploads NCMS source code (`src/`, `pyproject.toml`, `uv.lock`) into `/sandbox/ncms/`.
- Uploads agent code, LangGraph pipelines (for Researcher/PO/Builder), and domain knowledge files.
- Installs NCMS via `uv sync`, then installs agent dependencies.
- Starts the agent process and sets up `openshell forward` for port access.

| Step | Sandbox | Agent | Type | Port |
|------|---------|-------|------|------|
| 2/5 | ncms-architect | Architect | tool_calling_agent | 8001 |
| 3/5 | ncms-security | Security | tool_calling_agent | 8002 |
| 4/5 | ncms-builder | Builder | LangGraph | 8003 |
| 5/5 | ncms-researcher | Researcher | LangGraph | 8004 |

**Final step:** Polls the hub health endpoint waiting for all agents to register, with a 60-second timeout.

### Step 4: Approve Network Connections

NemoClaw sandboxes are fully network-isolated. Every outbound connection goes through the OpenShell proxy. Static YAML policies work for public HTTPS endpoints like PyPI, GitHub, and HuggingFace. Private IP endpoints are a different story -- the OpenShell proxy requires interactive approval regardless of what the YAML says.

Open a second terminal and run:

```bash
openshell term
```

This is the interactive approval terminal. When a sandbox tries to reach a private IP, you will see a prompt like:

```
[ncms-architect] python3.13 wants to connect to host.docker.internal:9080 (192.168.65.254)
Allow? [y/N]
```

Type `y` and hit enter. You will need to approve connections for:

- **Each sandbox** connecting to the hub (`host.docker.internal:9080`)
- **Each sandbox** connecting to Phoenix tracing (`host.docker.internal:6006`)
- **Each binary** that makes the connection (`python3.13`, `curl`)
- **DGX Spark** connections from LangGraph agents (`spark-ee7d.local:8000`)

With five agent sandboxes, expect a burst of approval prompts during initial setup. Keep the terminal open for the entire process.

### Rebuild from Scratch

If something goes wrong (it will, the first time), tear everything down and start fresh:

```bash
./setup_nemoclaw.sh --rebuild
```

This destroys all sandboxes and Docker containers, then runs the full setup again.

---

## Testing the Pipeline

This is the end-to-end workflow from research to implementation design. One message triggers the entire auto-chaining pipeline.

### 1. Trigger the Research Pipeline

Open http://localhost:8420 and click the **Researcher** card to open the chat overlay. Type:

> Research modern identity service patterns including OAuth 2.0, passkeys, and zero-trust authentication.

The Researcher's LangGraph pipeline activates:
1. **Plan** -- generates 5 parallel search queries
2. **Search** -- executes all 5 Tavily searches in parallel (25 results total)
3. **Synthesize** -- compiles an 11KB market research report
4. **Publish** -- document appears in the sidebar
5. **Trigger** -- bus announcement fires, Product Owner activates automatically

### 2. Watch the Auto-Chain

No further human interaction is required. The pipeline chains automatically:

**Product Owner activates** (triggered by Researcher's bus announcement):
- Reads the 9KB research report
- Issues parallel `bus_ask` calls to Architect and Security
- Receives 2.4KB architecture input + 3KB security input
- Synthesizes and publishes a 15KB PRD
- Fires bus announcement, Builder activates automatically

**Builder activates** (triggered by Product Owner's bus announcement):
- Reads the 15KB PRD
- Issues parallel `bus_ask` calls to Architect and Security
- Receives 6.6KB architecture input + 384 bytes security confirmation
- Synthesizes and publishes an 18KB TypeScript implementation design

### 3. Review the Artifacts

Three documents appear in the Documents sidebar:

1. **Market Research Report** (9KB) -- NIST standards, OAuth 2.0 PKCE, passkey adoption, web sources
2. **PRD** (15KB) -- ADR references, STRIDE threat mitigations, CALM governance, OWASP controls, NIST compliance, PKCE flows, RS256 signing
3. **Implementation Design** (18KB) -- TypeScript project structure, `jwt.strategy.ts`, `auth.middleware.ts`, interface contracts, NestJS modules, per-STRIDE mitigations, three-phase delivery plan

### 4. Inspect Traces

Open Phoenix at http://localhost:6006. Each agent traces to a separate project. You can follow the full reasoning chain across the pipeline -- every LLM call, tool invocation, knowledge bus interaction, and inter-agent handoff is captured.

### Scripted Trigger (Alternative)

You can also trigger the pipeline from the command line:

```bash
./setup_nemoclaw.sh --trigger
```

This sends the research prompt programmatically and the auto-chain executes without the dashboard UI.

---

## Configuration Reference

### Agent Configs

Each agent is configured via YAML files in `deployment/nemoclaw-blueprint/configs/`. Key configuration patterns:

- **LangGraph agents** (Researcher, Product Owner, Builder) define deterministic node graphs. Each node executes one step. The LLM generates content within each node; the graph enforces sequence and parallelism.
- **Expert agents** (Architect, Security) use NAT `tool_calling_agent` with `use_native_tool_calling: true`. They respond to `bus_ask` queries rather than driving workflows.
- `max_tokens: 32768` -- enables rich, detailed output for document synthesis
- Direct Spark URL (`spark-ee7d.local:8000`) for LangGraph agents bypasses the NemoClaw proxy 60-second timeout
- Single-word tool names (`writeprd`, `writedesign`, not `write_prd`) -- NAT's text parser sometimes bolds multi-word names, breaking tool dispatch

### Builder Config (`configs/builder.yml`)

The Builder's LangGraph pipeline uses the RCTRO prompt format (Role, Context, Task, Requirements, Output) at each node:

```yaml
llms:
  spark_llm:
    _type: openai
    model_name: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    base_url: "http://spark-ee7d.local:8000/v1"
    api_key: "dummy"
    max_tokens: 32768

memory:
  ncms_store:
    _type: ncms_memory
    hub_url: "http://host.docker.internal:9080"
    agent_id: builder
    domains: [identity-service, implementation]
    subscribe_to: [architecture, security, threats, controls, product, requirements]

functions:
  read_document:
    _type: read_document
    hub_url: "http://host.docker.internal:9080"

  bus_ask:
    _type: ask_knowledge
    hub_url: "http://host.docker.internal:9080"
    from_agent: builder
    timeout_ms: 120000

  publish_document:
    _type: writedesign
    hub_url: "http://host.docker.internal:9080"
    from_agent: builder
```

### Agent Differences

- **Researcher** -- LangGraph pipeline with Tavily web search. No seeded knowledge. Produces research reports.
- **Product Owner** -- LangGraph pipeline that reads research documents and consults experts. Produces PRDs. Triggers Builder.
- **Builder** -- LangGraph pipeline that reads PRDs and consults experts. Produces implementation designs. Terminal node in the chain.
- **Architect** -- NAT `tool_calling_agent` with seeded ADRs, CALM model, quality attribute scenarios. Answers `bus_ask` queries.
- **Security** -- NAT `tool_calling_agent` with seeded STRIDE threat models, OWASP controls. Answers `bus_ask` queries.
- Each agent traces to a separate Phoenix project.

### Network Policy Reference

The policy file at `deployment/nemoclaw-blueprint/policies/openclaw-sandbox.yaml` defines all allowed network endpoints:

| Policy Name | Host | Port | Purpose |
|------------|------|------|---------|
| `ncms_hub` | host.docker.internal | 9080 | Hub API + Bus + SSE |
| `dgx_spark` | spark-ee7d.local | 8000 | LLM inference (direct, bypasses proxy timeout) |
| `phoenix` | host.docker.internal | 6006 | Tracing (OpenTelemetry) |
| `python_packages` | pypi.org, files.pythonhosted.org | 443 | pip/uv installs |
| `nvidia_packages` | pypi.nvidia.com | 443 | NAT packages |
| `huggingface` | huggingface.co, cdn-lfs.huggingface.co | 443 | Model downloads |
| `tavily` | api.tavily.com | 443 | Web search (Researcher) |
| `claude_code` | api.anthropic.com | 443 | Claude Code (if using) |
| `github` | github.com, api.github.com | 443 | Git operations |

For private IP endpoints (`host.docker.internal`, `spark-ee7d.local`), use `access: full` with `allowed_ips` and explicit binary paths. The wildcard binary `{ path: "-" }` is supposed to match any binary but does not always work for private IPs -- list specific binaries as well.

### Config File Locations

```
deployment/nemoclaw-blueprint/
  configs/
    architect.yml          # Architect agent NAT config
    security.yml           # Security agent NAT config
    builder.yml            # Builder LangGraph config
    product_owner.yml      # Product Owner LangGraph config
    researcher.yml         # Researcher LangGraph config
  knowledge/
    architecture/          # ADRs, CALM model
    security/              # Threat model, OWASP
  policies/
    openclaw-sandbox.yaml  # Network policy for all sandboxes
  docker-compose.hub.yaml  # Hub + Phoenix Docker Compose
  setup_nemoclaw.sh        # One-command deploy script
```

---

## Troubleshooting

### LLM outputs bold tool names (`**ask_knowledge**`)

**Cause:** Some models emit markdown bold around tool names in their ReAct output. NAT's text parser fails to match the bolded name to a registered tool.

**Fix:** Expert agent configs use `use_native_tool_calling: true` and single-word tool names. LangGraph agents avoid this problem entirely since tool dispatch is graph-driven, not text-parsed.

### Agent times out or hangs

**Cause:** vLLM not running, not reachable from the sandbox, or the NemoClaw proxy 60-second timeout cutting off long synthesis operations.

**Fix:** Verify vLLM is healthy:
```bash
curl http://spark-ee7d.local:8000/health
```
For LangGraph agents, ensure configs point directly to `spark-ee7d.local:8000` rather than `inference.local` to bypass the proxy timeout.

### Documents don't appear in the sidebar

**Cause:** The hub is not emitting `document.published` SSE events, or the document was not actually published.

**Fix:** Check hub logs for document events:
```bash
docker logs ncms-hub 2>&1 | grep document
```
Verify the agent's publish tool call completed successfully in the Phoenix trace.

### Auto-chain doesn't trigger downstream agents

**Cause:** Bus announcement not reaching the next agent, or the receiving agent's trigger listener not running.

**Fix:** Check that the publishing agent's bus announcement completed in the Phoenix trace. Verify the downstream agent is registered and subscribed to the correct domains in the hub health endpoint:
```bash
curl http://localhost:9080/api/v1/health
```

### Tavily web search fails

**Cause:** `TAVILY_API_KEY` not injected into the Researcher's sandbox.

**Fix:** Verify the provider exists and the key is set:
```bash
openshell provider list
```
If the tavily provider is missing, create it:
```bash
openshell provider create --name tavily --type generic \
  --credential "TAVILY_API_KEY=your_key_here"
```
Then rebuild the researcher sandbox so it picks up the provider.

### "Failed to fetch" in dashboard chat

**Cause:** CORS issue. The dashboard is calling the agent directly instead of through the hub proxy.

**Fix:** Make sure you are using the dashboard at `http://localhost:8420`, not calling agent ports directly. The hub proxy at `/api/v1/agent/{id}/chat` handles CORS correctly.

### Agent not connecting to hub

**Cause:** Proxy blocking the connection to `host.docker.internal:9080`.

**Fix:** Open `openshell term` and approve the pending connection. Look for:
```
[ncms-architect] python3.13 wants to connect to host.docker.internal:9080
```

### 403 Forbidden in agent logs

**Cause:** OpenShell proxy denying the connection.

**Fix:** Check `openshell term` for pending approvals. If approvals were previously denied, restart the gateway:
```bash
openshell gateway restart
```
Then rebuild the problematic sandbox or run `./setup_nemoclaw.sh --rebuild`.

### Knowledge not loading

**Cause:** Agent cannot reach hub to upload knowledge files.

**Fix:** Check agent logs:
```bash
ssh openshell-ncms-architect 'tail -20 /tmp/ncms-nat-agent.log'
```
If you see connection errors, approve the hub connection in `openshell term`.

### NAT not installed in sandbox

**Cause:** PyPI access blocked by proxy.

**Fix:** The `python_packages` policy in `policies/openclaw-sandbox.yaml` should allow PyPI access. If it does not, approve PyPI connections in `openshell term`. The setup script installs:
```
nvidia-nat-core nvidia-nat-langchain nvidia-nat-opentelemetry
```
These pull from `pypi.org` and `files.pythonhosted.org`.

### `nat serve` crashes with `_dask_client` error

**Cause:** Known NAT bug.

**Fix:** Use `nat start fastapi` instead of `nat serve`. The setup script already does this.

### Multiple processes fighting for a port

**Cause:** Previous agent process was not killed before starting a new one.

**Fix:** Shell into the sandbox and kill stale processes:
```bash
openshell sandbox connect ncms-builder
killall -9 python3.13
exit
```
Then restart the agent or run `./setup_nemoclaw.sh --rebuild`.

### Known Quirks

1. **Architect always needs manual approval.** Security, builder, and other sandboxes tend to auto-approve after the first time, but the architect sandbox has stale state in the gateway database that persists across restarts.

2. **Gateway restart wipes approvals.** If you restart the NemoClaw gateway (`openshell gateway restart`), all interactive approvals are lost. The sandboxes will need re-approval on their next connection attempt.

3. **Stale sandbox names.** If a sandbox name was previously denied, the gateway DB keeps the deny state. Restarting the gateway clears this. If you see persistent 403 errors after creating a sandbox with a previously-used name, restart the gateway.

4. **`openshell forward` maps same port only.** You cannot map `localhost:9000` to sandbox port `8000`. Each agent listens on a unique port so the forwards work 1:1.

5. **NemoClaw proxy 60-second timeout.** The `inference.local` proxy has a hardcoded 60-second timeout. LangGraph synthesis nodes that generate large documents (15-18KB) can exceed this. Solution: connect LangGraph agents directly to the Spark URL.

---

## Commands Reference

```bash
# Full lifecycle
./setup_nemoclaw.sh                     # Full setup (hub + agent sandboxes)
./setup_nemoclaw.sh --rebuild           # Teardown everything, then full setup
./setup_nemoclaw.sh --status            # Show status of all components
./setup_nemoclaw.sh --teardown          # Remove everything (sandboxes + Docker)
./setup_nemoclaw.sh --trigger           # Trigger research pipeline (auto-chains to PRD + design)
./setup_nemoclaw.sh --skip-hub          # Only create agent sandboxes (hub already running)

# NemoClaw management
openshell term                          # Interactive approval terminal (KEEP THIS OPEN)
openshell sandbox connect ncms-builder  # Shell into a sandbox
openshell sandbox list                  # List all sandboxes
openshell forward list                  # Check port forwards
openshell gateway restart               # Clear stale proxy state
openshell provider list                 # Check providers (tavily, dgx-spark)

# Agent debugging
ssh openshell-ncms-architect 'tail -f /tmp/ncms-nat-agent.log'       # Stream agent logs
ssh openshell-ncms-researcher 'tail -f /tmp/ncms-nat-agent.log'      # Researcher logs
ssh openshell-ncms-builder 'pgrep -fa python'                         # Check running processes
ssh openshell-ncms-security 'curl -s localhost:8002/health'           # Agent health check

# Hub debugging
docker logs -f ncms-hub                 # Hub container logs
curl http://localhost:9080/api/v1/health  # Hub health (includes agent count)

# Custom inference endpoint
INFERENCE_ENDPOINT=http://my-gpu:8000/v1 \
INFERENCE_MODEL=my-org/my-model \
./setup_nemoclaw.sh
```

---

## The NAT-NCMS Plugin

The glue between NAT and NCMS lives in `packages/nvidia-nat-ncms/`. It is a NAT plugin that registers these components:

- **`ncms_memory`** -- A `MemoryEditor` that stores and searches memories via the NCMS Hub HTTP API. Handles agent registration, SSE subscription, and knowledge file loading on startup.
- **`ask_knowledge`** / **`bus_ask`** -- A tool that sends questions through the Knowledge Bus to domain experts and returns their answers. Supports comma-separated domain targeting.
- **`announce_knowledge`** -- A tool that broadcasts information to all subscribed agents.
- **`web_search`** -- A tool that calls the Tavily API for web search results. Uses the `TAVILY_API_KEY` environment variable injected by the OpenShell provider.
- **`read_document`** -- A tool that reads a published document from the hub's document store by ID.
- **`writeprd`** -- A tool that publishes a PRD to the hub's document store, triggering an SSE event for the dashboard.
- **`writedesign`** -- A tool that publishes a design document to the hub's document store.
- **`publish_document`** -- Generic document publishing tool used by LangGraph pipeline nodes.

The plugin uses namespace packages (no `__init__.py` in `nat/` or `nat/plugins/`). Only `nat/plugins/ncms/__init__.py` exists, which imports the registration decorators. This is critical -- adding `__init__.py` files in the wrong places shadows the core NAT package and breaks everything.

---

## What's Next

The auto-chaining pipeline runs end-to-end. These are the next areas of investment:

### Review Loop

The pipeline currently produces three documents in a forward-only chain. The next step is a review loop: the Architect and Security agents review the Builder's implementation design, score it against ADR compliance and STRIDE coverage, provide structured feedback, and the Builder iterates until the design exceeds an 80% quality threshold. This closes the loop between expert knowledge and implementation output.

### Coding Agent

A sixth agent that takes the Builder's 18KB TypeScript implementation design and generates actual source code, tests, and Dockerfiles. The design documents are already structured for this -- the coding agent would consume them as specifications. The project structure, interface contracts, and module organization are explicit enough to drive code generation directly.

### Model Experiments

**Nemotron Super 120B-A12B-NVFP4** is the next experiment. At 120B total / 12B active parameters (4x Nano's active compute) with NVIDIA's FP4 quantization, it fits on the DGX Spark (~60GB model, ~68GB for KV cache). Expected improvements: richer expert consultations, more detailed implementation designs, and more reliable multi-step reasoning. Requires NVIDIA driver 590+ for NVFP4 support. The vLLM command:

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

### 1M Context

The current pipeline uses 256K context. Nemotron Nano supports up to 1M tokens. Pushing to `--max-model-len 1048576` would enable massive document synthesis -- feeding the full research report, PRD, and all expert consultations into a single Builder context for richer, more coherent implementation designs. At 1M context, KV cache usage increases but remains feasible on the 128GB Spark for single-user workloads.

### Platform Capabilities

- **Human approval workflows** -- The dashboard supports agent chat, but structured approve/reject/modify flows for PRDs and designs are not yet wired up. This is the path to human-in-the-loop governance.
- **CrewAI StorageBackend integration** -- CrewAI defines 14 storage methods vs NAT's 3. An NCMS StorageBackend would let CrewAI agents use the same shared memory, opening the platform to a second agent framework.
- **Automated proxy approval** -- NemoClaw issue #326 tracks the request for static policy to work with private IPs without interactive approval. When that lands, the setup becomes truly one-command with zero manual steps.
- **Multi-pipeline orchestration** -- Running multiple pipelines concurrently (e.g., identity service + payment service) with cross-pipeline knowledge sharing through the bus.
- **Production hardening** -- mTLS between agents and hub, secret rotation for API keys, pod-level resource limits, and horizontal scaling of the hub.
