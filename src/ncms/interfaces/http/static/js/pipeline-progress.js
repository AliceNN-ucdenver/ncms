// ── NCMS Dashboard — Pipeline Progress ───────────────────────────────
// Renders live pipeline progress bars inside expanded project cards.
// Shows per-phase node status with animated indicators.

// Pipeline progress state: project_id -> { phases: { agent: [nodes] } }
if (!state.pipelineProgress) state.pipelineProgress = {};

// ── Default Pipeline Definitions ─────────────────────────────────────
// Each phase defines its agent and the ordered processing nodes.

const PIPELINE_PHASES = {
  research:  { agent: 'researcher',     label: 'Research',  nodes: ['check_guardrails', 'plan_queries', 'parallel_search', 'synthesize', 'publish', 'verify'] },
  prd:       { agent: 'product_owner',  label: 'PRD',       nodes: ['check_guardrails', 'read_document', 'ask_experts', 'synthesize_prd', 'generate_manifest', 'publish_prd', 'verify_and_trigger'] },
  design:    { agent: 'builder',        label: 'Design',    nodes: ['check_guardrails', 'read_document', 'ask_experts', 'synthesize_design', 'validate_completeness', 'publish_design', 'request_review', 'revise_design', 'verify'] },
};

const NODE_LABELS = {
  check_guardrails: 'Guardrails',
  plan_queries: 'Plan',
  parallel_search: 'Search',
  synthesize: 'Synthesize',
  synthesize_prd: 'Synthesize',
  synthesize_design: 'Synthesize',
  publish: 'Publish',
  publish_prd: 'Publish',
  publish_design: 'Publish',
  verify: 'Verify',
  verify_and_trigger: 'Trigger',
  read_document: 'Read Doc',
  ask_experts: 'Ask Experts',
  validate_completeness: 'Validate',
  request_review: 'Review',
  revise_design: 'Revise',
  generate_manifest: 'Manifest',
  test: 'Test',
};

// ── Render ───────────────────────────────────────────────────────────

async function renderPipelineProgress(projectId, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  // Fetch stored events from hub if we don't have them yet
  if (!state.pipelineProgress[projectId]) {
    try {
      const resp = await fetch(HUB_API + '/api/v1/pipeline/events/' + encodeURIComponent(projectId));
      if (resp.ok) {
        const events = await resp.json();
        state.pipelineProgress[projectId] = {};
        for (const evt of events) {
          handlePipelineProgressEvent({ data: evt });
        }
      }
    } catch (e) {
      console.debug('Failed to fetch pipeline events:', e);
    }
  }

  // Find the project to determine which phases are in scope
  const project = (state.projects || []).find(p => p.project_id === projectId);
  const scope = project?.scope || ['research', 'prd', 'design'];
  const progressData = state.pipelineProgress[projectId] || {};

  let html = '<div class="pipeline-progress-bar">';

  for (const phaseKey of scope) {
    const phaseDef = PIPELINE_PHASES[phaseKey];
    if (!phaseDef) continue;

    const phaseData = progressData[phaseKey] || {};
    const isPhaseActive = project?.phases?.find(p => p.name === phaseKey)?.status === 'active'
      || project?.phases?.find(p => p.name === phaseKey)?.status === 'in_progress';
    const isPhaseCompleted = project?.phases?.find(p => p.name === phaseKey)?.status === 'completed';
    const isPhaseFailed = project?.phases?.find(p => p.name === phaseKey)?.status === 'failed';
    const isPhaseWaiting = !isPhaseActive && !isPhaseCompleted && !isPhaseFailed;

    const phaseClass = isPhaseCompleted ? 'completed'
      : isPhaseFailed ? 'failed'
      : isPhaseActive ? 'active'
      : 'waiting';

    html += `<div class="pipeline-phase ${phaseClass}">
      <div class="pipeline-phase-label">${escapeHtml(phaseDef.label)}</div>
      <div class="pipeline-phase-nodes">`;

    for (const nodeKey of phaseDef.nodes) {
      const nodeData = phaseData[nodeKey] || {};
      const nodeStatus = nodeData.status || 'waiting';
      const nodeLabel = NODE_LABELS[nodeKey] || nodeKey;
      const detail = nodeData.detail ? ` title="${escapeHtml(nodeData.detail)}"` : '';

      let statusIcon = '';
      if (nodeStatus === 'completed') statusIcon = ' &#x2713;';
      else if (nodeStatus === 'failed') statusIcon = ' &#x2717;';
      else if (nodeStatus === 'started' || nodeStatus === 'active') statusIcon = '';

      const nodeClass = `pipeline-node ${nodeStatus}${isPhaseWaiting ? ' dimmed' : ''}`;

      html += `<div class="${nodeClass}"${detail}
        onclick="onPipelineNodeClick('${escapeHtml(projectId)}', '${escapeHtml(phaseKey)}', '${escapeHtml(nodeKey)}')"
        >${escapeHtml(nodeLabel)}${statusIcon}</div>`;
    }

    html += '</div></div>';
  }

  html += '</div>';
  container.innerHTML = html;
}

