# Dashboard Evolution: From Monitoring to Project Delivery

## Context

The NCMS dashboard today is an observability tool. It shows agent activity via SSE event streams, renders a D3 knowledge graph, provides per-agent chat overlays, and lists documents in a flat sidebar. It includes episode timelines, entity state history views, an admission scoring panel, and a floating approval queue. The backend is a Starlette application (`interfaces/http/dashboard.py`) serving REST endpoints and SSE streams, with a single-page frontend in `interfaces/http/static/index.html`.

This is effective for watching agents work. It is not effective for managing a portfolio of AI-driven design projects, tracking governance compliance across those projects, or giving a human approver enough context to make informed decisions about code generation.

This document describes twenty capabilities that transform the dashboard from a monitoring tool into a project delivery and governance compliance platform. Each section states the problem, describes the design, and outlines the implementation path. The phased delivery plan at the end groups them by dependency and impact.


## 1. Project/Epic View

### Problem

Documents appear as flat entries in the sidebar. There is no grouping by project, no phase tracking, and no visible relationship between a research report and the design it eventually produced. A user returning to the dashboard after a day away sees a list of documents with no narrative structure.

### Design

Each pipeline run creates a **Project** with a unique ID, topic, and creation timestamp. A project groups all documents by phase, establishing a clear progression: Research, PRD, Design (v1, v2, ...), Review Report, and Implementation. The project view replaces the flat document sidebar as the default.

**Project card.** Each project renders as a card showing the topic, phase progress as a row of checkmarks, total elapsed time, the latest quality score from review, and a count of knowledge-grounded references. Clicking the card expands it to reveal a phase timeline where each document appears with its size, creation timestamp, and inbound reference count.

**New project trigger.** A "New Project" button opens a structured trigger panel. The panel collects a topic string, a target description, and scope checkboxes selecting which phases to run (research only, research through design, full pipeline including implementation). Submitting the form creates the project record with a hub-generated `project_id` and triggers the first agent with the `project_id` embedded in the bus trigger message.

**Project ID propagation.** The hub generates a unique `project_id` when the trigger panel submits. This ID is included in the bus trigger message to the Researcher. Each agent extracts the `project_id` from its input, passes it through its LangGraph state, tags all published documents with it, and includes it in the trigger message to the next agent. This creates an unbroken chain: every document, review report, and contract links back to the originating project.

**Persistence.** Projects persist as NCMS memories with `type: "project"` and metadata linking to constituent document IDs. This means projects are searchable through the standard memory retrieval pipeline and benefit from entity extraction, episode linking, and all other NCMS features.

**Project list.** The list supports filtering by status (active, completed, failed, archived), sorting by recency or quality score, and text search across topics. Active projects sort to the top with a visual indicator.

**Archival.** Completed or abandoned projects can be archived from the dashboard. Archival marks the project as read-only and moves it to the archive view. Documents and memories are preserved (immutable for compliance) but excluded from active project lists and knowledge retrieval by default. Archived projects remain accessible for audit and compliance queries.

### Implementation

Backend:

- New REST endpoints on the dashboard Starlette app:
  - `POST /api/v1/projects` generates a `project_id`, creates a project memory with topic, scope, and phase configuration, and returns the ID.
  - `GET /api/v1/projects` returns all project memories with phase completion status derived from linked documents.
  - `GET /api/v1/projects/{id}` returns the full project detail including all linked documents grouped by phase.
  - `POST /api/v1/projects/{id}/archive` marks the project as archived.
- Each agent's publish step tags documents with a `project_id` field in the memory's `structured` metadata. The dashboard resolves these links at query time.
- Project creation calls `memory_service.store()` with the project memory, then dispatches a bus announcement to `trigger-researcher` with the `project_id` in the message payload.
- All LangGraph agents add `project_id` to their state TypedDict, extract it from input via regex, and include it in trigger announcements and document publish calls.

Frontend:

- New `projects.js` module replaces the document sidebar rendering logic. The module manages the project list, card rendering, phase timeline, trigger panel, and archive view.
- The trigger panel posts a structured JSON payload to the projects endpoint and subscribes to SSE events filtered by the new project ID.
- Project cards update in real time as SSE events arrive for documents tagged with matching project IDs.


## 2. Compliance Dashboard

### Problem

Review scores are buried inside individual review report documents. There is no aggregate view showing governance compliance across projects. A CIO asking "how compliant are our designs?" must open each review report individually and mentally aggregate the results.

### Design

A dedicated compliance tab, accessible from the header bar alongside the existing Graph, Episodes, and States buttons, provides aggregate governance visibility.

**ADR compliance matrix.** A table with rows for each ADR number (ADR-001, ADR-002, ...) and columns for each project. Each cell shows pass, fail, or partial status derived from the COVERED and MISSING sections of review reports. The matrix highlights ADRs that are frequently missed across projects.

**STRIDE coverage heat map.** A six-row grid (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege) by project columns. Cell color intensity maps to coverage level: green for fully addressed, amber for partially addressed, red for not addressed. Coverage levels are parsed from review reports that evaluate STRIDE categories.

**Quality trend chart.** A line graph showing the average review score per project over time. Each data point represents one review round. The chart reveals whether iterative review is improving quality and whether specific projects are trending downward.

**Knowledge grounding metrics.** Per-project counts of: memories retrieved during review, average expert response size, number of distinct domains consulted, and the ratio of grounded references to total references in the final document.

**Drift score.** A composite score per project computed as `100 - (critical * 15 + high * 5 + medium * 2 + low * 1)` from Looking Glass severity classifications. Higher scores indicate better compliance. Projects below a configurable threshold are flagged.

**Top findings.** The most frequently appearing items from MISSING and CHANGES sections across all review reports, ranked by occurrence count. This surfaces systemic gaps that no single project review would reveal.

**Export.** The compliance view is exportable as a PDF or Markdown file for inclusion in governance reporting workflows.

### Implementation

Backend:

- `GET /api/v1/compliance/summary` aggregates data across all project memories. It parses review report documents to extract SCORE, SEVERITY, COVERED, MISSING, and CHANGES fields using structured markers that the review agents already produce.
- The endpoint returns a JSON payload with the full matrix, heat map data, trend series, grounding metrics, and top findings.

Frontend:

- New `compliance.js` module renders the compliance tab using D3 (already loaded for the graph view) for the heat map and trend chart. The ADR matrix and findings table render as styled HTML tables.
- Data refreshes on tab activation and can be manually triggered. No SSE subscription needed since compliance data changes only when reviews complete.


## 3. Live Pipeline Progress

### Problem

During an active pipeline run, the user watches the general activity feed and waits. There is no clear indication of which phase is active, how long it has been running, or what the pipeline will do next. The current pipeline panel shows "No pipeline activity yet" until events arrive, and events appear as an undifferentiated stream.

### Design

A horizontal pipeline progress bar renders at the top of the project view whenever a project has an active run. The bar shows the full node-level sequence for each phase, not just the phase name. This gives visibility into exactly where the pipeline is within each agent's LangGraph:

```
Research: [plan ✓] [search ✓] [synthesize ████░░] [publish] [trigger]
PRD:      [read_doc] [ask_experts] [synthesize] [publish] [trigger]  (waiting)
Design:   [read_doc] [ask_experts] [synthesize] [validate] [publish] [review] [contracts]  (waiting)
```

Each node shows its status: completed (checkmark), active (progress bar), waiting (dimmed), or failed (red). The `validate` node (completeness check) and `contracts` node (OpenAPI/Zod generation) appear as visible steps in the Design phase so the user sees the spec quality checks happening.

**Phase detail.** Hovering over any node shows a tooltip with the agent name, elapsed time, and output size (growing in real time during active generation).

**Review round tracking.** The Review node expands to show round-by-round progress. For example, "R1: Arch 88% / Sec 78% [pass]" where each reviewer's score is visible. If revision occurs, the bar shows the loop: Review → Revise → Publish → Review.

