# NemoClaw Analysis Runbook

Quick reference for inspecting the NCMS hub, agent sandboxes, and database during and after a project run.

## Architecture

```
Host Machine
  |
  +-- Docker: ncms-hub (container)
  |     Port 9080 → API + Knowledge Bus + SSE
  |     Port 8420 → Dashboard UI
  |     DB: /app/data/ncms.db (persistent volume)
  |
  +-- OpenShell/NemoClaw: k8s sandboxes
        ncms-archeologist  (port 8001)
        ncms-architect     (port 8002)
        ncms-security      (port 8003)
        ncms-product-owner (port 8004)
        ncms-designer      (port 8005)
        Agents connect to hub via host.docker.internal:9080
```

## Hub (Docker)

### Logs

```bash
docker logs ncms-hub --tail 50              # Recent logs
docker logs ncms-hub --tail 200 2>&1 | grep -i "error\|exception\|500"  # Errors only
docker logs ncms-hub -f                     # Stream live
```

### Copy Database for Analysis

```bash
# Copy from container to host
docker cp ncms-hub:/app/data/ncms.db /tmp/hub-analysis.db

# Quick health check
sqlite3 /tmp/hub-analysis.db "
  SELECT 'memories', COUNT(*) FROM memories
  UNION ALL SELECT 'nodes', COUNT(*) FROM memory_nodes
  UNION ALL SELECT 'entities', COUNT(*) FROM entities
  UNION ALL SELECT 'edges', COUNT(*) FROM graph_edges
  UNION ALL SELECT 'events', COUNT(*) FROM dashboard_events;
"
```

### Common Database Queries

```bash
DB=/tmp/hub-analysis.db

# Memory breakdown by type and agent
sqlite3 $DB "SELECT source_agent, type, COUNT(*) FROM memories GROUP BY source_agent, type ORDER BY source_agent, 3 DESC;"

# Fact memories (the real knowledge, not sections)
sqlite3 $DB "SELECT source_agent, substr(content, 1, 100), type FROM memories WHERE type='fact' ORDER BY created_at DESC;"

# Document profiles (rich TOC memories — should be 1 per navigable document)
sqlite3 $DB "SELECT source_agent, substr(content, 1, 120), type FROM memories WHERE type='document_profile';"

# Legacy section indexes (from old ingestion — should be 0 after migration)
sqlite3 $DB "SELECT source_agent, substr(content, 1, 100) FROM memories WHERE type='section_index';"

# Document sections in document store (NOT memory store)
sqlite3 $DB "SELECT d.id, substr(d.title, 1, 60), d.parent_doc_id FROM documents d WHERE d.doc_type='section' ORDER BY d.created_at;"

# Entity states (should be real state changes, not document sections)
sqlite3 $DB "
  SELECT json_extract(metadata, '$.entity_id'),
         json_extract(metadata, '$.state_key'),
         json_extract(metadata, '$.state_value'),
         memory_id
  FROM memory_nodes WHERE node_type='entity_state';
"

# Episodes
sqlite3 $DB "
  SELECT json_extract(metadata, '$.episode_title'),
         json_extract(metadata, '$.status'),
         json_extract(metadata, '$.member_count')
  FROM memory_nodes WHERE node_type='episode' ORDER BY created_at DESC;
"

# Recent dashboard events by type
sqlite3 $DB "SELECT type, COUNT(*) FROM dashboard_events GROUP BY type ORDER BY 2 DESC LIMIT 20;"

# Events for a specific agent
sqlite3 $DB "SELECT type, substr(data, 1, 80), timestamp FROM dashboard_events WHERE agent_id='architect' ORDER BY seq DESC LIMIT 20;"

# Search/recall activity
sqlite3 $DB "SELECT agent_id, substr(data, 1, 100) FROM dashboard_events WHERE type='memory.searched' ORDER BY seq DESC LIMIT 10;"
```

## Agent Sandboxes (OpenShell)

### List and Connect

```bash
openshell sandbox list                        # List all sandboxes
openshell sandbox connect ncms-architect      # Shell into a sandbox
openshell sandbox get ncms-architect          # Get sandbox details
```

### Agent Logs

Agent logs are at `/sandbox/ncms-nat-agent.log` inside each sandbox. Access via SSH:

