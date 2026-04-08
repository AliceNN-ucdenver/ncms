# Multi-Agent Self-Improvement Loop with Human-in-the-Loop

> **Status: COMPLETE** (Phases 1-2 implemented, Phase 3 PO+PRD pipeline operational via NemoClaw). Orchestration patterns now production-validated on NCMS Hub.

## Context

We have a working NCMS + NAT + NemoClaw deployment: 3 agents (architect, security, builder) in NemoClaw sandboxes, NCMS Hub in Docker, DGX Spark for LLM inference, dashboard with chat. The agents work but have key limitations:

1. **Builder sends one combined question** to both agents → gets identical responses (both search same shared memory without domain filtering)
2. **No human participation** — dashboard chat is one-shot Q&A, not a collaborative workflow
3. **No phased workflow** — agents don't plan → review → execute → verify
4. **No reflection loop** — builder doesn't iterate on its design with expert feedback
5. **Architect/Security don't proactively review** builder's output

**Goal:** Transform one-shot Q&A into a phased orchestration pipeline where agents collaborate, humans approve plans, and peers review output.

## Architecture: The Self-Improvement Loop

```
User sends task to Builder (via dashboard chat → /generate)
    ↓
PHASE 1: PLAN
    Builder asks Architect: "What ADRs apply?" (domains: architecture,decisions)
    Builder asks Security: "What threats apply?" (domains: security,threats)
    (SEPARATE calls → domain-filtered responses → distinct expert input)
    Builder synthesizes into a draft plan
    ↓
PHASE 2: SUBMIT FOR REVIEW (Non-Blocking)
    Builder calls request_approval tool → bus announce to "human-approval"
    Builder stores state in memory: "AWAITING_APPROVAL, plan_id=X"
    Builder returns: "Plan submitted, awaiting approval"
    ↓
HUMAN REVIEWS (async — minutes, hours, or next day)
    Dashboard Approval Queue shows pending plans
    Human clicks Approve/Reject/Suggest
    → bus announce to "approval-response" domain
    Builder's SSE listener receives approval
    If rejected → human triggers Builder again with feedback
    ↓
PHASE 3: EXECUTE
    Builder produces detailed design based on approved plan
    Builder announces: "Build complete, ready for review"
    ↓
PHASE 4: VERIFY (Peer Review)
    Architect reviews for ADR alignment → announces feedback
    Security reviews for OWASP/STRIDE compliance → announces feedback
    ↓
PHASE 5: ITERATE (if needed)
    Builder reads review feedback → addresses issues → back to Phase 1
    Or: all clear → announce final design, store in memory
```

**Key design choice:** The human participates as a **real bus agent** — registered with domains `["human-approval"]`, receiving questions via SSE, responding via `/bus/respond`. No special infrastructure needed.

## Implementation Plan (MVP: Steps 1-4)

### Step 1: Memory Domain Filtering in SSE Listener

**Problem:** When architect answers a question, it searches ALL hub memory (ADRs + threat models). Same for security. Both return identical context.

**Fix:** Pass the agent's own domains as a filter when searching memory.

**Files:**
- `packages/nvidia-nat-ncms/src/nat/plugins/ncms/sse_listener.py`
  - Add `agent_domains: list[str] | None = None` parameter
  - In `_handle_question`, pass first domain to `recall_memory(domain=agent_domains[0])`
- `packages/nvidia-nat-ncms/src/nat/plugins/ncms/register.py`
  - Already passes `domains=config.domains` to `sse_listener()` ✓

**Result:** Architect returns ADR-specific context. Security returns STRIDE-specific context.

### Step 2: Add `request_approval` Tool (Non-Blocking)

**Design:** The builder doesn't block waiting for human response. Instead:
1. Builder **announces** its plan to domain `"human-approval"` (fire-and-forget)
2. Builder **stores its state** in NCMS memory: `"Phase: AWAITING_APPROVAL, plan_id: X"`
3. Builder's NAT `/generate` call **returns** with "Plan submitted for approval"
4. Dashboard shows the plan in a **persistent Approval Queue**
5. Human reviews whenever available (minutes, hours, next day)
6. Human clicks Approve → **announces** to domain `"approval-response"` with plan_id
7. Builder's SSE listener receives the approval announcement
8. Builder **resumes Phase 3** by the human or a scheduled check triggering `/generate` again

