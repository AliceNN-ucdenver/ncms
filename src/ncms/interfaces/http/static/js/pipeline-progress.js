// ── NCMS Dashboard — Pipeline Progress ───────────────────────────────
// Renders live pipeline progress bars inside expanded project cards.
// Shows per-phase node status with animated indicators.

// Pipeline progress state: project_id -> { phases: { agent: [nodes] } }
if (!state.pipelineProgress) state.pipelineProgress = {};

// ── Default Pipeline Definitions ─────────────────────────────────────
// Each phase defines its agent and the ordered processing nodes.

const PIPELINE_PHASES = {
  research:  { agent: 'researcher',     label: 'Research',  nodes: ['plan', 'search', 'synthesize', 'publish', 'trigger'] },
  prd:       { agent: 'product_owner',  label: 'PRD',       nodes: ['read_doc', 'ask_experts', 'synthesize', 'publish', 'trigger'] },
  design:    { agent: 'builder',        label: 'Design',    nodes: ['read_doc', 'ask_experts', 'synthesize', 'validate', 'publish', 'review', 'contracts'] },
  implement: { agent: 'builder',        label: 'Implement', nodes: ['read_doc', 'scaffold', 'generate', 'test', 'publish'] },
};

const NODE_LABELS = {
  plan: 'Plan',
  search: 'Search',
  synthesize: 'Synthesize',
  publish: 'Publish',
  trigger: 'Trigger',
  read_doc: 'Read Doc',
  ask_experts: 'Ask Experts',
  validate: 'Validate',
  review: 'Review',
  contracts: 'Contracts',
  scaffold: 'Scaffold',
  generate: 'Generate',
  test: 'Test',
};

// ── Render ───────────────────────────────────────────────────────────

function renderPipelineProgress(projectId, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

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
  // event: { project_id, agent, node, status, detail }
  if (!event || !event.project_id) return;

  const pid = event.project_id;
  if (!state.pipelineProgress[pid]) state.pipelineProgress[pid] = {};

  // Determine which phase this agent belongs to
  let phaseKey = null;
  for (const [key, def] of Object.entries(PIPELINE_PHASES)) {
    if (def.agent === event.agent) {
      phaseKey = key;
      break;
    }
  }
  if (!phaseKey) return;

  if (!state.pipelineProgress[pid][phaseKey]) {
    state.pipelineProgress[pid][phaseKey] = {};
  }

  state.pipelineProgress[pid][phaseKey][event.node] = {
    status: event.status || 'started',
    detail: event.detail || '',
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