```bash
# Tail logs for a specific agent
ssh openshell-ncms-architect 'tail -30 /sandbox/ncms-nat-agent.log'
ssh openshell-ncms-security 'tail -30 /sandbox/ncms-nat-agent.log'
ssh openshell-ncms-product-owner 'tail -30 /sandbox/ncms-nat-agent.log'
ssh openshell-ncms-designer 'tail -30 /sandbox/ncms-nat-agent.log'
ssh openshell-ncms-archeologist 'tail -30 /sandbox/ncms-nat-agent.log'

# Stream logs live
ssh openshell-ncms-architect 'tail -f /sandbox/ncms-nat-agent.log'

# Search for errors
ssh openshell-ncms-architect 'grep -i "error\|exception\|traceback" /sandbox/ncms-nat-agent.log | tail -20'

# Check all agents for errors (quick scan)
for agent in archeologist architect security product-owner designer; do
  echo "=== ncms-$agent ==="
  ssh openshell-ncms-$agent 'grep -ci "error\|exception" /sandbox/ncms-nat-agent.log 2>/dev/null' || echo "unreachable"
done
```

### Check Agent Process

```bash
ssh openshell-ncms-architect 'ps aux | grep nat'
ssh openshell-ncms-architect 'cat /sandbox/configs/*.yaml'  # View agent config
```

## Dashboard

- **URL**: `http://localhost:8420` (or the Cloudflare tunnel URL)
- **Hub API**: `http://localhost:9080`
- **Force refresh**: `Cmd+Shift+R` (clears cached JS/CSS)

### API Endpoints (from host)

```bash
# Health
curl -s http://localhost:9080/api/v1/health | python3 -m json.tool

# Registered agents
curl -s http://localhost:9080/api/v1/bus/agents | python3 -m json.tool

# Stats
curl -s http://localhost:8420/api/stats | python3 -m json.tool

# Recent events (global)
curl -s "http://localhost:8420/api/events?limit=20" | python3 -m json.tool

# Recent events (per agent)
curl -s "http://localhost:8420/api/events?agent_id=architect&limit=10" | python3 -m json.tool

# Maintenance scheduler status
curl -s http://localhost:9080/api/v1/maintenance/status | python3 -m json.tool

# Trigger maintenance manually
curl -s -X POST http://localhost:9080/api/v1/maintenance/run \
  -H "Content-Type: application/json" \
  -d '{"task": "all"}' | python3 -m json.tool
```

## Full Analysis Checklist

Run after a project completes (or periodically during a run):

### 1. Copy and inspect database

**Important:** Always copy the WAL file too — SQLite WAL mode means uncommitted pages live in the WAL, not the main DB file. Without it you get stale data.

```bash
docker cp ncms-hub:/app/data/ncms.db /tmp/hub-analysis.db
docker cp ncms-hub:/app/data/ncms.db-wal /tmp/hub-analysis.db-wal 2>/dev/null
docker cp ncms-hub:/app/data/ncms.db-shm /tmp/hub-analysis.db-shm 2>/dev/null
DB=/tmp/hub-analysis.db

echo "--- Totals ---"
sqlite3 $DB "
  SELECT 'memories', COUNT(*) FROM memories
  UNION ALL SELECT 'facts', COUNT(*) FROM memories WHERE type='fact'
  UNION ALL SELECT 'doc_profiles', COUNT(*) FROM memories WHERE type='document_profile'
  UNION ALL SELECT 'legacy_sections', COUNT(*) FROM memories WHERE type IN ('document_section','section_index')
  UNION ALL SELECT 'doc_store_sections', (SELECT COUNT(*) FROM documents WHERE doc_type='section')
  UNION ALL SELECT 'nodes', COUNT(*) FROM memory_nodes
  UNION ALL SELECT 'entity_states', COUNT(*) FROM memory_nodes WHERE node_type='entity_state'
  UNION ALL SELECT 'episodes', COUNT(*) FROM memory_nodes WHERE node_type='episode'
  UNION ALL SELECT 'entities', COUNT(*) FROM entities
  UNION ALL SELECT 'edges', COUNT(*) FROM graph_edges
  UNION ALL SELECT 'events', COUNT(*) FROM dashboard_events;
"

echo "--- By agent ---"
sqlite3 $DB "SELECT source_agent, type, COUNT(*) FROM memories GROUP BY source_agent, type ORDER BY source_agent;"

echo "--- Entity states (check for false positives) ---"
sqlite3 $DB "SELECT json_extract(metadata, '$.entity_id'), json_extract(metadata, '$.state_key'), json_extract(metadata, '$.state_value') FROM memory_nodes WHERE node_type='entity_state';"

echo "--- Episodes ---"
sqlite3 $DB "SELECT json_extract(metadata, '$.episode_title'), json_extract(metadata, '$.member_count'), json_extract(metadata, '$.status') FROM memory_nodes WHERE node_type='episode' ORDER BY created_at;"
```