**File:** `packages/nvidia-nat-ncms/src/nat/plugins/ncms/tools.py`

New tool registration:
```python
class RequestApprovalConfig(FunctionBaseConfig, name="request_approval"):
    hub_url: str = "http://host.docker.internal:9080"
    from_agent: str = "nat-agent"

@register_function(config_type=RequestApprovalConfig)
async def request_approval(config, builder):
    # Announces plan to "human-approval" domain (non-blocking)
    # Stores approval request in hub memory for persistence
    # Returns immediately — does NOT wait for human response
    # Human reviews in dashboard Approval Queue at their leisure
```

**Dashboard Approval Queue:**
- Persistent panel showing all pending approvals (not just current session)
- Each card: plan content (markdown), from_agent, timestamp, plan_id
- Buttons: Approve / Reject / Suggest Changes + comment input
- On Approve → `POST /api/v1/bus/announce` with `{domains: ["approval-response"], content: "APPROVED: plan_id=X [comments]", from_agent: "human"}`
- Queue populated from NCMS memory search: `domain=human-approval, type=fact`
- Approvals persist across dashboard refreshes and human sessions

### Step 3: Update Agent Configs

**File:** `deployment/nemoclaw-blueprint/configs/builder.yml`
- Add `ask_human` to functions section and `tool_names`
- Replace description with multi-phase system prompt:
  - Phase 1: Make SEPARATE calls to architect (architecture,decisions) and security (security,threats)
  - Phase 2: Call `ask_human` with synthesized plan, wait for approval
  - Phase 3: Execute only after approval
  - Phase 4: Announce completion with "ready for review" trigger
  - Rules: never combine domain calls, always get human approval, cite sources

**File:** `deployment/nemoclaw-blueprint/configs/architect.yml`
- Add `ask_knowledge` tool (so architect can query when reviewing)
- Update description: ANSWER MODE (respond to questions) + REVIEW MODE (review announcements containing "ready for review", post feedback)

**File:** `deployment/nemoclaw-blueprint/configs/security.yml`
- Same pattern as architect: add `ask_knowledge`, add REVIEW MODE

### Step 4: Dashboard Refactor — Vanilla JS Modules + Human Agent

**Problem:** `index.html` is 4619 lines. Adding approval UI will make it worse.

**Solution:** Split into vanilla JS modules. No build step, no framework.

```
src/ncms/interfaces/http/static/
├── index.html              (shell: layout + script imports only)
├── js/
│   ├── app.js              (init, SSE connections, shared state, human agent registration)
│   ├── bus.js              (bus backbone visualization)
│   ├── agents.js           (agent cards panel)
│   ├── chat.js             (agent chat — calls /generate directly)
│   ├── approvals.js        (NEW: approval queue panel)
│   ├── events.js           (event/conversation history feed)
│   ├── graph.js            (D3 knowledge graph)
│   └── memory.js           (memory browser table)
└── css/
    └── dashboard.css       (extracted from inline styles)
```

**Layout (top to bottom):**
```
┌─────────────────────────────────────────────────┐
│  Header: NCMS Dashboard                         │
├─────────────────────────────────────────────────┤
│  Bus Backbone (animated SVG)                    │
│  Agent Cards: architect | security | builder    │
├──────────────────┬──────────────────────────────┤
│  Human Agent     │  Tabs:                       │
│  Panel           │  [Chat] [Approvals] [Events] │
│                  │                              │
│  Agent Chat      │  Tab content area:           │
│  (select agent,  │  - Chat: conversation thread │
│   send message)  │  - Approvals: pending queue  │
│                  │  - Events: bus event history  │
├──────────────────┴──────────────────────────────┤
│  Knowledge Graph (D3) | Memory Browser          │
└─────────────────────────────────────────────────┘
```

