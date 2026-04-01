# Step-by-Step Setup & Configuration Guide

Detailed setup, testing, configuration, and troubleshooting for the Multi-Agent Software Delivery Pipeline.

> For the architecture overview, pipeline design, and results, see the [main quickstart](nemoclaw-nat-quickstart.md).

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
6. **GITHUB_PERSONAL_ACCESS_TOKEN** (optional) -- a GitHub PAT for the Archeologist agent's repository analysis. Without it, the Archeologist uses unauthenticated API calls (60 requests/hour). With it, 5000 requests/hour. Generate at github.com → Settings → Developer settings → Personal access tokens.
7. **A `.env` file** in the project root (`~/ncms/.env`) with your keys:
   ```bash
   HF_TOKEN=hf_your_token_here
   TAVILY_API_KEY=tvly-your_key_here
   GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_token_here
   ```
   The setup script auto-loads this file. No need to export variables manually. The setup script creates named providers for Tavily and GitHub, injecting the tokens into the appropriate agent sandboxes.
8. **Python 3.12+ with uv** -- the NCMS build toolchain:
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

**Important:** All agents are now LangGraph pipelines and connect directly to `spark-ee7d.local:8000` to avoid the proxy's 60-second timeout. The inference provider is still configured for compatibility but all agents bypass it in practice.

### Step 2: Deploy vLLM with 512K Context

```bash
# Download the Nemotron Nano reasoning parser plugin (enables thinking mode)
sudo wget -O /root/nano_v3_reasoning_parser.py \
  https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py

sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-nano \
  -p 8000:8000 \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  -v /root/nano_v3_reasoning_parser.py:/app/nano_v3_reasoning_parser.py \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --max-model-len 524288 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --reasoning-parser-plugin /app/nano_v3_reasoning_parser.py \
    --reasoning-parser nano_v3
```

