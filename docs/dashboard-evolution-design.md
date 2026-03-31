# Dashboard Evolution: From Monitoring to Project Delivery

## Context

The NCMS dashboard today is an observability tool. It shows agent activity via SSE event streams, renders a D3 knowledge graph, provides per-agent chat overlays, and lists documents in a flat sidebar. It includes episode timelines, entity state history views, an admission scoring panel, and a floating approval queue. The backend is a Starlette application (`interfaces/http/dashboard.py`) serving REST endpoints and SSE streams, with a single-page frontend in `interfaces/http/static/index.html`.

This is effective for watching agents work. It is not effective for managing a portfolio of AI-driven design projects, tracking governance compliance across those projects, or giving a human approver enough context to make informed decisions about code generation.

This document describes ten capabilities that transform the dashboard from a monitoring tool into a project delivery and governance compliance platform. Each section states the problem, describes the design, and outlines the implementation path. The priority table at the end orders them by impact and effort.


## 1. Project/Epic View

### Problem

Documents appear as flat entries in the sidebar. There is no grouping by project, no phase tracking, and no visible relationship between a research report and the design it eventually produced. A user returning to the dashboard after a day away sees a list of documents with no narrative structure.

### Design

Each pipeline run creates a **Project** with a unique ID, topic, and creation timestamp. A project groups all documents by phase, establishing a clear progression: Research, PRD, Design (v1, v2, ...), Review Report, and Implementation. The project view replaces the flat document sidebar as the default.

**Project card.** Each project renders as a card showing the topic, phase progress as a row of checkmarks, total elapsed time, the latest quality score from review, and a count of knowledge-grounded references. Clicking the card expands it to reveal a phase timeline where each document appears with its size, creation timestamp, and inbound reference count.

**New project trigger.** A "New Project" button opens a structured trigger panel. The panel collects a topic string, a target description, and scope checkboxes selecting which phases to run (research only, research through design, full pipeline including implementation). Submitting the form creates the project record and triggers the first agent.

**Persistence.** Projects persist as NCMS memories with `type: "project"` and metadata linking to constituent document IDs. This means projects are searchable through the standard memory retrieval pipeline and benefit from entity extraction, episode linking, and all other NCMS features.

**Project list.** The list supports filtering by status (active, completed, failed), sorting by recency or quality score, and text search across topics. Active projects sort to the top with a visual indicator.

### Implementation

Backend:

- New REST endpoints on the dashboard Starlette app:
  - `POST /api/v1/projects` creates a project memory with topic, scope, and phase configuration.
  - `GET /api/v1/projects` returns all project memories with phase completion status derived from linked documents.
  - `GET /api/v1/projects/{id}` returns the full project detail including all linked documents grouped by phase.
- Each agent's publish step tags documents with a `project_id` field in the memory's `structured` metadata. The dashboard resolves these links at query time.
- Project creation calls `memory_service.store()` with the project memory, then dispatches a bus announcement to trigger the first pipeline agent.

Frontend:

- New `projects.js` module replaces the document sidebar rendering logic. The module manages the project list, card rendering, phase timeline, and trigger panel.
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

A horizontal pipeline progress bar renders at the top of the project view whenever a project has an active run. The bar shows the full phase sequence: Research, PRD, Design, Review, Implement. The active phase displays an animated progress indicator. Completed phases show a checkmark and the final document size. Future phases appear dimmed.

**Phase detail.** Hovering over any phase shows a tooltip with the agent name handling it, elapsed time, and document size (growing in real time during active generation).

**Review round tracking.** The Review phase is special: it shows round-by-round progress. For example, "R1: 78%/72% [fail] -> R2: 85%/92% [pass]" where the two percentages represent the architecture and security review scores.

**Time estimation.** Estimated time remaining for the active phase is computed from a rolling average of previous runs stored in the browser's localStorage. After three or more completed runs, the estimate becomes reasonably accurate.

**Bus event integration.** The progress bar updates in real time by listening to SSE events. Handoff triggers (one agent completing and triggering the next) advance the active phase. Review result announcements update the review round display. Document publish events update sizes and mark phases complete.

### Implementation

This is a frontend-only feature. No backend changes are required.

- New `pipeline-progress.js` module subscribes to the existing SSE stream and filters for `bus.announce` events whose content matches known trigger, review, and publish patterns.
- Phase transitions are inferred from event content: a publish event from the Researcher with a matching project ID marks Research complete and PRD as active.
- Time estimates are stored in localStorage keyed by phase name. Each completed phase updates the rolling average.
- The progress bar DOM element is injected at the top of the expanded project card when a run is active and removed when all phases complete or fail.


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