**Completeness validation visibility.** The `validate` node in the Design phase shows the check results: "7/9 sections present, 5 endpoints, 4 interfaces, 2 PRD gaps found." If it fails, the user sees the specific gaps before the LLM fix pass runs.

**Contract generation visibility.** The `contracts` node shows "Generating OpenAPI 3.1 + Zod schemas" and on completion "3 contracts published: openapi.yaml, validation.ts, migrations.sql."

**Time estimation.** Estimated time remaining for the active node is computed from a rolling average of previous runs stored in the browser's localStorage. After three or more completed runs, the estimate becomes reasonably accurate.

**Lightweight pipeline telemetry.** Node-level events use a dedicated telemetry channel, not the knowledge bus. The bus (`bus_announce`) is reserved for meaningful events that should persist as memories: document publications, review scores, handoff triggers. Pipeline telemetry (node started, node completed, progress updates) is ephemeral and should not clutter the memory store.

A new hub endpoint accepts lightweight telemetry and relays it via SSE:

```
POST /api/v1/pipeline/events
{"project_id": "abc", "agent": "builder", "node": "synthesize", "status": "started", "timestamp": "..."}

# Dashboard receives via SSE:
event: pipeline.node
data: {"project_id": "abc", "agent": "builder", "node": "synthesize", "status": "started"}
```

No memory storage. No entity extraction. No indexing. Just an event relay. Each LangGraph node calls this endpoint at entry and exit. The dashboard subscribes to `pipeline.node` events for the active project.

This separation also cleans up the existing system: several current `bus_announce` calls that exist only for dashboard visibility (e.g., "Querying architecture and security experts") should move to the telemetry channel, reducing noise in the memory store.

**Node interrupt and retry.** Each node in the progress bar is clickable. Clicking an active node shows two options: "Interrupt" (stop this node, publish partial results, skip to the next phase) and "Retry" (re-run this node from its inputs). The interrupt sends a cancellation signal to the agent via a bus announcement to `interrupt-{agent_id}`. The agent's LangGraph checks for interrupt signals between nodes and exits cleanly if one is received. Retry re-triggers the same node with the same state.

**Failure states.** Failed nodes display in red with the error message in the tooltip. The progress bar shows the pipeline as "failed at [node]" with a retry button. If the review loop exhausts max iterations, the Review node shows amber with "Accepted at X% (below threshold)" and the pipeline continues to the next phase.

### Implementation

Backend:

- New `POST /api/v1/pipeline/events` endpoint on the hub. Ring buffer in memory (last 1000 events per project). SSE broadcast to subscribed dashboard clients.
- New `interrupt-{agent_id}` bus domain. Agents subscribe to their interrupt domain. LangGraph nodes check for interrupt signals at entry via a shared interrupt flag in the agent state.

Frontend:

- New `pipeline-progress.js` module subscribes to both the main SSE stream (for bus events) and the pipeline telemetry stream (for node events).
- Phase transitions inferred from telemetry events: node "trigger" completing in the Researcher marks Research complete and PRD as active.
- Time estimates stored in localStorage keyed by phase and node name. Each completed node updates the rolling average.
- The progress bar DOM element is injected at the top of the expanded project card when a run is active.
- Click handlers on active nodes show interrupt/retry options. Interrupt sends a bus announcement. Retry re-POSTs to the agent's `/generate` endpoint.


## 4. Document Diff View

### Problem

When the Builder agent revises a design after review feedback, each version is stored as a separate document. The user can read version 1 and version 2 independently, but there is no way to see what changed between them or why those changes were made.

### Design

**Version selector.** When multiple documents share the same project ID and phase (e.g., two Design documents), the document detail view shows a version selector dropdown. Selecting a previous version activates the diff view.

**Side-by-side diff.** The default diff mode shows the previous version on the left and the current version on the right, with additions highlighted in green, modifications in yellow, and deletions in red. Line-level alignment keeps corresponding sections adjacent.

**Inline diff mode.** An alternative single-column view shows the merged document with inline change markers. This is more compact and works better for small changes.

**Review comment annotations.** The Builder agent embeds structured comments in revised documents following the pattern `<!-- Rev N: Addressed arch change #3 -->`. The diff view parses these markers and renders them as annotations linked to the specific CHANGES items from the review report that prompted them. Clicking an annotation scrolls to the corresponding review finding.

**Summary header.** Above the diff, a summary line states: "Version 2: +3,200 characters, addressed 4/5 architecture items, 3/3 security items." The counts are derived by matching revision annotations against the review report's CHANGES list.

### Implementation

This is a frontend-only feature. The existing document detail API already returns full document content, so diffs are computed client-side.

- New `diff-viewer.js` module uses a JavaScript diff library (jsdiff or diff-match-patch, both lightweight and dependency-free) to compute character-level or line-level diffs.
- Revision annotations are extracted by parsing `<!-- Rev N: ... -->` patterns from the document content using a simple regex.
- The version selector queries the project detail API for documents matching the same project and phase, then sorts by creation timestamp.
- The diff view replaces the standard document content panel when the user selects a comparison version.


## 5. Knowledge Grounding Inspector

### Problem

Documents reference standards like "ADR-003" or "THR-012" or "OWASP A01:2021", but there is no way to verify what the agent actually retrieved from NCMS memory when producing that citation. The reference might be accurately grounded in retrieved knowledge, or it might be an LLM hallucination with no backing memory.

### Design

**Clickable references.** The document renderer detects reference patterns (ADR-NNN, THR-NNN, OWASP ANNN, RFC NNNN, and custom patterns) and renders them as clickable links. The detection uses a configurable set of regex patterns.

**Grounding popup.** Clicking a reference opens a popup panel showing the actual memory content that backs that reference. The popup displays: the full memory text (truncated to 500 characters with an expand option), the BM25 and SPLADE relevance scores, the agent that stored the memory, the timestamp, and the query string that triggered the retrieval (if captured in the search log).

**Provenance chain.** Below the memory content, the popup shows the full provenance chain: the source file that was loaded into NCMS, the memory it became, the expert query that retrieved it, the expert response that cited it, and the downstream document that incorporated it. Each link in the chain is clickable, opening the corresponding memory or document.

**Grounding score.** Each document receives a grounding score: the percentage of detected references that map to actual NCMS memories retrieved during the pipeline run versus references that appear to be generated without retrieval backing. A score of 85% means 85% of citations trace to real memories. The score appears in the document header and in the project card.

### Implementation

Backend:

- The memory search API (`GET /api/v1/memories/search`) already exists. The grounding inspector calls it with the reference text as the query and the appropriate domain filter.
- A new lightweight endpoint `GET /api/v1/grounding/{project_id}/{document_id}` computes the grounding score by comparing detected reference patterns in the document against search log entries from the pipeline run. This requires correlating document timestamps with search log timestamps.

Frontend:

- New `grounding-inspector.js` module attaches click handlers to detected reference patterns in rendered documents.
- On click, it fetches from the memory search API and renders the popup with the retrieved memory, scores, and provenance chain.
- The grounding score computation runs once when a document is opened and caches the result for the session.


## 6. Contextual Approval Queue

### Problem

The current approval panel (`approval-float` in the dashboard) is a narrow floating panel that shows pending approvals with minimal context. For a human-in-the-loop gate before code generation, the approver needs to see the full design, the review results, and the estimated impact of approving before making a decision.

### Design

**Full-width approval view.** When the user opens an approval, it renders as a full-width overlay (similar to the existing graph or episode overlays) rather than a narrow sidebar float.

**Context panels.** The approval view contains three panels arranged vertically:

1. **Design document** (expandable/collapsible): The approved design document that the coding agent will implement. Rendered with the same formatting as the document detail view, including clickable references.
2. **Review report with scores**: The final review report showing architecture and security scores, with COVERED, MISSING, and CHANGES sections. Items marked as addressed in the latest revision are highlighted.
3. **Implementation summary**: A generated summary of what the coding agent will do: files to create, frameworks to use, estimated complexity, and any dependencies.