512K context window with `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` (model's max_position_embeddings is 262144 but NVIDIA documents support up to 1M via RoPE scaling). At 512K, the model uses less than 0.5% of available KV cache on the 128GB Spark. The `--tool-call-parser qwen3_coder` flag is critical -- Nemotron Nano emits tool calls in the `<tool_call><function=name>` format, and `qwen3_coder` is the only vLLM parser that handles this correctly. The `--reasoning-parser-plugin` flag enables proper handling of `<think>` tags when thinking mode is on — without it, thinking tokens leak into the output content.

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
| 2/5 | ncms-architect | Architect | LangGraph (dual-mode) | 8001 |
| 3/5 | ncms-security | Security | LangGraph (dual-mode) | 8002 |
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
- Reads the 11KB research report
- Issues parallel `bus_ask` calls to Architect and Security
- Receives architecture + security expert input
- Synthesizes and publishes a 16KB PRD
- Fires bus announcement, Builder activates automatically

**Builder activates** (triggered by Product Owner's bus announcement):
- Reads the 16KB PRD
- Consults Architect and Security experts
- Synthesizes and publishes a 21KB implementation design
- Submits for structured review: Round 1 (Architect 88%, Security 78% -- APPROVED at 83%)
- Publishes 21KB implementation design + 6KB Design Review Report

### 3. Review the Artifacts

Four documents appear in the Documents sidebar:

1. **Market Research Report** (11KB) -- 5 parallel Tavily searches, 25 results synthesized
2. **PRD** (16KB) -- CALM (7), ADR (4), OWASP (3), NIST (3), JWT (10), RBAC (4), RS256 (2)
3. **Implementation Design** (21KB) -- JWT (24), TypeScript (3), interface (7), bcrypt
4. **Design Review Report** (6KB) -- ADR (10), CALM (3), STRIDE, SCORE 88%/78%, approved at 83%

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

### Entity Labels (GLiNER Topics)

GLiNER zero-shot NER extracts entities from every document and memory. Labels control what entity types the model looks for. NCMS uses two layers:

**Universal labels** (always active, 10 labels): `person`, `organization`, `location`, `technology`, `concept`, `event`, `product`, `process`, `document`, `metric`

**Domain-specific labels** (additive, configured per domain): The hub seeds a `software` domain on startup with 10 labels tuned for the software delivery pipeline:

| Label | What it catches | Examples |
|-------|----------------|----------|
| `framework` | Application frameworks | NestJS, Express, FastAPI, React |
| `database` | Data stores | MongoDB, PostgreSQL, Redis, DynamoDB |
| `protocol` | Communication protocols | OAuth, OIDC, SAML, gRPC, REST, HTTPS |
| `standard` | Compliance and governance | OWASP ASVS, NIST 800-63B, GDPR, ISO 27001 |
| `threat` | Security threats | SQL injection, XSS, spoofing, DoS, CSRF |
| `pattern` | Architecture patterns | CQRS, circuit breaker, rate limiting, saga |
| `security_control` | Security mechanisms | MFA, RBAC, JWT signing, encryption, hashing |
| `api_endpoint` | API routes | /auth/login, POST /users, GET /health |
| `data_model` | Data structures | User schema, Session collection, Token entity |
| `architecture_decision` | Design decisions | ADR-001, service boundary, microservice |

Combined with universals, GLiNER extracts against 20 label types — matching the MAX_ENTITIES cap for rich entity coverage.

**Custom domains:** Add domain-specific labels via CLI or programmatically:
```bash
uv run ncms topics set <domain> <label1> <label2> ...
uv run ncms topics list              # Show all domains
uv run ncms topics list software     # Show labels for a domain
```

The hub Docker container seeds topics automatically at startup via `entrypoint-hub.sh`. Labels persist in SQLite (`consolidation_state` table) and survive restarts.

### Agent Configs

Each agent is configured via YAML files in `deployment/nemoclaw-blueprint/configs/`. Key configuration patterns:

- **LangGraph agents** (Researcher, Product Owner, Builder) define deterministic node graphs. Each node executes one step. The LLM generates content within each node; the graph enforces sequence and parallelism.
- **Expert agents** (Architect, Security) use LangGraph dual-mode pipelines. They classify incoming requests as knowledge questions or design reviews, search NCMS memory with domain filtering, and produce either grounded knowledge answers or structured reviews (SCORE/SEVERITY/COVERED/MISSING/CHANGES).
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
- **Builder** -- LangGraph pipeline that reads PRDs, consults experts, produces implementation designs, and runs a review loop (revise until 80%+ average from reviewers, max 5 iterations).
- **Architect** -- LangGraph dual-mode pipeline with seeded ADRs, CALM model, quality attribute scenarios. Classifies requests as knowledge questions or design reviews. Knowledge answers cite ADRs and CALM specs. Structured reviews return SCORE/SEVERITY/COVERED/MISSING/CHANGES.
- **Security** -- LangGraph dual-mode pipeline with seeded STRIDE threat models, OWASP controls. Classifies requests as knowledge questions or design reviews. Knowledge answers cite threat IDs and OWASP sections. Structured reviews evaluate OWASP Top 10, STRIDE compliance, secrets management, transport security.
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

1. **Gateway restart wipes approvals.** If you restart the NemoClaw gateway (`openshell gateway restart`), all interactive approvals are lost. The sandboxes will need re-approval on their next connection attempt.

2. **Stale sandbox names.** If a sandbox name was previously denied, the gateway DB keeps the deny state. Restarting the gateway clears this. If you see persistent 403 errors after creating a sandbox with a previously-used name, restart the gateway.

3. **Port forwards can drop.** SSH-based port forwards (`openshell forward`) may drop during long-running operations or Mac sleep. The auto-chain pipeline uses bus-based triggers to avoid this, but dashboard chat still requires port forwards.

4. **`openshell forward` maps same port only.** You cannot map `localhost:9000` to sandbox port `8000`. Each agent listens on a unique port so the forwards work 1:1.

5. **NemoClaw inference proxy has a 60-second timeout.** LangGraph agents connect directly to `spark-ee7d.local:8000` instead of through `inference.local` to avoid this. The proxy timeout is hardcoded in the OpenShell gateway.

5. **NemoClaw proxy 60-second timeout.** The `inference.local` proxy has a hardcoded 60-second timeout. LangGraph synthesis nodes that generate large documents (20KB+) can exceed this. Solution: connect LangGraph agents directly to the Spark URL.

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