**app.js responsibilities:**
- Shared state object (`window.ncmsState`)
- Two SSE connections: main events stream + human agent stream
- Register "human" agent on load: `POST /bus/register {agent_id: "human", domains: ["human-approval"]}`
- Human SSE: `GET /bus/subscribe?agent_id=human` — routes `bus.ask.routed` events to approvals.js
- Event dispatching: `document.dispatchEvent(new CustomEvent('ncms:agent-update', {detail}))`
- Each module listens for custom events, no direct coupling

**approvals.js (NEW):**
- Listens for `ncms:approval-request` custom events from app.js
- Renders approval cards: plan content (markdown → HTML via marked.js), from_agent, timestamp
- Three buttons: Approve / Reject / Suggest Changes + comment textarea
- On action → `POST /bus/announce {domains: ["approval-response"], content: "APPROVED|REJECTED: plan_id=X [comments]", from_agent: "human"}`
- Queue persists across refreshes: loads pending approvals from NCMS memory (`GET /memories/search?q=AWAITING_APPROVAL&domain=human-approval`)
- Shows approval history (approved/rejected items greyed out)

**chat.js updates:**
- Shows conversation thread with phase indicators
- Phase 1 (Plan): agent asks experts → shows expert responses
- Phase 2 (Review): "Submitted for approval" message
- Phase 3 (Execute): design output
- Phase 4 (Verify): review feedback from architect/security

**events.js:**
- Extracted from current inline event feed code
- Filterable by agent, domain, event type
- Shows full conversation history across all agents

## Files to Create

| File | Purpose |
|------|---------|
| `src/ncms/interfaces/http/static/js/app.js` | Init, SSE, state, human agent registration |
| `src/ncms/interfaces/http/static/js/bus.js` | Bus backbone SVG animation |
| `src/ncms/interfaces/http/static/js/agents.js` | Agent cards panel |
| `src/ncms/interfaces/http/static/js/chat.js` | Agent chat with phase indicators |
| `src/ncms/interfaces/http/static/js/approvals.js` | Approval queue panel (NEW) |
| `src/ncms/interfaces/http/static/js/events.js` | Event/conversation history feed |
| `src/ncms/interfaces/http/static/js/graph.js` | D3 knowledge graph |
| `src/ncms/interfaces/http/static/js/memory.js` | Memory browser table |
| `src/ncms/interfaces/http/static/css/dashboard.css` | Extracted styles |

## Files to Modify

| File | Change |
|------|--------|
| `src/ncms/interfaces/http/static/index.html` | Strip to shell: layout + `<script type="module">` imports only |
| `packages/nvidia-nat-ncms/src/nat/plugins/ncms/sse_listener.py` | Domain filter in `_handle_question` recall |
| `packages/nvidia-nat-ncms/src/nat/plugins/ncms/tools.py` | Add `request_approval` tool registration |
| `deployment/nemoclaw-blueprint/configs/builder.yml` | Multi-phase prompt, request_approval tool |
| `deployment/nemoclaw-blueprint/configs/architect.yml` | Add ask_knowledge tool, review mode prompt |
| `deployment/nemoclaw-blueprint/configs/security.yml` | Add ask_knowledge tool, review mode prompt |

## Phase 2 — Completed

| Enhancement | Status |
|-------------|--------|
| Automated review trigger | ✅ SSE listener detects "ready for review", calls workflow_fn |
| Late-binding workflow_fn | ✅ Inject NAT workflow into SSE listener after construction |
| Multi-domain recall | ✅ Hub API accepts comma-separated domains: `?domain=architecture,security` |
| Phoenix trace view | ✅ Dashboard agent activity items link to Phoenix traces |
| Review iteration counter | ✅ Approval cards track review rounds, escalation warning at 3 |

## Phase 3 — Product Owner + PRD→Design Pipeline