### 2. Check hub for errors

```bash
docker logs ncms-hub 2>&1 | grep -iE "error|exception|traceback|failed" | grep -v "GET /api" | tail -20
```

### 3. Check each agent sandbox

```bash
for agent in archeologist architect security product-owner designer; do
  echo "=== ncms-$agent ==="
  ssh openshell-ncms-$agent 'tail -5 /sandbox/ncms-nat-agent.log' 2>/dev/null || echo "  unreachable"
  echo ""
done
```

### 4. Verify features are working

Check the database for evidence of each feature:

```bash
DB=/tmp/hub-analysis.db

echo "--- Admission (should see scored events) ---"
sqlite3 $DB "SELECT COUNT(*) FROM dashboard_events WHERE type='admission.scored';"

echo "--- Episodes (should be >0 if enabled) ---"
sqlite3 $DB "SELECT COUNT(*) FROM memory_nodes WHERE node_type='episode';"

echo "--- Entity states (real state changes, not section artifacts) ---"
sqlite3 $DB "SELECT COUNT(*) FROM memory_nodes WHERE node_type='entity_state';"

echo "--- Document profiles (1 per navigable doc in memory store, sections in doc store) ---"
sqlite3 $DB "SELECT COUNT(*) FROM memories WHERE type='document_profile';"
sqlite3 $DB "SELECT COUNT(*) FROM documents WHERE doc_type='section';"
echo "--- Legacy sections (should be 0 after migration) ---"
sqlite3 $DB "SELECT COUNT(*) FROM memories WHERE type IN ('section_index', 'document_section');"

echo "--- Search activity ---"
sqlite3 $DB "SELECT COUNT(*) FROM dashboard_events WHERE type='memory.searched';"

echo "--- Bus asks (agent-to-agent questions) ---"
sqlite3 $DB "SELECT COUNT(*) FROM dashboard_events WHERE type='bus.ask';"
```

## Ports Reference

| Service | Port | Access |
|---------|------|--------|
| Hub API + Bus | 9080 | `http://localhost:9080` |
| Dashboard | 8420 | `http://localhost:8420` |
| Phoenix (traces) | 6006 | `http://localhost:6006` |
| Archeologist agent | 8001 | Inside sandbox only |
| Architect agent | 8002 | Inside sandbox only |
| Security agent | 8003 | Inside sandbox only |
| Product Owner agent | 8004 | Inside sandbox only |
| Designer agent | 8005 | Inside sandbox only |

## Post-Rebuild Verification

Run after every hub rebuild to catch wiring issues before starting a project:

```bash
# Wait for all agents to register and load knowledge (~30s)
sleep 30

# Copy DB with WAL
docker cp ncms-hub:/app/data/ncms.db /tmp/hub-verify.db
docker cp ncms-hub:/app/data/ncms.db-wal /tmp/hub-verify.db-wal 2>/dev/null
docker cp ncms-hub:/app/data/ncms.db-shm /tmp/hub-verify.db-shm 2>/dev/null
DB=/tmp/hub-verify.db

echo "=== Wiring Check ==="
sqlite3 $DB "
  SELECT 'memories', COUNT(*) FROM memories
  UNION ALL SELECT 'doc_profiles', COUNT(*) FROM memories WHERE type='document_profile'
  UNION ALL SELECT 'facts', COUNT(*) FROM memories WHERE type='fact'
  UNION ALL SELECT 'legacy_sections', COUNT(*) FROM memories WHERE type IN ('section_index','document_section')
  UNION ALL SELECT 'doc_store_parents', (SELECT COUNT(*) FROM documents WHERE parent_doc_id IS NULL)
  UNION ALL SELECT 'doc_store_sections', (SELECT COUNT(*) FROM documents WHERE parent_doc_id IS NOT NULL)
  UNION ALL SELECT 'ephemeral_docs', (SELECT COUNT(*) FROM ephemeral_cache WHERE content LIKE '%Document:%' OR content LIKE '%# %')
  UNION ALL SELECT 'entity_states', COUNT(*) FROM memory_nodes WHERE node_type='entity_state'
  UNION ALL SELECT 'episodes', COUNT(*) FROM memory_nodes WHERE node_type='episode';
"

echo "=== Per-Agent Breakdown ==="
sqlite3 $DB "SELECT source_agent, type, COUNT(*) FROM memories GROUP BY source_agent, type ORDER BY source_agent;"

echo "=== Hub Errors ==="
docker logs ncms-hub 2>&1 | grep -iE "error|exception|traceback|failed" | grep -v "GET /api" | tail -10
```