**Cost estimation panel.** Below the context panels, a cost estimation section shows: estimated token count for the implementation prompt, estimated number of LLM calls (based on code generation plus up to three test-fix iterations), and estimated wall-clock time (based on historical averages from previous coding runs).

**Approval history.** A collapsible section at the bottom shows previous approval decisions for this project, including timestamps, the approver's comments, and whether the decision was approve, reject, or request-changes.

**Decision actions.** Three buttons: Approve (green), Reject (red), and Request Changes (amber). Each opens a comment text field. Approve triggers the coding agent. Reject notifies the Builder with the feedback comment. Request Changes returns the design to the Builder for revision with specific guidance.

**Audit trail.** Every approval decision is stored as an NCMS memory with `type: "approval_decision"` and metadata containing the project ID, decision type, comment text, and timestamp. This creates a searchable audit trail.

### Implementation

Backend:

- Approval decisions POST to `/api/v1/bus/announce` with domain `approval-response`, carrying the decision type, project ID, and comment. This is the existing bus announcement mechanism.
- A new approval type `implementation_approval` supplements the existing `plan_approval` type, carrying the additional context fields (estimated cost, review summary).
- Storing the audit trail uses the existing `memory_service.store()` with the approval decision memory.

Frontend:

- Extend the existing `approvals.js` (or the approval float logic in the main JS) with the full-context rendering. The overlay structure follows the same pattern as `episode-overlay` and `graph-overlay` in `index.html`.
- Cost estimation data is fetched from a new `GET /api/v1/projects/{id}/implementation-estimate` endpoint that computes estimates based on the design document size and historical data.


## 7. Telegram Integration

### Problem

Starting a pipeline run or approving an implementation currently requires the user to be at the dashboard. For teams where the decision-maker is mobile, a Telegram interface provides accessibility without requiring a browser.

### Design

**Prompt submission.** A Telegram bot accepts research prompts via a `/research` command. For example: `/research authentication patterns for IMDB`. The bot creates a new project via the hub API and confirms receipt with the project ID.

**Progress updates.** As the pipeline progresses, the bot sends phase completion messages: "Research complete (12,400 chars). PRD generation started." Messages are concise, one per phase transition, avoiding notification fatigue.

**Completion summary.** When the pipeline finishes (or fails), the bot sends a summary with the topic, final quality score, key findings count, and a deep link to the dashboard project view.

**Mobile approval.** When an approval is pending, the bot sends a message with the implementation summary and an inline keyboard with Approve, Reject, and Request Changes buttons. Tapping a button posts the decision to the hub API and confirms the action.

### Implementation

- A Python Telegram bot using the `python-telegram-bot` library, running as a separate async process alongside the dashboard (or as a standalone service).
- The bot authenticates with a Telegram bot token (stored in environment configuration, not in code).
- Prompt submission: POST to the projects API endpoint (the same one the dashboard trigger panel uses).
- Progress updates: The bot maintains an SSE listener on the dashboard event stream, filtering for events tagged with the project ID. Phase transitions are detected using the same logic as the pipeline progress bar.
- Approval: The inline keyboard callback posts to `/api/v1/bus/announce` with domain `approval-response`.
- Configuration: `NCMS_TELEGRAM_BOT_TOKEN` and `NCMS_TELEGRAM_CHAT_ID` environment variables. The chat ID restricts the bot to authorized users.


## 8. Coding Agent (Claude Code)

### Problem

The current pipeline ends at design and review. Approved designs must be implemented manually. A coding agent completes the pipeline by translating reviewed designs into working code.

### Design

**Agent architecture.** A new LangGraph agent (the sixth in the system, alongside Researcher, PRD Writer, Builder, Architecture Reviewer, and Security Reviewer) that runs inside a NemoClaw sandbox with Claude CLI access. The sandbox provides file system isolation and network restrictions appropriate for code generation.

**Pipeline.** The coding agent follows a deterministic five-node LangGraph pipeline:

1. **read_design**: Parse the approved design document. Extract the file structure, component list, API contracts, and any implementation notes from the review.
2. **scaffold_project**: Create the directory structure and boilerplate files (package.json, tsconfig, test configuration) based on the design specifications.
3. **generate_code**: Invoke Claude CLI with the design document as context and implementation instructions. Claude produces source files for each component.
4. **run_tests**: Execute the test suite (`npm test`, `pytest`, or the appropriate runner for the target language). Capture stdout, stderr, exit code, and coverage metrics.
5. **publish_results**: Package the source code, test results, and coverage report. Publish them to the document store as a new project phase. Announce completion on the bus.

**Test-fix loop.** If tests fail in step 4, the agent extracts error messages, passes them back to Claude CLI with the failing test output as context, and re-runs generation. This loop executes a maximum of three times. If tests still fail after three iterations, the agent publishes partial results with a failure report and requests human intervention.

**Approval gate.** This agent does not start automatically. It waits for an `implementation_approval` announcement on the bus (produced by the contextual approval queue). This ensures a human reviews and approves every design before code generation begins.

### Implementation

- New agent module at an appropriate location in the agents package (e.g., `demo/agents/coding_agent.py` for the demo, or in the multi-agent orchestration package for production).
- Claude CLI is invoked via `subprocess.run()` inside the NemoClaw sandbox. The design document is passed as a file argument, and implementation instructions are passed via stdin or a prompt file.
- Test execution uses `subprocess.run()` with the appropriate test command, capturing output for analysis.
- Results are published as NCMS memories with `type: "implementation"` and the project ID in metadata.
- The agent registers on the bus with domain `implementation` and subscribes to `approval-response` announcements.


## 9. Resilience Improvements

### Problem

The current system has several reliability gaps. Port forwards to sandboxed agents can drop without recovery. A stuck agent blocks the entire pipeline. Observability depends on reading logs rather than structured telemetry. Agent health is not monitored. Review failures halt the pipeline entirely.

### Design

**Port forward auto-recovery.** A background health check task runs every 30 seconds, probing each agent's health endpoint through its port forward. If the health check fails twice consecutively, the system automatically tears down and re-establishes the port forward. The dashboard shows a brief "reconnecting" indicator on the affected agent card.

**Agent interruptability.** Each agent card in the dashboard gains a "Stop" button. Pressing it sends a cancellation signal via bus announcement with domain `agent-control` and action `cancel`. The agent's LangGraph pipeline checks for cancellation at each node boundary. On cancellation, the agent publishes whatever partial results it has produced and announces the interruption. The pipeline progress bar shows the interrupted phase in amber.

**Phoenix event enrichment.** Custom OpenTelemetry spans wrap five key operations: `bus_ask`, `bus_announce`, `memory_store`, `memory_search`, and `document_publish`. Each span carries structured attributes (agent ID, domain, query text, result count, latency). These spans provide complete pipeline visibility in Phoenix without reading application logs.

**Agent health monitoring.** Agents emit heartbeat announcements every 30 seconds via the bus. The dashboard tracks the last heartbeat timestamp per agent. If no heartbeat arrives within 60 seconds, the agent card shows an "unresponsive" badge. If the agent runs in a sandbox, the process supervisor attempts an automatic restart. The health status is also exposed via the agents REST endpoint.

**Graceful review degradation.** If one reviewer agent fails after its configured retry limit, the pipeline continues with the available review only. The review report clearly notes which reviewer was unavailable. If both reviewers fail, the pipeline pauses and requests human review through the approval queue, providing the design document for manual inspection.

### Implementation

