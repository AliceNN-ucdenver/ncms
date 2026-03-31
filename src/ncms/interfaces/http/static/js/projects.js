// ── NCMS Dashboard — Projects View ───────────────────────────────────
// Full-width project view tab. Manages project lifecycle, displays
// project cards with phase progress and document timelines.

// Initialize projects in shared state
if (!state.projects) state.projects = [];

// ── Load & Render ────────────────────────────────────────────────────

async function loadProjects() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/projects');
    if (!resp.ok) return;
    state.projects = await resp.json();
    renderProjects();
  } catch (e) {
    console.debug('Failed to load projects:', e);
  }
}

function renderProjects() {
  const container = document.getElementById('projects-panel');
  if (!container) return;

  if (!state.projects || state.projects.length === 0) {
    container.innerHTML = '<div class="projects-empty">No projects yet. Click <strong>+ New Project</strong> to start a pipeline.</div>';
    return;
  }

  // Sort: active first, then by created_at descending
  const statusOrder = { active: 0, pending: 1, completed: 2, failed: 3, archived: 4 };
  const sorted = [...state.projects].sort((a, b) => {
    const sa = statusOrder[a.status] ?? 3;
    const sb = statusOrder[b.status] ?? 3;
    if (sa !== sb) return sa - sb;
    return (b.created_at || '').localeCompare(a.created_at || '');
  });

  // Filter by active status filter if set
  const filter = container.dataset.statusFilter || 'all';
  const filtered = filter === 'all'
    ? sorted
    : sorted.filter(p => p.status === filter);

  let html = '';
  for (const project of filtered) {
    html += projectCardHTML(project);
  }
  container.innerHTML = html;
}

// ── Project Card ─────────────────────────────────────────────────────

function projectCardHTML(project) {
  const id = escapeHtml(project.project_id || '');
  const topic = escapeHtml(project.topic || 'Untitled Project');
  const status = project.status || 'pending';
  const statusLabel = status.charAt(0).toUpperCase() + status.slice(1);
  const createdAt = formatTime(project.created_at || '');
  const isExpanded = project._expanded === true;

  // Phase indicators
  const phases = project.phases || [];
  let phaseHTML = '';
  for (const phase of phases) {
    const pName = escapeHtml(phase.name || '');
    const pStatus = phase.status || 'waiting';
    let icon = '';
    let cls = 'phase-indicator';
    if (pStatus === 'completed') { icon = ' &#x2713;'; cls += ' completed'; }
    else if (pStatus === 'active' || pStatus === 'in_progress') { icon = ' &#x25B6;'; cls += ' active'; }
    else if (pStatus === 'failed') { icon = ' &#x2717;'; cls += ' failed'; }
    else { cls += ' waiting'; }

    // Show percentage if available
    const pct = phase.progress != null ? Math.round(phase.progress) + '%' : '';
    phaseHTML += `<span class="${cls}">${pName}${pct ? ' ' + pct : ''}${icon}</span>`;
  }

  // Quality score
  const quality = project.quality_score != null
    ? `<span class="project-quality">Quality: ${Math.round(project.quality_score)}%</span>`
    : '';

  // Total time
  const totalTime = project.total_time_ms != null
    ? `<span class="project-time">${formatDurationMs(project.total_time_ms)}</span>`
    : '';

  let html = `<div class="project-card${isExpanded ? ' expanded' : ''}" data-project-id="${id}">
    <div class="project-card-header" onclick="toggleProjectExpand('${id}')">
      <div class="project-card-title-row">
        <span class="project-topic">${topic}</span>
        <span class="project-status-badge ${status}">${statusLabel}</span>
      </div>
      <div class="project-card-meta">
        <span class="project-id">${id}</span>
        <span class="project-created">${createdAt}</span>
        ${quality}${totalTime}
      </div>
      <div class="project-phases">${phaseHTML}</div>
    </div>`;

  if (isExpanded) {
    html += renderProjectDetail(project);
  }

  html += '</div>';
  return html;
}