**Expected results (NemoClaw blueprint with 13 knowledge files):**

| Check | Expected | Problem if wrong |
|-------|----------|-----------------|
| `doc_profiles` | 13 (one per knowledge file) | Admission rejecting documents — check importance >= 8.0 |
| `legacy_sections` | 0 | Old code path active — check `content_classification_enabled` |
| `ephemeral_docs` | 0 | Documents routed to ephemeral cache — check importance threshold |
| `doc_store_parents` | = `doc_profiles` | Profile memory not created — check `_store_bypassing_classification` path |
| architect memories | 7 (6 profiles + 1 fact) | ADR-001 is small enough to also create a fact |
| security memories | 3 (2 profiles + 1 fact) | 4 YAML files, small ones go ATOMIC |
| product_owner memories | 2 (2 profiles) | 2 markdown files |

**After running consolidation:**

```bash
# Trigger consolidation
curl -s -X POST http://localhost:9080/api/v1/maintenance/run \
  -H "Content-Type: application/json" -d '{"task": "all"}' | python3 -m json.tool

# Wait, then re-copy DB and check events
sleep 10
docker cp ncms-hub:/app/data/ncms.db /tmp/hub-verify.db
docker cp ncms-hub:/app/data/ncms.db-wal /tmp/hub-verify.db-wal 2>/dev/null
sqlite3 /tmp/hub-verify.db "
  SELECT type, count(*) FROM dashboard_events
  WHERE type IN ('consolidation.pass_complete', 'dream.cycle_complete', 'consolidation.abstract_created')
  GROUP BY type;
"
```

| Check | Expected | Problem if wrong |
|-------|----------|-----------------|
| `consolidation.pass_complete` | >= 1 | event_log not wired to ConsolidationService |
| `dream.cycle_complete` | >= 1 | event_log not wired to ConsolidationService |

## Troubleshooting

**Agents going offline**: Check heartbeats in hub logs. Agents send heartbeats every 30s; hub marks offline after 90s.
```bash
docker logs ncms-hub 2>&1 | grep heartbeat | tail -10
```

**Empty agent activity on dashboard refresh**: Hard refresh with `Cmd+Shift+R`. The per-agent bootstrap fetches from persistent DB — if events aren't there, check that `event_log._db` is wired (look for migration log on hub startup).

**500 errors on search/recall**: Check `NCMS_PIPELINE_DEBUG` — debug mode adds extra fields. Check hub logs for the actual traceback.

**Entity state false positives**: Document sections (`document_section`, `section_index`, `document_chunk`, `document` types) are excluded from state detection. If false positives appear, check the memory type of the source.

**Learning card counters stuck at zero**: Consolidation/dream events not reaching dashboard. Check that `event_log` is passed to `ConsolidationService` in `create_ncms_services()` (server.py). The scheduler's `maintenance.task_complete` events show in the activity feed, but the counters need `consolidation.pass_complete` / `dream.cycle_complete` events emitted by the service itself.

**Documents disappearing (fewer memories than expected)**: Admission scoring rejected low-utility content. Check `ephemeral_cache` table and pipeline events for `route: "ephemeral_cache"`. Documents published via `publish_document` should have `importance >= 8.0` to bypass admission. Trace: `sqlite3 $DB "SELECT type, substr(data,1,200) FROM dashboard_events WHERE type='pipeline.store.admission' AND data LIKE '%ephemeral%';"`

**WAL-related stale data**: `docker cp` only copies the main `.db` file. Always copy `.db-wal` and `.db-shm` too. Without the WAL, recently written data (last few seconds/minutes) will be missing.