A deterministic check that parses the markdown design and verifies structural presence:

- **Section presence:** Every required section (Project Structure, API Endpoints, Data Models, Authentication, Security Controls, Configuration, Error Handling, Testing, Deployment) must exist as a heading.
- **Code examples:** Every section must contain at least one code fence. A section that describes "rate limiting" in prose but includes no code example is flagged.
- **Endpoint coverage:** Regex extracts all HTTP method + path combinations (e.g., `POST /auth/login`). The design must define at least as many endpoints as the PRD requires.
- **Interface definitions:** Count TypeScript `interface` declarations. A design with fewer than 3 interfaces lacks the type contracts a coding agent needs.
- **PRD cross-reference:** Extract key terms from each PRD requirement and verify they appear somewhere in the design. A PRD requirement like "token revocation with Redis-backed store" should match at least one of ["revocation", "Redis", "revoke"] in the design text.
- **Environment variables:** The configuration section must document env vars.
- **Error response format:** The error handling section must define status codes.

If the check finds issues, it returns them to the `synthesize_design` node for a targeted fix pass (LLM sees only the specific gaps, not the full design). This avoids full re-synthesis for minor structural gaps.

**Interface contract generation (LLM, after approval):**

After the design passes review, a final LLM pass produces machine-parseable contracts:

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

### Implementation

- NemoGuardrails configuration files (`guardrails/`) define the policy rules in Colang format.
- A guardrails wrapper around each LangGraph pipeline's entry point validates inputs before the graph runs.
- A guardrails check node after each `publish` step validates outputs before the document is made visible.
- Policy violations are announced to the bus and surfaced in the dashboard as warnings with specific remediation guidance.
- The compliance dashboard tracks guardrail violations per project and across the portfolio.

### Integration with Existing Pipeline

The guardrails layer sits between the hub's `/generate` proxy and the agent's LangGraph pipeline. It does not modify the graph itself. This means guardrails can be enabled or disabled per agent via configuration, and the pipeline continues to work identically when guardrails are off. The goal is policy as an overlay, not policy embedded in agent logic.


## Priority Order

| Priority | Feature | Impact | Effort | Rationale |
|----------|---------|--------|--------|-----------|
| 1 | Project/Epic View | High | Medium | Transforms the UX from a flat document list into a structured project management interface. Foundation for every other feature. |
| 2 | Live Pipeline Progress | High | Low | Immediate usability improvement with no backend changes. Depends on Project View for context. |
| 3 | Coding Agent (Claude Code) | High | High | Completes the pipeline from research through implementation. The most visible capability extension. |
| 4 | Contextual Approval Queue | High | Medium | Required before the coding agent can operate safely. Human-in-the-loop gate with full context. |
| 5 | Spec Quality (Completeness + Contracts) | High | Medium | Structural validation and machine-parseable output. The spec is the single most important artifact for the coding agent. |
| 6 | NemoGuardrails | High | Medium | Policy enforcement before the coding agent generates real code. Prevents scope drift and compliance violations. |
| 7 | Compliance Dashboard | High | Medium | The CIO story. Aggregate governance visibility across projects, essential for enterprise adoption. |
| 8 | Document Diff View | Medium | Low | Audit trail for design iterations. Low effort since it is entirely frontend with existing APIs. |
| 9 | Knowledge Grounding Inspector | Medium | Medium | Provenance and trust. Lets users verify that agent citations are backed by real retrieved knowledge. |
| 10 | Telegram Integration | Medium | Low | Accessibility for mobile decision-makers. Uses existing APIs with a thin bot wrapper. |
| 11 | Resilience Improvements | Medium | Medium | Reliability across the board. Each sub-feature is independently valuable and deployable. |
| 12 | Looking Glass Governance Mesh | High | High | Highest enterprise value, but depends on external Looking Glass infrastructure and the full compliance dashboard. |

Priorities 1 and 2 should ship together as a single release since the pipeline progress bar is most valuable when rendered inside a project context. Priority 4 must ship before or alongside priority 3 since the coding agent requires the approval gate. Priority 5 (NemoGuardrails) should ship before or alongside the coding agent to ensure generated code respects organizational policies from day one. The remaining features are independently deployable.