// ── SSE Event Handling ───────────────────────────────────────────────

function handlePipelineProgressEvent(event) {
  // Normalize: event may be raw {project_id, agent, node, status, detail}
  // or an SSE event wrapper {data: {project_id, ...}} or {detail: {data: {...}}}
  const evt = event?.data || event?.detail?.data || event || {};
  if (!evt.project_id) return;

  const pid = evt.project_id;
  if (!state.pipelineProgress[pid]) state.pipelineProgress[pid] = {};

  // Determine which phase this agent belongs to
  let phaseKey = null;
  for (const [key, def] of Object.entries(PIPELINE_PHASES)) {
    if (def.agent === evt.agent) {
      phaseKey = key;
      break;
    }
  }
  if (!phaseKey) return;

  if (!state.pipelineProgress[pid][phaseKey]) {
    state.pipelineProgress[pid][phaseKey] = {};
  }

  state.pipelineProgress[pid][phaseKey][evt.node] = {
    status: evt.status || 'started',
    detail: evt.detail || '',
  };

  // Re-render the progress bar if the container is visible
  const containerId = 'pipeline-' + pid;
  const container = document.getElementById(containerId);
  if (container) {
    renderPipelineProgress(pid, containerId);
  }
}

// ── Node Click Handlers ──────────────────────────────────────────────

function onPipelineNodeClick(projectId, phaseKey, nodeKey) {
  const data = state.pipelineProgress[projectId]?.[phaseKey]?.[nodeKey];
  if (!data) return;

  // Only show actions for active or failed nodes
  if (data.status !== 'started' && data.status !== 'active' && data.status !== 'failed') return;

  const phaseDef = PIPELINE_PHASES[phaseKey];
  if (!phaseDef) return;

  // Create a simple popup near the node
  const existing = document.getElementById('pipeline-node-popup');
  if (existing) existing.remove();

  const popup = document.createElement('div');
  popup.id = 'pipeline-node-popup';
  popup.className = 'pipeline-node-popup';
  popup.innerHTML = `
    <div class="pipeline-popup-header">${escapeHtml(NODE_LABELS[nodeKey] || nodeKey)} - ${escapeHtml(data.status)}</div>
    ${data.detail ? `<div class="pipeline-popup-detail">${escapeHtml(data.detail)}</div>` : ''}
    <div class="pipeline-popup-actions">
      ${data.status === 'failed' ? `<button class="pipeline-popup-btn retry" onclick="retryPipelineNode('${escapeHtml(projectId)}', '${escapeHtml(phaseDef.agent)}', '${escapeHtml(nodeKey)}')">Retry</button>` : ''}
      ${data.status === 'started' || data.status === 'active' ? `<button class="pipeline-popup-btn interrupt" onclick="interruptAgent('${escapeHtml(phaseDef.agent)}')">Interrupt</button>` : ''}
      <button class="pipeline-popup-btn close" onclick="this.closest('.pipeline-node-popup').remove()">Close</button>
    </div>
  `;
  document.body.appendChild(popup);

  // Auto-remove after 10 seconds
  setTimeout(() => { if (popup.parentNode) popup.remove(); }, 10000);
}

async function interruptAgent(agentId) {
  try {
    await fetch(HUB_API + '/api/v1/pipeline/interrupt/' + encodeURIComponent(agentId), {
      method: 'POST',
    });
  } catch (e) {
    console.debug('Failed to interrupt agent:', e);
  }
  const popup = document.getElementById('pipeline-node-popup');
  if (popup) popup.remove();
}

async function retryPipelineNode(projectId, agentId, nodeKey) {
  try {
    await fetch(HUB_API + '/api/v1/pipeline/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, agent_id: agentId, node: nodeKey }),
    });
  } catch (e) {
    console.debug('Failed to retry pipeline node:', e);
  }
  const popup = document.getElementById('pipeline-node-popup');
  if (popup) popup.remove();
}