### Product Owner Agent (port 8004)
- **Type:** Plain `react_agent` (no reasoning wrapper — simpler, more reliable)
- **Tools:** `web_search` (Tavily), `ask_knowledge`, `create_prd`
- **Domains:** `[product, requirements, research]`
- **Flow:** User asks PO → PO calls Tavily web search → PO calls `create_prd` → PRD published to document store

### `create_prd` Tool
- Accepts structured PRD sections (problem, goals, user stories, requirements, etc.)
- Applies markdown template programmatically (no LLM formatting needed)
- POSTs to hub `/api/v1/documents` endpoint
- PRD becomes available in dashboard Documents panel

### `create_design` Tool (RCTRO Structure)
- Accepts design sections (architecture, components, APIs, data models, security, deployment, testing)
- Uses RCTRO format: Role → Context → Task → Requirements → Output
- POSTs to hub `/api/v1/documents` endpoint
- Design doc becomes available in dashboard Documents panel

### Builder Agent Refactored
- **Type:** Plain `react_agent` with RCTRO prompt
- **Tools:** `ask_knowledge`, `create_design`
- **Flow:** User sends PRD reference → Builder asks architect → Builder asks security → Builder calls `create_design` → Design doc published

### Document-Triggered Workflow
- Dashboard Documents panel shows "📐 Send to Builder" button on each document
- Clicking opens builder chat with pre-filled message referencing the PRD
- Builder reads PRD reference, consults experts, produces design document

### Key Learnings
- **Nemotron Nano + `reasoning_agent`**: Planning phase works perfectly, but react executor ignores the plan and gives direct answers. Plain `react_agent` is more reliable for tool-calling workflows.
- **Markdown bold in tool names**: Nano outputs `**web_search**` instead of `web_search`. NAT's parser retries resolve this, but adding "no bold" to prompts reduces wasted retries.
- **`max_tokens: 16384`**: Required to prevent mid-sentence truncation (vLLM default is too low).
- **Tavily provider via OpenShell**: `openshell provider create --name tavily --type generic --credential TAVILY_API_KEY` injects API key into sandbox environment.

## Verification (Full Pipeline)

1. `./setup_nemoclaw.sh --rebuild`
2. Dashboard at localhost:8420 — 5 agent cards (architect, security, builder, product_owner, human)
3. Click Product Owner card → chat overlay
4. Send: "Research authentication patterns for IMDB identity service"
5. Watch: PO calls web_search (Tavily), then create_prd → PRD appears in Documents sidebar
6. Documents sidebar: PRD shows "📐 Send to Builder" button
7. Click "Send to Builder" → builder chat opens with pre-filled PRD reference
8. Send the message → builder asks architect + security → calls create_design
9. Design doc appears in Documents sidebar under "builder"
10. Agent activity items show 🔍 trace links → click opens Phoenix UI
11. Search API: `GET /api/v1/memories/search?q=auth&domain=architecture,security` returns merged results

## Risks & Mitigations

- **LLM prompt compliance:** Nemotron Nano 30B may struggle with 5-phase protocol. Mitigation: simplify prompt, test with Qwen 3.5 35B as fallback.
- **Human timeout:** Not an issue — `request_approval` is non-blocking (announce, not ask). Builder submits the plan and returns immediately. No timeout. Approvals persist in NCMS memory indefinitely. Human reviews whenever available. Builder resumes when it receives an approval announcement via SSE.
- **SSE reconnection:** Improve the SSE listener with:
  1. **Heartbeat detection** — if no SSE event received in 45s (server sends keepalive every 30s), force reconnect
  2. **Re-registration on reconnect** — already implemented (re-registers with hub on each SSE reconnect)
  3. **Exponential backoff with jitter** — current backoff is deterministic (1s, 2s, 4s...). Add random jitter (±25%) to prevent thundering herd when hub restarts with 3+ agents
  4. **Dashboard SSE auto-reconnect** — `app.js` detects `EventSource.onerror`, waits 3s, reconnects. Pending approvals reload from NCMS memory on reconnect (not lost)
  5. **Connection state indicator** — dashboard header shows green/yellow/red dot for SSE connection health