function renderProjectDetail(project) {
  const id = project.project_id || '';
  const docs = project.documents || [];
  const status = project.status || 'pending';

  let html = '<div class="project-detail">';

  // Pipeline progress bar
  html += `<div class="project-pipeline-container" id="pipeline-${escapeHtml(id)}"></div>`;

  // Phase timeline with documents
  if (docs.length > 0) {
    html += '<div class="phase-timeline">';
    for (const doc of docs) {
      const docTitle = escapeHtml(doc.title || 'Untitled');
      const docTime = formatTime(doc.created_at || '');
      const docSize = doc.size != null ? formatDocSize(doc.size) : '';
      const versionMatch = (doc.title || '').match(/\b(v\d+)\b/);
      const versionBadge = versionMatch
        ? `<span class="doc-version-badge">${versionMatch[1]}</span>`
        : '';
      const docAgent = escapeHtml(doc.from_agent || '');

      html += `<div class="phase-timeline-item">
        <div class="phase-timeline-dot"></div>
        <div class="phase-timeline-content">
          <div class="phase-timeline-header">
            <span class="phase-timeline-title">${docTitle}</span>
            <span class="phase-timeline-meta">
              ${versionBadge}
              <span class="phase-timeline-agent">${docAgent}</span>
              ${docSize ? `<span class="phase-timeline-size">${docSize}</span>` : ''}
              <span class="phase-timeline-time">${docTime}</span>
            </span>
          </div>
          <div class="phase-timeline-actions">
            ${doc.url ? `<a class="document-download-btn" href="${escapeHtml(doc.url)}" target="_blank" download>Download</a>` : ''}
            ${doc.next_agent ? `<button class="doc-action-btn send-to-next" onclick="sendDocToAgent('${escapeHtml(doc.next_agent)}', '${escapeHtml(doc.document_id || '')}', '${escapeHtml((doc.title || '').replace(/'/g, "\\'"))}', 'Process this document')">Send to ${escapeHtml(doc.next_agent)}</button>` : ''}
          </div>
        </div>
      </div>`;
    }
    html += '</div>';
  } else {
    html += '<div class="phase-timeline-empty">No documents in this project yet</div>';
  }

  // Action buttons
  html += '<div class="project-actions">';
  if (status === 'active' || status === 'pending') {
    html += `<button class="project-action-btn archive" onclick="archiveProject('${escapeHtml(id)}')">Archive</button>`;
  }
  if (status === 'archived') {
    html += `<span class="project-archived-label">Archived</span>`;
  }
  html += '</div>';

  html += '</div>';

  // Trigger pipeline progress render after DOM update
  setTimeout(() => {
    if (typeof renderPipelineProgress === 'function') {
      renderPipelineProgress(id, 'pipeline-' + id);
    }
  }, 0);

  return html;
}

// ── Interactions ─────────────────────────────────────────────────────

function toggleProjectExpand(projectId) {
  const project = state.projects.find(p => p.project_id === projectId);
  if (project) {
    project._expanded = !project._expanded;
    renderProjects();
  }
}

async function archiveProject(projectId) {
  try {
    await fetch(HUB_API + '/api/v1/projects/' + encodeURIComponent(projectId) + '/archive', {
      method: 'POST',
    });
    loadProjects();
  } catch (e) {
    console.debug('Failed to archive project:', e);
  }
}

// ── New Project Modal ────────────────────────────────────────────────

function openNewProject() {
  const existing = document.getElementById('new-project-panel');
  if (existing) {
    existing.style.display = existing.style.display === 'none' ? 'flex' : 'none';
    return;
  }

  const panel = document.createElement('div');
  panel.className = 'new-project-panel';
  panel.id = 'new-project-panel';
  panel.innerHTML = `
    <div class="new-project-inner">
      <div class="new-project-header">
        <span class="new-project-title">New Project</span>
        <button class="new-project-close" onclick="closeNewProject()">&times;</button>
      </div>
      <div class="new-project-form">
        <label class="new-project-label">Topic</label>
        <input type="text" class="new-project-input" id="new-project-topic"
               placeholder="e.g. Authentication microservice redesign">
        <label class="new-project-label">Target</label>
        <input type="text" class="new-project-input" id="new-project-target"
               placeholder="e.g. Production-ready design document">
        <label class="new-project-label">Scope</label>
        <div class="new-project-scope">
          <label class="scope-checkbox"><input type="checkbox" id="scope-research" checked> Research</label>
          <label class="scope-checkbox"><input type="checkbox" id="scope-prd" checked> PRD</label>
          <label class="scope-checkbox"><input type="checkbox" id="scope-design" checked> Design</label>
          <label class="scope-checkbox"><input type="checkbox" id="scope-implement"> Implement</label>
        </div>
        <button class="new-project-start-btn" onclick="startProject()">Start Pipeline</button>
      </div>
    </div>
  `;
  document.body.appendChild(panel);
}

function closeNewProject() {
  const panel = document.getElementById('new-project-panel');
  if (panel) panel.style.display = 'none';
}

async function startProject() {
  const topic = (document.getElementById('new-project-topic') || {}).value || '';
  const target = (document.getElementById('new-project-target') || {}).value || '';
  if (!topic.trim()) return;

  const scope = [];
  if (document.getElementById('scope-research')?.checked) scope.push('research');
  if (document.getElementById('scope-prd')?.checked) scope.push('prd');
  if (document.getElementById('scope-design')?.checked) scope.push('design');
  if (document.getElementById('scope-implement')?.checked) scope.push('implement');

  try {
    const resp = await fetch(HUB_API + '/api/v1/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, target, scope }),
    });
    if (resp.ok) {
      const project = await resp.json();
      state.projects.unshift(project);
      renderProjects();
      closeNewProject();
    }
  } catch (e) {
    console.debug('Failed to start project:', e);
  }
}

// ── Tab Switching ────────────────────────────────────────────────────

function showProjectsView() {
  // Hide agent view elements
  document.querySelector('.main').style.display = 'none';
  // Show projects panel
  const panel = document.getElementById('projects-view');
  if (panel) panel.style.display = 'block';
  // Update tab button states
  updateViewTabButtons('projects');
  // Load fresh data
  loadProjects();
}

function showAgentView() {
  // Show agent view elements
  document.querySelector('.main').style.display = 'grid';
  // Hide projects panel
  const panel = document.getElementById('projects-view');
  if (panel) panel.style.display = 'none';
  // Update tab button states
  updateViewTabButtons('agents');
}

function updateViewTabButtons(activeView) {
  const projectsBtn = document.getElementById('projects-tab-btn');
  const agentsBtn = document.getElementById('agents-tab-btn');
  if (projectsBtn) {
    projectsBtn.classList.toggle('active', activeView === 'projects');
  }
  if (agentsBtn) {
    agentsBtn.classList.toggle('active', activeView === 'agents');
  }
}

// ── SSE Event Listeners ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Auto-update projects on relevant SSE events
  document.addEventListener('ncms:document-published', () => {
    if (document.getElementById('projects-view')?.style.display !== 'none') {
      loadProjects();
    }
  });

  document.addEventListener('ncms:pipeline-node', (e) => {
    if (typeof handlePipelineProgressEvent === 'function') {
      handlePipelineProgressEvent(e.detail);
    }
  });
});