- Port forward recovery: A background `asyncio.Task` in the dashboard (or hub) process that calls each agent's health endpoint and manages port forward lifecycle.
- Agent interruptability: A new bus domain `agent-control` with action types `cancel` and `resume`. Agents check a cancellation flag between LangGraph nodes.
- OpenTelemetry spans: Wrap existing bus and memory service calls with `tracer.start_as_current_span()`. The NCMS library already has no OpenTelemetry dependency, so this is added as an optional integration (import guarded by try/except).
- Heartbeat: Add a periodic task in `KnowledgeAgent.start()` that calls `bus_service.announce()` with domain `agent-health`.
- Review degradation: Modify the review orchestration logic to catch reviewer failures and proceed with partial results when possible.


## 10. Looking Glass Governance Mesh

### Problem

Expert agents currently load knowledge from static files. The governance standards they enforce are frozen at deployment time. In an enterprise environment, governance artifacts evolve continuously. ADRs are created, threat models are updated, compliance checklists change. Static knowledge files create drift between what agents enforce and what the organization actually requires.

### Design

**BAR artifact integration.** Connect to the Looking Glass governance mesh via MCP (Model Context Protocol) servers. Each application in the enterprise has a Business Application Record (BAR) containing CALM architecture models, STRIDE threat models, ADRs, fitness functions, compliance checklists, and operational runbooks. Expert agents load BAR artifacts as their knowledge base, replacing static knowledge files.

**Four-pillar governance.** The review pipeline expands from two reviewers (architecture and security) to the full Oraculum four-pillar model: Architecture, Security, Information Risk, and Operations. Each pillar has its own expert agent (or the existing experts are extended with additional pillar responsibilities). Reviews evaluate designs against all four pillars with pillar-specific scoring criteria.

**Dynamic knowledge refresh.** BAR artifacts are refreshed at the start of each pipeline run. The MCP server provides versioned artifacts, and the agent compares versions to detect changes since the last run. Changed artifacts trigger a knowledge reload. This ensures reviews always use current governance standards.

**Cross-pillar drift analysis.** The compliance dashboard gains a cross-pillar view showing how a design decision in one pillar affects compliance in others. For example, a caching decision that improves operational performance might introduce information risk. Drift scores are computed across all four pillars with dependency edges.

**Results integration.** All four-pillar review results feed into the Compliance Dashboard described in Section 2, populating the ADR matrix, STRIDE heat map, and drift scores with data from the full governance mesh rather than just architecture and security.

### Implementation

- MCP server integration: A new infrastructure module (`infrastructure/mcp/looking_glass.py`) that connects to Looking Glass MCP servers and retrieves BAR artifacts. The module implements the existing knowledge loading protocol so it can replace file-based loading without changing the agent layer.
- BAR artifact loader: Modify the agent registration flow to accept an MCP source as an alternative to `knowledge_paths`. At registration time, the loader pulls artifacts from the MCP server, stores them as NCMS memories, and indexes them for retrieval.
- Extended review prompts: Each pillar's review prompt template is stored as a BAR artifact itself, enabling the organization to customize review criteria without code changes.
- New review report format: Review reports gain a `pillar` field and per-pillar scoring. The compliance summary endpoint aggregates across pillars.
- Cross-pillar drift analysis: A new scoring function that computes dependency-weighted drift across pillar pairs, surfaced in the compliance dashboard.


## 11. Spec Quality: Completeness Validation and Interface Contracts

### Problem

The review loop scores designs holistically (architecture 88%, security 78%) but does not validate structural completeness. A design might score well because it describes JWT validation eloquently, while quietly missing an entire API endpoint, omitting error response schemas, or referencing a PRD requirement that has no corresponding implementation detail. The coding agent needs a structurally complete specification, not just a well-written one.

### Design

Two new nodes added to the Builder's LangGraph pipeline, between `synthesize_design` and `publish_design`:

**Completeness validation (Python, no LLM):**

A deterministic check that validates the design against both structural rules and a machine-readable requirements manifest from the PRD.

**Requirements manifest.** The PO's `publish_prd` node produces a structured JSON manifest alongside the prose PRD:

```json
{
  "project_id": "abc123",
  "type": "requirements_manifest",
  "endpoints": [
    {"method": "POST", "path": "/auth/login", "description": "User login"},
    {"method": "POST", "path": "/auth/refresh", "description": "Token refresh"},
    {"method": "POST", "path": "/auth/revoke", "description": "Token revocation"},
    {"method": "GET", "path": "/auth/me", "description": "Current user profile"},
    {"method": "POST", "path": "/auth/register", "description": "User registration"}
  ],
  "security_requirements": ["token_revocation", "rate_limiting", "mfa", "password_hashing"],
  "technology_constraints": ["TypeScript", "NestJS", "MongoDB", "Redis"],
  "quality_targets": {"latency_p99_ms": 200, "availability": "99.9%"}
}
```

The PO's LLM generates this JSON as a second output from the same synthesis step. The prose PRD is for humans. The manifest is for machines.

**Completeness checks against the manifest:**

- **Section presence:** Every required section (Project Structure, API Endpoints, Data Models, Authentication, Security Controls, Configuration, Error Handling, Testing, Deployment) must exist as a heading.
- **Code examples:** Every section must contain at least one code fence. Prose without code is flagged.
- **Endpoint coverage:** Design endpoints (extracted via regex) are compared against the manifest's endpoint list. Missing endpoints are reported by name and path.
- **Security requirement coverage:** Each item in the manifest's `security_requirements` array must appear in the design text. "PRD requires token_revocation but design does not mention revocation, revoke, or revocation list."
- **Technology alignment:** Design must reference the technologies in `technology_constraints`. A manifest saying "NestJS" but a design importing Express is flagged.
- **Interface definitions:** Count TypeScript `interface` declarations. A design with fewer interfaces than endpoints is suspect.
- **Environment variables:** The configuration section must document env vars.
- **Error response format:** The error handling section must define status codes.

If the check finds issues, it returns them to a targeted `fix_gaps` LLM node that receives only the specific gaps (not the full design). This avoids full re-synthesis for minor structural gaps.

**Interface contract generation (LLM, after final approval only):**

Contracts are generated once, after the design passes the review loop. If the design went through revisions, contracts reflect the final approved version only. This avoids wasting LLM calls generating contracts for designs that will be revised. A final LLM pass produces machine-parseable contracts:

- **OpenAPI 3.1 YAML:** Every API endpoint with request/response schemas, status codes, error responses, authentication requirements.
- **Zod validation schemas:** TypeScript-native validation for every request body and query parameter, directly importable by the coding agent.
- **Database migration scripts:** Schema definitions derived from the Data Models section, ready for Prisma, TypeORM, or raw SQL.

These contracts are published as separate documents alongside the design. The coding agent consumes the contracts as specifications instead of parsing prose.

### Implementation

- `validate_completeness` node: pure Python, added to the Builder's LangGraph between `synthesize_design` and `publish_design`. Uses regex and string matching. No LLM cost. Runs in under 1 second.
- `generate_contracts` node: LLM pass, added after the review loop approves the design. Produces OpenAPI YAML and Zod schemas. Published as separate documents with `type: "contract"` metadata.
- Conditional edge: if completeness check fails, loop back to a targeted `fix_gaps` LLM node that receives only the specific issues (not the full design), then re-validates. Max 2 fix iterations.
- The completeness check is configurable via `min_endpoints`, `min_interfaces`, `required_sections` in the builder config.

### Why This Matters

The hypothesis is that the spec is the single most important artifact. A coding agent with a perfect spec produces good code. A coding agent with a vague spec produces garbage. The completeness checker ensures structural rigor (every section present, every endpoint typed, every requirement traced). The contract generator produces machine-parseable output (OpenAPI, Zod) that eliminates ambiguity. Together, they transform the design from "a document a human can read" to "a specification a machine can execute."


## 12. NemoGuardrails: Pipeline Policy Enforcement

### Problem

The pipeline builds whatever you ask it to. There is no validation that a research topic aligns with organizational scope, that generated designs stay within approved technology stacks, or that outputs comply with organizational policies. A user could prompt "Research cryptocurrency mining optimization" and the pipeline would happily produce a PRD and implementation design for it.

