# Architecture Review — Domain Prompt Pack

This pack provides **deep architecture analysis** beyond the Default pack's baseline. Use it when you need thorough validation of the CALM model, ADR compliance, fitness functions, and quality attributes.

---

## CALM Model Drift Analysis

Perform a comprehensive comparison between the CALM 1.2 architecture model (`architecture/bar.arch.json`) and the actual code:

### Node-to-Code Mapping
For every `service`, `system`, `database`, and `network` node in the CALM model:
1. Identify the corresponding codebase, module, or infrastructure component in `repos/`
2. Verify the node's `description` matches the actual purpose of the code
3. Check that the node's `data-classification` level aligns with the data actually handled
4. Report **phantom nodes** — documented in CALM but no corresponding code exists
5. Report **undocumented components** — code exists with no matching CALM node

### Relationship Verification
For every `connects` relationship:
1. Verify the source and destination nodes have actual integration code (API calls, message publishing, database queries)
2. Check that the documented protocol matches reality:
   - REST endpoints → actual HTTP clients/servers
   - gRPC → actual protobuf definitions and gRPC stubs
   - AMQP/Kafka → actual message producers/consumers
   - Database connections → actual connection strings and query patterns
3. Check for **undocumented integrations** — code-level connections not in the CALM model
4. Verify the authentication mechanism on each connection matches what's documented

### Containment Hierarchy
For every `composed-of` relationship:
1. Verify the documented container boundaries match the actual deployment units
2. Check that services grouped in a container are actually co-deployed or share a bounded context
3. Identify services that should be in different containers based on their actual coupling patterns

### Flow Validation
For every documented flow:
1. Trace the flow through the code — does the sequence of service calls match the documented relationship order?
2. Identify flow steps that exist in code but are not documented
3. Check for error/fallback paths not captured in the flow definition

---

## Architecture Decision Record (ADR) Compliance

For each ADR in `architecture/ADRs/`:

1. **Read the decision** — understand what was decided and why
2. **Verify implementation** — check if the code follows the decision
3. **Check status** — if the ADR status is "Accepted" or "Approved", the code MUST follow it
4. **Report violations** — code that contradicts an accepted ADR is a HIGH severity finding
5. **Check for superseded patterns** — if an ADR was superseded, verify the old pattern is no longer used

Common ADR compliance checks:
- Technology choices (e.g., "Use PostgreSQL" — verify no other databases are used)
- Communication patterns (e.g., "Use event-driven" — verify no synchronous RPC for specified flows)
- Data storage decisions (e.g., "Store PII only in encrypted columns")
- Authentication approach (e.g., "Use OAuth 2.0 with JWT" — verify no session-based auth)

---

## Fitness Function Validation

If `architecture/fitness-functions.yaml` exists, validate each defined gate:

### Complexity
- If a threshold is defined (e.g., cyclomatic complexity <= 10), scan source files for complex functions
- Report functions that appear to exceed the threshold based on branching depth and conditional density
- Focus on business logic files, not generated code or configuration

### Test Coverage
- Check for test directories and test files in each repo
- Estimate coverage by comparing test file count to source file count
- Report repos with no test infrastructure at all as HIGH severity

### Dependency Freshness
- Check `package.json`, `pom.xml`, `requirements.txt`, `go.mod` for dependency declarations
- Identify dependencies that appear outdated based on version patterns
- Flag any dependencies with known end-of-life status

### Performance
- If latency budgets are defined, check for performance-impacting patterns:
  - N+1 query patterns
  - Missing pagination on list endpoints
  - Synchronous calls in hot paths that could be async
  - Large payload serialization without streaming

### Security Compliance
- Cross-reference with the security controls checklist
- Verify security-related fitness functions are achievable given the code patterns

---

## Quality Attribute Verification

If `architecture/quality-attributes.yaml` exists, verify implementation patterns:

### Availability
- Check for health check endpoints in each service
- Verify graceful shutdown handling
- Look for single points of failure (singleton databases without replication, hardcoded hosts)
- Check for proper connection pooling and timeout configuration

### Latency
- Identify hot paths (user-facing request handlers)
- Check for N+1 queries, excessive serialization, blocking I/O in async contexts
- Verify caching is implemented where the quality attributes suggest it should be

### Throughput
- Check for connection pooling and resource limits
- Verify batch processing for high-volume operations
- Look for rate limiting on inbound APIs

### Scalability
- Check for stateless service design (no in-memory session state)
- Verify configuration is externalized (environment variables, config files)
- Look for horizontal scaling blockers (file system dependencies, local caches without invalidation)

---

## Component Boundary Analysis

Assess the cleanliness of service and module boundaries:

1. **Coupling analysis** — identify cross-service dependencies that bypass documented APIs (direct database access, shared mutable state, circular imports)
2. **Cohesion check** — verify each service has a clear, single responsibility
3. **API surface** — check that services expose well-defined APIs (REST controllers, gRPC services, message handlers) rather than leaking internal details
4. **Shared libraries** — identify shared code and verify it's intentional and documented
5. **Data ownership** — verify each database is owned by a single service (no shared database anti-pattern unless documented)

---

## Technology Stack Drift

Compare documented technology choices against actual usage:

1. **Languages and frameworks** — verify the codebase uses what the architecture documents say
2. **Infrastructure dependencies** — check for undocumented databases, message brokers, or caches
3. **Build tooling** — verify CI/CD patterns match architectural expectations
4. **Library choices** — identify major libraries and verify they align with architectural decisions

---

## Output Format

Report findings using the standard Oraculum format from the Default pack. Tag all findings from this pack as **Architecture** pillar. Use severity criteria:

- **Critical**: Core architectural violation that could cause systemic failure
- **High**: Significant drift from documented architecture, ADR violation, or broken containment boundary
- **Medium**: Moderate drift, missing fitness function compliance, or quality attribute gap
- **Low**: Minor inconsistency, documentation gap, or technology drift with low impact