### Design

[NemoGuardrails](https://github.com/NVIDIA/NeMo-Guardrails) provides programmable policy enforcement at three checkpoints in the pipeline:

**Input guardrails (before research starts):**
- Topic scope validation: does the research topic fall within the organization's approved domains? Configurable allow-list (e.g., "identity services, authentication, authorization, data access") and deny-list (e.g., "cryptocurrency, gambling, weapons").
- Technology scope validation: if the prompt specifies a technology stack, verify it aligns with approved stacks from ADRs.
- Sensitivity screening: flag topics that require elevated approval (e.g., PII handling, financial data, healthcare compliance).

**Process guardrails (during pipeline execution):**
- Expert response validation: verify that architect and security responses cite actual knowledge base content, not hallucinated standards or invented ADR numbers.
- Design constraint enforcement: the Builder's implementation design must use approved frameworks, libraries, and patterns. Flag designs that introduce unapproved dependencies.
- Token budget enforcement: prevent runaway LLM calls by capping total tokens per pipeline run.

**Output guardrails (before document publication):**
- Secret detection: scan all documents for hardcoded credentials, API keys, connection strings, or PII before publishing.
- Prohibited pattern detection: flag designs that include known anti-patterns (SQL string concatenation, eval() usage, disabled CORS, wildcard permissions).
- Compliance checklist: verify the final design addresses all mandatory compliance requirements from the organization's governance baseline.

### Policy Storage and Management

Guardrails policies must be living, editable configurations, not hardcoded files. They are stored in NCMS as versioned documents with `type: "policy"` and managed from the dashboard:

**Domain policies** define what the organization works on:
```yaml
# Stored in NCMS as policy document, editable from dashboard
policy_type: domain_scope
allowed_domains:
  - identity services
  - authentication and authorization
  - data access and APIs
  - microservice architecture
  - observability and monitoring
denied_domains:
  - cryptocurrency
  - gambling
  - weapons systems
elevated_approval:
  - PII handling
  - financial data processing
  - healthcare compliance (HIPAA)
```

**Technology policies** define what the organization builds with:
```yaml
policy_type: technology_scope
approved_stacks:
  backend:
    - TypeScript / NestJS
    - TypeScript / Express
    - Python / FastAPI
  database:
    - MongoDB
    - PostgreSQL
  authentication:
    - JWT with RS256 signing
    - OAuth 2.0 with PKCE
  hashing:
    - bcrypt (cost >= 12)
    - Argon2id
prohibited:
  - eval() or Function() constructor
  - SQL string concatenation
  - wildcard CORS origins in production
  - HTTP without TLS
```

**Compliance policies** define what must be present:
```yaml
policy_type: compliance_requirements
mandatory_sections:
  - authentication middleware
  - rate limiting
  - input validation
  - error handling with status codes
  - environment variable documentation
  - health check endpoint
  - audit logging
standards:
  - OWASP ASVS v5.0 Level 2
  - NIST SP 800-63B
```

**Configurable escalation.** Each policy rule has an escalation level:
- **Warn:** Pipeline continues but the violation is flagged in the project card, the pipeline progress bar shows an amber warning, and the compliance dashboard records it. Use for process guardrails where the team should be aware but not blocked.
- **Block:** Pipeline pauses at the violation point and waits for human override. The approval queue shows the violation with context and an "Override" button. Use for output guardrails like secret detection.
- **Reject:** Pipeline cancels and the project is marked failed with the violation reason. Use for input guardrails like denied domains.

Escalation levels are configurable per policy rule, not globally. A domain policy might reject "cryptocurrency" but warn on "blockchain infrastructure."

**Dashboard policy editor.** A dedicated "Policies" tab, restricted to admin users, where policies can be viewed, edited, and versioned. Changes take effect on the next pipeline run. Policy version history shows what changed, when, and by whom, with links to the pipeline runs that used each version.

**Policy inheritance.** Organization-level policies apply to all projects by default. Individual projects can add stricter constraints (never weaker). For example, a healthcare project inherits the base technology policy but adds HIPAA compliance requirements.

### Implementation

- New document type `policy` in NCMS with metadata: `policy_type`, `version`, `scope` (organization or project-level)
- Hub API: `GET /api/v1/policies?type=domain_scope` to fetch active policies, `PUT /api/v1/policies/{id}` to update
- NemoGuardrails loads policies from NCMS at pipeline start (not from static Colang files). The Colang rules reference NCMS policy documents as dynamic data sources.
- A guardrails wrapper around each LangGraph pipeline's entry point validates inputs against domain and technology policies before the graph runs.
- A guardrails check node after each `publish` step validates outputs against compliance and technology policies before the document is made visible.
- Policy violations are announced to the bus and surfaced in the dashboard as warnings with specific remediation guidance and a link to the policy that was violated.
- The compliance dashboard tracks guardrail violations per project, per policy, and across the portfolio.
- Dashboard: "Policies" tab with YAML editor, version history, and per-project override support.

### Integration with Existing Pipeline

The guardrails layer sits between the hub's `/generate` proxy and the agent's LangGraph pipeline. It does not modify the graph itself. This means guardrails can be enabled or disabled per agent via configuration, and the pipeline continues to work identically when guardrails are off. The goal is policy as an overlay, not policy embedded in agent logic. Policies stored in NCMS are versioned and auditable, and the audit trail (feature 16) records which policy version was active for each pipeline run.


## 13. Template Library

### Problem

Every pipeline run starts from scratch. The Builder generates authentication middleware, error handling, health checks, and configuration patterns from nothing each time. Most services share common structural patterns. The Builder wastes tokens regenerating what already exists, and inconsistencies creep in across projects because each generation is independent.

### Design

A versioned template library stored in the NCMS document store with `type: "template"`. Templates are reusable design fragments:

- **Middleware templates:** authentication, rate limiting, error handling, request validation, CORS configuration
- **Infrastructure templates:** Dockerfile, docker-compose, CI pipeline, health check endpoint
- **API patterns:** pagination envelope, error response format, authentication header contract
- **Test patterns:** unit test scaffolding, integration test setup, mock strategies

The Builder's `synthesize_design` node queries the template library before generating each section. If a matching template exists, the Builder adapts it to the current project context instead of generating from scratch. Templates are tagged with technology stack (TypeScript/NestJS, Python/FastAPI) and domain (authentication, data access, messaging).

New templates are created automatically: when a design passes review at 90%+ and the human approves it for the template library, its sections are extracted and stored as reusable templates. The library grows organically from successful pipeline runs.

### Implementation

- New document type `template` in the hub's document store with metadata: `stack`, `domain`, `section`, `version`
- Hub API: `GET /api/v1/templates?stack=typescript&domain=authentication` to query matching templates
- Builder's `synthesize_design` prompt includes relevant templates as context: "Here is an approved template for authentication middleware. Adapt it to this project's requirements."
- Template extraction: a post-approval node in the Builder graph that offers to extract sections from high-scoring designs into the template library
- Dashboard: template browser in the project view showing available templates by domain


## 14. Design Pattern Library (Mined from Historical Runs)

### Problem

After running the pipeline across multiple services, the published designs contain recurring implementation patterns. These patterns are trapped inside individual documents. There is no way to surface "every design we produced uses RS256 JWT signing with 15-minute expiry" as a discoverable organizational pattern.

### Design

A pattern mining system that analyzes historical designs and extracts recurring patterns:

- **Automatic detection:** NCMS's knowledge consolidation (Phase 5: recurring pattern detection) clusters design documents by topic entity overlap and extracts common patterns with stability-based promotion
- **Pattern catalog:** each discovered pattern becomes a searchable entry with: pattern name, frequency (how many designs use it), example implementations, and the specific projects that contributed to it
- **Builder integration:** the Builder's synthesis prompt includes "These patterns are established across the organization" context, promoting consistency
- **Drift detection:** when a new design deviates from an established pattern, the review flags it. Deviation is not necessarily wrong, but it requires justification.

This transforms NCMS from "memory for one pipeline run" to "organizational knowledge that improves over time." The dream cycle (Phase 8 in CLAUDE.md) already supports importance drift and co-occurrence edge generation. Applying this to design documents would surface the patterns naturally.

### Implementation

- Extend NCMS consolidation to run across documents with `type: "design"`
- New document type `pattern` with metadata: `frequency`, `contributing_projects`, `stability_score`
- Hub API: `GET /api/v1/patterns?domain=authentication` to query established patterns
- Compliance dashboard integration: pattern adherence rate across projects
- Builder prompt injection: top patterns for the relevant domain included as context


## 15. Prompt Library (Agent Prompt Management)

### Problem

Agent prompts are hardcoded in YAML config files and Python source code. Changing a prompt requires editing code, rebuilding sandboxes, and redeploying. There is no versioning, no A/B testing, and no way to see which prompt produced which result. When a prompt change improves security review scores from 78% to 92%, that improvement is invisible in the commit history.

### Design

A managed prompt library where each agent's prompts are versioned, testable, and traceable:

- **Prompt registry:** all agent prompts (system prompts, synthesis prompts, review prompts, revision prompts) stored in the hub as versioned documents with `type: "prompt"`
- **Version tracking:** each prompt version is linked to the pipeline runs that used it and the review scores those runs produced
- **Dashboard editor:** edit prompts in the dashboard with live preview. Save creates a new version. Rollback to any previous version.
- **Prompt performance metrics:** which prompt version produces the highest average review scores? Which prompt produces the largest designs? Which prompt grounds the most knowledge references?
- **Fail-fast startup:** agents fetch prompts from the hub at startup. If the hub is unreachable, the agent fails to start rather than falling back to embedded prompts. This makes the prompt library authoritative — the hub is the single source of truth for agent behavior.

**Future (not Phase 1):** A/B comparison — run the same research topic with two different prompt versions and compare the resulting design quality scores side by side. This requires parallel pipeline execution which adds significant complexity.

### Implementation

- New document type `prompt` with metadata: `agent_id`, `prompt_type` (system, synthesis, review, revision), `version`
- Agent configs reference prompt IDs instead of inline text: `synthesis_prompt: prompt://builder/synthesis/v3`
- At startup, agents fetch their prompts from the hub. If the hub is unreachable, the agent fails to start with a clear error message. This is a hard dependency, not a graceful degradation.
- Hub API: `GET /api/v1/prompts?agent=builder&type=synthesis` to list versions
- Dashboard: prompt editor tab with version history and performance metrics


## 16. Audit Trail and Reproducibility

### Problem

Can you reproduce a pipeline run from three months ago? The answer is no. Web search results change daily. LLM output is non-deterministic. Expert memories evolve as new knowledge is seeded. Review scores vary with memory retrieval results. For compliance and governance, you need a frozen snapshot of exactly what happened: every input, every retrieved memory, every LLM response, and every review score.

### Design

Each pipeline run produces a complete audit record:

- **Run manifest:** a JSON document capturing the full execution trace: project ID, timestamp, each node's input and output, LLM prompts and responses (with token counts), memory queries and retrieved results, review scores and feedback, document IDs published
- **Memory snapshot:** the exact memories retrieved during each `bus_ask` and review, with their NCMS memory IDs and content at the time of retrieval
- **Input freeze:** for the Researcher, the exact Tavily search results (URLs, content, scores) are preserved. Even if those web pages change or disappear, the audit record shows what the agent saw.
- **LLM response capture:** every LLM call's full prompt and response, including thinking tokens if enabled, token usage, and latency
- **Reproducibility score:** a metric indicating how reproducible the run is. A run with no web search (all from seeded knowledge) scores higher than one that depends on live web results.

NCMS's bitemporal fields (`observed_at`, `ingested_at`) provide the foundation. The audit trail extends this to the pipeline level.

### Implementation

- Each LangGraph node emits structured audit events via bus announcement with `type: "audit"`
- New document type `audit_trail` published at pipeline completion with the full run manifest
- Hub API: `GET /api/v1/projects/{id}/audit` returns the complete audit record
- Dashboard: "Audit" tab on each project showing the execution timeline with expandable detail at each node
- Phoenix integration: link each audit node to its corresponding Phoenix trace span
- Retention policy: audit records are immutable and retained per organizational compliance requirements


## 17. Knowledge Lifecycle Management

### Problem

Expert knowledge is seeded once at startup from static files in `knowledge/architecture/` and `knowledge/security/`. When ADR-004 is added, a threat model is updated, or a CALM spec changes, the only option is rebuilding the sandboxes. There is no hot-reload, no versioning of knowledge files, and no way to deprecate superseded knowledge. NCMS already has reconciliation capabilities (supersedes/conflicts relations in Phase 2) but they are not wired into the agent knowledge loading flow.

### Design

A knowledge management layer that treats seeded knowledge as a living, versioned corpus:

- **Hot-reload:** file watcher on the knowledge directories. When a file changes, the agent re-indexes it into NCMS without restart. The SSE listener detects a `knowledge.updated` bus event and triggers re-indexing.
- **Versioned knowledge:** each knowledge file stored with a version hash. When the file changes, the new version is stored alongside the old one. NCMS reconciliation marks the old version as superseded. Queries return the current version by default but can request historical versions.
- **Knowledge deprecation:** mark specific memories as deprecated (e.g., "ADR-002 is superseded by ADR-005"). Deprecated memories still exist for historical queries but are excluded from expert synthesis and review grounding.
- **Knowledge provenance:** each memory tracks its source file, load timestamp, version hash, and which agent loaded it. The knowledge grounding inspector (feature 9) shows this full provenance chain.
- **Cross-agent knowledge synchronization:** when the architect loads a new ADR, the security agent's next review should be aware of it. Bus announcements for `knowledge.updated` events notify all agents that the knowledge base has changed.

### Implementation

- File watcher in `register.py`: use `watchdog` or polling to detect changes in `knowledge_paths`
- On change: re-run the knowledge loading logic for the changed file, with NCMS reconciliation to handle supersession
- New bus event type `knowledge.updated` with metadata: `agent_id`, `file_path`, `version_hash`, `action` (added, updated, deprecated)
- Dashboard: knowledge management panel showing loaded files per agent, versions, and deprecation status
- NCMS reconciliation (Phase 2) activated for knowledge memories: new versions automatically supersede old ones with `valid_to` closure


## 18. Feedback Loop: Code Back to Design

### Problem

The pipeline flows one direction: Research → PRD → Design → Code. When the coding agent discovers the design is unimplementable (circular dependency, schema mismatch, missing API endpoint), there is no path for feedback to flow back to the Builder. The coding agent either fails silently or produces broken code that matches a broken spec.

### Design

A bidirectional feedback channel from the coding agent back to the Builder:

- **Implementation feedback report:** when the coding agent encounters an issue (test failure, type error, missing dependency, architectural conflict), it produces a structured feedback document with: the specific design section that caused the problem, the error or conflict description, and a suggested design change
- **Builder revision trigger:** the feedback document triggers the Builder's review loop, but instead of expert review, the input is the coding agent's implementation feedback. The Builder revises the design to address the specific issues, then re-publishes.
- **Cascading update:** the revised design triggers the coding agent to re-generate the affected code sections (not the entire codebase). The project view shows this as a "design-code reconciliation" phase.
- **Convergence tracking:** the project tracks how many design-code round trips were needed. A design that requires zero code feedback is a high-quality spec. A design that requires three rounds has gaps. This metric feeds into the compliance dashboard and the design pattern library.

### Implementation

- New bus domain `feedback-builder` that the coding agent announces to with structured feedback
- Builder's SSE listener detects feedback announcements and triggers a targeted revision pass
- New LangGraph node in the coding agent: `report_issues` that produces the feedback document before retrying
- Project model tracks `design_code_iterations` count
- Dashboard shows the feedback loop as a visible phase in the pipeline progress bar


## 19. Document-Memory Integration (Entity-Enriched Recall)

### Problem

When an expert agent receives a design document for review, it needs to search NCMS memory for relevant governance knowledge. Currently, the expert sends a truncated excerpt of the design text (up to 400 characters) as a search query. This produces poor retrieval: BM25 matches on raw prose miss the specific entities that matter (technology names, threat IDs, ADR references, compliance standards). The search is keyword-blind to the document's actual subject matter.

Meanwhile, published documents sit in the hub's document store as opaque blobs. There is no structured metadata about what a document contains, what entities it references, or what domains it touches. The only way to find related documents is full-text search of their prose.

### Design

**Entity extraction at publish time.** When any agent publishes a document via the hub's `/api/v1/documents` endpoint, the hub runs GLiNER zero-shot NER on the document content. GLiNER is already a required NCMS dependency with automatic text chunking (1,200 char chunks, sentence boundary splitting, entity dedup). The extracted entities — technology names, standards references, threat IDs, ADR numbers, architectural patterns, compliance frameworks — are stored as structured metadata alongside the document.

**JSON sidecar persistence.** Document metadata (entities, domain tags, timestamps, project linkage) is persisted as a JSON sidecar file alongside the markdown document. This replaces the current in-memory dict that loses metadata on hub restart. The sidecar file lives at `{doc_path}.meta.json` and contains:

```json
{
  "document_id": "design-abc123",
  "project_id": "PRJ-12345678",
  "agent": "builder",
  "phase": "design",
  "published_at": "2026-03-31T10:00:00Z",
  "entities": {
    "technology": ["NestJS", "MongoDB", "Redis", "JWT"],
    "standard": ["OWASP A01:2021", "NIST SP 800-63B"],
    "threat": ["THR-001", "THR-003"],
    "adr": ["ADR-001", "ADR-004"],
    "pattern": ["CQRS", "circuit breaker", "rate limiting"]
  },
  "domains": ["authentication", "authorization", "data access"],
  "summary": "Implementation design for JWT-based auth service with Redis token revocation"
}
```

**Entity-enriched search for expert agents.** When an expert agent needs to search memory for review grounding, it queries using the document's extracted entities instead of raw text excerpts. The search query becomes a targeted list of entity keywords (e.g., `"NestJS JWT OWASP A01:2021 ADR-001 circuit breaker"`) rather than a 400-char prose excerpt. This produces dramatically better BM25 hits because the query terms directly match the entities indexed in NCMS memories.

**NCMS memory creation.** At publish time, the hub also creates an NCMS memory with the document summary and entity list (not the full document content). This makes documents discoverable through the standard NCMS retrieval pipeline (BM25 + SPLADE + graph spreading activation) and benefits from episode linking, reconciliation, and all other NCMS features.

**Document search API.** A new endpoint `GET /api/v1/documents/search?entities=JWT,NestJS&domain=authentication` enables entity-aware document search. The dashboard's document viewer gains a search box that queries by entities, not just titles.

### Implementation

Backend:

- Modify `store_document()` in `api.py` to run GLiNER extraction on the document content at publish time. Use the existing `GlinerExtractor` from `infrastructure/extraction/gliner_extractor.py` with auto-chunking.
- Persist metadata as JSON sidecar files. On hub startup, rebuild the in-memory metadata index from sidecar files.
- New endpoint `GET /api/v1/documents/search` with entity and domain query parameters.
- Modify the expert agent's `search_memory` node to use document entities for search queries instead of text excerpts.
- Create an NCMS memory per published document with `type: "fact"`, summary content, and entity metadata.

Frontend:

- Document viewer shows extracted entities as clickable tags below the document header.
- Entity tags link to the knowledge graph view filtered to that entity.
- Document search panel with entity-based filtering.


## 20. Archeologist Agent (Existing Repository Analysis)

### Problem

The current pipeline is green-field only. Every project starts with a research prompt, searches the web, and generates designs from scratch. But most real engineering work involves existing codebases — modernizing a legacy service, adding features to an established system, migrating between frameworks, or improving security posture of deployed code. There is no path to bring an existing repository into the pipeline.

### Design

**New entry point: "+ Start Archaeology".** The dashboard's project creation panel gains a second action alongside the existing "+ New Project" button. "+ Start Archaeology" opens a repository browser that connects to GitHub via PAT authentication. The user selects a repository (or pastes a URL), chooses a branch, and describes the goal: "Modernize authentication from session cookies to JWT", "Add rate limiting to all public endpoints", "Migrate from Express to NestJS", or "Security audit against OWASP Top 10."

**GitHub MCP provider.** The Archeologist agent uses GitHub MCP (analogous to the Tavily provider for web search) to explore repositories. The PAT is accessed through the sandbox provider configuration — the same pattern used for Tavily API keys. The environment variable `GITHUB_PERSONAL_ACCESS_TOKEN` is already set and `api.github.com` is already allowed by the NemoClaw network policy, so no policy changes are needed. The provider gives structured access to:

- Repository file tree and directory structure
- File contents with language detection
- Dependency manifests (package.json, requirements.txt, go.mod, Cargo.toml)
- CI/CD configuration (.github/workflows, Dockerfile, docker-compose)
- Recent commit history and contributors
- Open issues and pull requests (for context on known problems)

**Archeologist LangGraph pipeline.** A new agent with a seven-node deterministic pipeline:

1. **check_guardrails**: Validate the repository URL and goal against domain/technology policies (reuses existing guardrails infrastructure).
2. **clone_and_index**: Fetch the repository structure via GitHub MCP. Build a file tree, identify entry points, extract dependency manifests. Index key files into the agent's working context.
3. **analyze_architecture**: Map the existing codebase structure — frameworks, patterns, data models, API endpoints, authentication mechanisms, test coverage. Produce a structured "as-is" architecture assessment.
4. **identify_gaps**: Compare the current architecture against the stated goal. Cross-reference with NCMS expert knowledge (ADRs, threat models, best practices). Produce a gap analysis: what exists, what's missing, what needs to change.
5. **web_research**: Targeted web search (via Tavily) grounded in codebase understanding. Instead of searching "JWT authentication patterns" generically, search "migrate Express session middleware to NestJS JWT guard" — queries informed by what's actually in the code.
6. **synthesize_report**: Produce a research report combining: as-is assessment, gap analysis, web research findings, and recommended modernization path. This is the equivalent of the Researcher's output but grounded in an existing codebase.
7. **publish_and_trigger**: Publish the archaeology report as a project document, then trigger the Product Owner to produce a PRD — feeding into the existing PO → Builder pipeline.

**Convergence with existing pipeline.** The Archeologist's output (a research report grounded in an existing codebase) feeds into the same downstream pipeline as the Researcher's output. The PO reads it, produces a PRD with a requirements manifest, and the Builder produces a design. The difference is the quality of grounding: the Researcher starts from web search; the Archeologist starts from actual code.

**Dashboard integration.** Archaeology projects appear in the same project list as green-field projects but with a repository badge showing the GitHub org/repo. The pipeline progress bar shows the Archeologist's nodes instead of the Researcher's. The phase timeline shows "Archaeology" instead of "Research" as the first phase.

**Repository metadata.** The Archeologist extracts and publishes structured metadata:

```json
{
  "repository": "org/repo-name",
  "branch": "main",
  "languages": {"TypeScript": 68, "Python": 22, "YAML": 10},
  "frameworks": ["Express", "Mongoose", "Jest"],
  "endpoints": 24,
  "test_coverage": "~60%",
  "dependencies": 47,
  "last_commit": "2026-03-28",
  "open_issues": 12,
  "goal": "Migrate authentication from session cookies to JWT"
}
```

This metadata is stored as a document sidecar (feature 19) and indexed into NCMS memory for expert agent grounding.

### Implementation

Backend:

- New agent module: `packages/nvidia-nat-ncms/src/nat/plugins/ncms/archeologist_agent.py` with the seven-node LangGraph pipeline.
- GitHub provider: `packages/nvidia-nat-ncms/src/nat/plugins/ncms/github_provider.py` wrapping GitHub REST API. PAT injected via sandbox provider config (same pattern as Tavily). Methods: `get_tree()`, `get_file()`, `get_dependencies()`, `get_workflows()`, `get_commits()`, `get_issues()`. No network policy change needed — `api.github.com` is already allowed.
- New sandbox config: `archeologist.yml` with GitHub MCP and Tavily providers, both available.
- Hub API: `POST /api/v1/projects` extended with `source_type: "archaeology"` and `repository_url` fields.
- Pipeline trigger: archaeology projects trigger `trigger-archeologist` domain instead of `trigger-researcher`.
- Agent port assignment: `"archeologist": 8006` in the hub's agent port map.

Frontend:

- New "+ Start Archaeology" button in the left nav (below "+ New Project").
- Repository browser panel: GitHub PAT configuration, org/repo search, branch selector, goal text input.
- Project cards for archaeology projects show repository badge (org/repo) and language breakdown.
- Pipeline progress bar uses Archeologist node sequence for archaeology projects.

Configuration:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_PERSONAL_ACCESS_TOKEN` | *(env)* | GitHub PAT, accessed via sandbox provider (same pattern as `TAVILY_API_KEY`) |
| `NCMS_ARCHAEOLOGY_MAX_FILES` | `200` | Max files to index from a repository |
| `NCMS_ARCHAEOLOGY_MAX_FILE_SIZE` | `50000` | Max file size in chars to include in analysis |


## Phased Delivery

### Phase 1 (Foundation) — IMPLEMENTED

| # | Feature | Status | Implementation Notes |
|---|---------|--------|---------------------|
| 1 | Project/Epic View | Done | Hub endpoints: `POST/GET /api/v1/projects`, `GET /api/v1/projects/{id}`, `POST .../archive`. Frontend: `projects.js` with project cards, phase timelines, "New Project" trigger panel. PRJ-XXXXXXXX IDs propagated through all agents via `pipeline_utils.py`. Documents linked by project_id extracted from content or metadata. |
| 2 | Live Pipeline Progress | Done | Hub endpoints: `POST /api/v1/pipeline/events`, `GET .../events/{project_id}`, `POST .../interrupt/{agent_id}`. Frontend: `pipeline-progress.js` with per-node status indicators for all three pipeline phases. Telemetry emitted at every node entry/exit by all five agents. Ring buffer (500 events/project). SSE broadcast via `pipeline.node` events. Interrupt signal support. |
| 5 | Spec Quality (Completeness + Contracts) | Done | `spec_validator.py`: 10-check completeness validator (section presence, code blocks, endpoint coverage, security requirements, technology alignment, PRD cross-reference, env vars, status codes). Validates against PO's requirements manifest. `design_agent.py`: `validate_completeness` node between synthesize and publish, `generate_contracts` node after review approval (OpenAPI 3.1 + Zod schemas). `prd_agent.py`: `generate_manifest` node produces structured JSON manifest alongside prose PRD. Auto-fix loop for minor structural gaps. |
| 6 | NemoGuardrails | Done | `guardrails.py`: `PolicyViolation` model with warn/block/reject escalation. `check_domain_scope`, `check_technology_scope`, `check_output_compliance` (secret detection, mandatory sections). `run_input_guardrails` and `run_output_guardrails` wrappers. All three pipeline agents have `check_guardrails` as first node. Builder has `check_output_guardrails` before publish. Violations announced to bus. Hub endpoints: `POST/GET /api/v1/policies`, `GET .../policies/{type}`. Frontend: `policy-editor.js` with YAML editor. |
| 15 | Prompt Library | Done | Hub endpoints: `POST/GET /api/v1/prompts`, `GET .../prompts/{agent}/{type}/latest`. Versioned storage with auto-incrementing version numbers. Frontend: `prompt-editor.js` with per-agent prompt editing. Prompts extracted into `research_prompts.py`, `prd_prompts.py`, `design_prompts.py`, `expert_prompts.py`. |

Phase 1 is complete. The project model, pipeline telemetry, spec validation, guardrails, and prompt management are all operational. All subsequent features build on this foundation.

### Phase 2 (Document Intelligence + Archeologist)

| # | Feature | Rationale |
|---|---------|-----------|
| 19 | Document-Memory Integration | GLiNER entity extraction at publish time. Entity-enriched search replaces raw text excerpts for expert grounding. JSON sidecar persistence for document metadata. Makes every published document a first-class knowledge object in NCMS. |
| 20 | Archeologist Agent | New entry point for existing repositories. GitHub MCP provider for codebase exploration. Seven-node LangGraph pipeline: guardrails → clone → analyze → gaps → research → synthesize → trigger. Output feeds into existing PO → Builder chain. |
| 4 | Document Diff View | Side-by-side diff for design revisions with review comment annotations. Essential for reviewing Archeologist-produced modernization plans against the existing codebase state. |
| 13 | Template Library | Reusable design fragments. The Archeologist's gap analysis can identify patterns from the existing codebase that should become templates for the modernization design. |

Phase 2 makes documents intelligent (entity-enriched, searchable, persistent metadata) and opens the second entry path: existing repository analysis. The Archeologist grounds the pipeline in real code instead of web search, while document-memory integration ensures expert agents retrieve precisely the governance knowledge each document needs. Template extraction from successful runs begins building organizational memory.

### Phase 3 (Coding Agent + Governance)

| # | Feature | Rationale |
|---|---------|-----------|
| 3 | Coding Agent (Claude Code) | Completes the pipeline from research/archaeology through implementation. Runs in NemoClaw sandbox with Claude CLI. |
| 6 | Contextual Approval Queue | Full-context human gate before code generation. Design + review scores + cost estimate in one approval view. |
| 7 | Compliance Dashboard | Aggregate governance visibility across all projects (green-field and archaeology). ADR matrix, STRIDE heat map, quality trends, drift scores. |
| 16 | Audit Trail | Reproducibility and compliance for all pipeline runs. Frozen snapshots of every input, retrieval, LLM response, and review score. |
| 18 | Feedback Loop (Code → Design) | Bidirectional improvement. When the coding agent discovers the design is unimplementable, structured feedback flows back to the Builder for targeted revision. |

Phase 3 closes the loop from design to code. The coding agent consumes approved designs (from either the Researcher or Archeologist path) and produces working implementations. Human approval gates every code generation. The feedback loop ensures design quality improves from implementation experience. Governance visibility and audit trails make the full pipeline enterprise-ready.

### Phase 4 (Learning System)

| # | Feature | Rationale |
|---|---------|-----------|
| 9 | Knowledge Grounding Inspector | Provenance verification — trace every citation to its backing NCMS memory. |
| 14 | Design Pattern Library | Organizational knowledge mined from historical runs. Patterns surface automatically via NCMS consolidation (Phase 5 clustering). |
| 17 | Knowledge Lifecycle Management | Living, versioned knowledge corpus with hot-reload. File watchers, reconciliation-based supersession, cross-agent sync. |
| 12 | Looking Glass Governance Mesh | Enterprise-grade 4-pillar governance via MCP. BAR artifact integration replaces static knowledge files. |

Phase 4 makes the system self-improving. Patterns emerge from successful runs across both green-field and archaeology projects. Knowledge evolves without rebuilds. The Looking Glass mesh connects to enterprise governance artifacts. The system gets smarter with each project.

### Future

| # | Feature | Rationale |
|---|---------|-----------|
| 10 | Telegram Integration | Mobile accessibility for approvals and pipeline triggers. |
| 11 | Resilience Improvements | Port forward auto-recovery, agent health monitoring, graceful review degradation. |
