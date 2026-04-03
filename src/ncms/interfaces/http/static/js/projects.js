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
    const projects = await resp.json();

    // Preserve _expanded state and loaded documents from previous render
    const prevMap = {};
    for (const p of (state.projects || [])) {
      if (p._expanded) prevMap[p.project_id] = p;
    }
    for (const p of projects) {
      const prev = prevMap[p.project_id];
      if (prev) {
        p._expanded = true;
        // Preserve loaded documents if the fresh data has an empty list
        if (prev.documents && prev.documents.length > 0
            && (!p.documents || p.documents.length === 0)) {
          p.documents = prev.documents;
        }
      }
    }

    state.projects = projects;
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
  const statusOrder = { active: 0, pending: 1, completed: 2, interrupted: 3, failed: 3, denied: 3, archived: 4 };
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
  const docCount = (project.documents || []).length;
  const sourceType = project.source_type || 'research';

  // Determine the currently active agent from pipeline progress
  const progress = state.pipelineProgress?.[project.project_id] || {};
  let activeAgent = '';
  for (const [, phaseData] of Object.entries(progress)) {
    for (const [, nodeData] of Object.entries(phaseData)) {
      if (nodeData.status === 'started' || nodeData.status === 'active') {
        activeAgent = nodeData.agent || '';
      }
    }
  }

  // Chevron
  const chevron = isExpanded ? '&#x25B2;' : '&#x25BC;';

  // Check if this project has a pending approval
  const hasPendingApproval = (state.approvals || []).some(
    a => a.project_id === id && a.status === 'pending'
  );

  // Compact summary chips
  const chips = [];
  if (hasPendingApproval) {
    chips.push(`<span class="project-chip approval-waiting" onclick="event.stopPropagation(); showApprovalForProject('${id}')" title="Awaiting human approval">&#x23F3; Approval</span>`);
  }
  if (sourceType === 'archaeology') {
    chips.push(`<span class="project-chip repo">Repo</span>`);
  }
  if (activeAgent) {
    chips.push(`<span class="project-chip agent">${escapeHtml(activeAgent)}</span>`);
  }
  if (docCount > 0) {
    chips.push(`<span class="project-chip docs">${docCount} doc${docCount !== 1 ? 's' : ''}</span>`);
  }
  const chipHTML = chips.length > 0 ? `<span class="project-chips">${chips.join('')}</span>` : '';

  let html = `<div class="project-card${isExpanded ? ' expanded' : ''}" data-project-id="${id}">
    <div class="project-card-header" onclick="toggleProjectExpand('${id}')">
      <div class="project-card-title-row">
        <span class="project-topic">${topic}</span>
        <span class="project-card-right">
          ${chipHTML}
          <span class="project-status-badge ${status}">${statusLabel}</span>
          <span class="project-chevron">${chevron}</span>
        </span>
      </div>
      <div class="project-card-meta">
        <span class="project-id">${id}</span>
        <span class="project-created">${createdAt}</span>
      </div>
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
  const reviewScores = project.review_scores || [];
  const docLinks = project.document_links || [];
  const qualityScore = project.quality_score;

  let html = '<div class="project-detail">';

  // Pipeline progress bar
  html += `<div class="project-pipeline-container" id="pipeline-${escapeHtml(id)}"></div>`;

  // Quality score summary bar (if we have scores)
  if (qualityScore != null) {
    const scoreColor = qualityScore >= 80 ? '#10b981' : qualityScore >= 60 ? '#f59e0b' : '#ef4444';
    html += `<div class="project-quality-bar">
      <span class="quality-label">Quality</span>
      <div class="quality-gauge">
        <div class="quality-fill" style="width:${Math.min(qualityScore, 100)}%;background:${scoreColor}"></div>
      </div>
      <span class="quality-score" style="color:${scoreColor}">${Math.round(qualityScore)}%</span>
    </div>`;
  }

  // Tab toggle: Document Flow | Audit Timeline
  html += `<div class="project-view-tabs">
    <button class="project-view-tab active" onclick="switchProjectTab('${escapeHtml(id)}', 'docflow', this)">Document Flow</button>
    <button class="project-view-tab" onclick="switchProjectTab('${escapeHtml(id)}', 'audit', this)">Audit Timeline</button>
  </div>`;

  // D3 Document Flow Graph
  if (docs.length > 0 && typeof renderDocFlowGraph === 'function') {
    html += `<div class="doc-flow-container" id="doc-flow-${escapeHtml(id)}"></div>`;
  } else if (docs.length === 0) {
    html += '<div class="phase-timeline-empty">No documents in this project yet</div>';
  }

  // Audit Timeline (hidden by default)
  html += `<div class="audit-timeline-container" id="audit-${escapeHtml(id)}" style="display:none"></div>`;

  // Action buttons
  html += '<div class="project-actions">';
  html += `<a class="project-action-btn export" href="${HUB_API}/api/v1/projects/${encodeURIComponent(id)}/export" download="audit-report-${escapeHtml(id)}.md">Export MD</a>`;
  html += `<button class="project-action-btn export-pdf" onclick="printAuditReport('${escapeHtml(id)}')">Export PDF</button>`;
  if (status === 'active' || status === 'pending') {
    html += `<button class="project-action-btn archive" onclick="archiveProject('${escapeHtml(id)}')">Archive</button>`;
  }
  if (status === 'archived') {
    html += `<span class="project-archived-label">Archived</span>`;
  }
  html += '</div>';

  html += '</div>';

  // Trigger pipeline progress + D3 doc flow render after DOM update
  setTimeout(() => {
    if (typeof renderPipelineProgress === 'function') {
      renderPipelineProgress(id, 'pipeline-' + id);
    }
    if (typeof renderDocFlowGraph === 'function' && docs.length > 0) {
      renderDocFlowGraph('doc-flow-' + id, docs, docLinks, reviewScores);
    }
  }, 0);

  return html;
}

// ── Interactions ─────────────────────────────────────────────────────

async function toggleProjectExpand(projectId) {
  const project = state.projects.find(p => p.project_id === projectId);
  if (!project) return;

  project._expanded = !project._expanded;

  // Fetch linked documents when expanding
  if (project._expanded) {
    try {
      const resp = await fetch(HUB_API + '/api/v1/projects/' + encodeURIComponent(projectId));
      if (resp.ok) {
        const detail = await resp.json();
        project.documents = detail.documents || [];
        project.document_links = detail.document_links || [];
        project.review_scores = detail.review_scores || [];
        project.quality_score = detail.quality_score;
        project.phases = detail.phases || project.phases || [];
      }
    } catch (e) {
      console.debug('Failed to fetch project detail:', e);
    }
  }

  renderProjects();
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

async function printAuditReport(projectId) {
  try {
    const resp = await fetch(HUB_API + '/api/v1/projects/' + encodeURIComponent(projectId) + '/export');
    if (!resp.ok) throw new Error('Failed to fetch report');
    const markdown = await resp.text();

    // Convert markdown to HTML (simple conversion)
    const html = simpleMarkdown(markdown);

    // Open print window with NCMS branding
    const win = window.open('', '_blank');
    win.document.write(`<!DOCTYPE html>
<html>
<head>
<title>NCMS Audit Report — ${projectId}</title>
<style>
  @page {
    size: A4;
    margin: 20mm 15mm 20mm 15mm;
    @top-center {
      content: "NCMS Audit Report";
      font-size: 9px;
      color: #64748b;
    }
    @bottom-center {
      content: "Page " counter(page) " of " counter(pages);
      font-size: 9px;
      color: #64748b;
    }
  }
  body {
    font-family: 'Inter', 'SF Pro', -apple-system, sans-serif;
    font-size: 11px;
    line-height: 1.6;
    color: #1a1a2e;
    max-width: 100%;
  }
  .report-header {
    display: flex;
    align-items: center;
    gap: 16px;
    border-bottom: 3px solid #10b981;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }
  .report-logo {
    font-size: 32px;
    font-weight: 900;
    color: #10b981;
    letter-spacing: 3px;
  }
  .report-logo-sub {
    font-size: 10px;
    color: #64748b;
    letter-spacing: 1px;
  }
  .report-title-block {
    flex: 1;
    text-align: right;
  }
  .report-title-block h1 {
    font-size: 16px;
    margin: 0;
    color: #1a1a2e;
  }
  .report-title-block p {
    margin: 2px 0 0 0;
    font-size: 10px;
    color: #64748b;
  }
  h1 { font-size: 18px; color: #1a1a2e; margin-top: 24px; page-break-after: avoid; }
  h2 { font-size: 14px; color: #10b981; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-top: 20px; page-break-after: avoid; }
  h3 { font-size: 12px; color: #334155; margin-top: 16px; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 10px; page-break-inside: avoid; }
  th { background: #f1f5f9; color: #334155; font-weight: 600; text-align: left; padding: 6px 8px; border: 1px solid #e2e8f0; }
  td { padding: 5px 8px; border: 1px solid #e2e8f0; vertical-align: top; }
  tr:nth-child(even) { background: #f8fafc; }
  code { background: #f1f5f9; padding: 1px 4px; border-radius: 3px; font-size: 10px; font-family: 'SF Mono', monospace; }
  hr { border: none; border-top: 1px solid #e2e8f0; margin: 16px 0; }
  strong { color: #1e293b; }
  ul, ol { padding-left: 20px; }
  li { margin: 2px 0; }
  .report-footer {
    margin-top: 32px;
    padding-top: 12px;
    border-top: 2px solid #10b981;
    font-size: 9px;
    color: #94a3b8;
    text-align: center;
  }
  @media print {
    body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .no-print { display: none; }
  }
</style>
</head>
<body>
  <div class="report-header">
    <div>
      <div class="report-logo">NCMS</div>
      <div class="report-logo-sub">Cognitive Memory System</div>
    </div>
    <div class="report-title-block">
      <h1>Audit Report</h1>
      <p>${projectId} &middot; ${new Date().toLocaleDateString()}</p>
    </div>
  </div>
  <button class="no-print" onclick="window.print()" style="padding:8px 16px;background:#10b981;color:white;border:none;border-radius:6px;cursor:pointer;margin-bottom:16px;font-size:12px">Print / Save as PDF</button>
  ${html}
  <div class="report-footer">
    Generated by NCMS Document Intelligence &middot; ${new Date().toISOString()}
  </div>
</body>
</html>`);
    win.document.close();
  } catch (e) {
    console.error('Failed to generate PDF:', e);
  }
}

function switchProjectTab(projectId, tab, btn) {
  const docFlow = document.getElementById('doc-flow-' + projectId);
  const audit = document.getElementById('audit-' + projectId);

  // Toggle visibility
  if (docFlow) docFlow.style.display = tab === 'docflow' ? '' : 'none';
  if (audit) audit.style.display = tab === 'audit' ? '' : 'none';

  // Update tab button styles
  const tabs = btn.parentElement.querySelectorAll('.project-view-tab');
  tabs.forEach(t => t.classList.remove('active'));
  btn.classList.add('active');

  // Lazy-load audit timeline on first click
  if (tab === 'audit' && audit && !audit.dataset.loaded) {
    audit.dataset.loaded = 'true';
    if (typeof loadAuditTimeline === 'function') {
      loadAuditTimeline(projectId, 'audit-' + projectId);
    }
  }
}

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
        <label class="new-project-label">Source</label>
        <div class="new-project-source">
          <label class="source-radio"><input type="radio" name="source-type" value="research" checked onchange="toggleRepoField()"> Research (Web)</label>
          <label class="source-radio"><input type="radio" name="source-type" value="archaeology" onchange="toggleRepoField()"> Archaeology (GitHub)</label>
        </div>
        <div id="repo-url-field" style="display:none">
          <label class="new-project-label">Repository</label>
          <input type="text" class="new-project-input" id="new-project-repo-url"
                 placeholder="e.g. https://github.com/owner/repo">
        </div>
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

function toggleRepoField() {
  const archaeology = document.querySelector('input[name="source-type"][value="archaeology"]');
  const field = document.getElementById('repo-url-field');
  if (field) field.style.display = archaeology?.checked ? 'block' : 'none';
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

  const sourceType = document.querySelector('input[name="source-type"]:checked')?.value || 'research';
  const repoUrl = (document.getElementById('new-project-repo-url') || {}).value || '';

  if (sourceType === 'archaeology' && !repoUrl.trim()) {
    alert('Please enter a repository URL for archaeology projects.');
    return;
  }

  // Close modal immediately so user sees the project view
  closeNewProject();

  try {
    const resp = await fetch(HUB_API + '/api/v1/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, target, scope, source_type: sourceType, repository_url: repoUrl }),
    });

    if (!resp.ok) {
      console.warn('Project creation failed:', resp.status);
      return;
    }

    const project = await resp.json();
    console.log('Project created:', project.project_id, '- researcher triggered by hub');

    // Refresh project list (hub triggers researcher automatically)
    await loadProjects();

  } catch (e) {
    console.error('Failed to start project:', e);
  }
}

// ── Tab Switching ────────────────────────────────────────────────────

function showProjectsView() {
  // Hide agent view
  const agentView = document.getElementById('agent-view');
  if (agentView) agentView.style.display = 'none';
  // Show projects panel
  const panel = document.getElementById('projects-view');
  if (panel) panel.style.display = 'block';
  // Update nav button states
  updateNavButtons('projects');
  // Load fresh data
  loadProjects();
}

function showAgentView() {
  // Show agent view
  const agentView = document.getElementById('agent-view');
  if (agentView) agentView.style.display = 'grid';
  // Hide projects panel
  const panel = document.getElementById('projects-view');
  if (panel) panel.style.display = 'none';
  // Update nav button states
  updateNavButtons('agents');
}

function updateNavButtons(activeView) {
  // Left nav buttons
  const navAgents = document.getElementById('nav-agents');
  const navProjects = document.getElementById('nav-projects');
  if (navAgents) navAgents.classList.toggle('active', activeView === 'agents');
  if (navProjects) navProjects.classList.toggle('active', activeView === 'projects');

  // Legacy header buttons (if they still exist)
  const projectsBtn = document.getElementById('projects-tab-btn');
  const agentsBtn = document.getElementById('agents-tab-btn');
  if (projectsBtn) projectsBtn.classList.toggle('active', activeView === 'projects');
  if (agentsBtn) agentsBtn.classList.toggle('active', activeView === 'agents');
}

// ── Document Viewer Modal ────────────────────────────────────────

async function openDocumentViewer(docId) {
  // Remove any existing viewer
  const existing = document.getElementById('doc-viewer-overlay');
  if (existing) existing.remove();

  // Create overlay
  const overlay = document.createElement('div');
  overlay.id = 'doc-viewer-overlay';
  overlay.className = 'doc-viewer-overlay';
  overlay.innerHTML = `
    <div class="doc-viewer-modal">
      <div class="doc-viewer-header">
        <span class="doc-viewer-title">Loading...</span>
        <button class="doc-viewer-download" id="doc-viewer-download-btn" style="display:none">Download</button>
        <button class="doc-viewer-close" onclick="closeDocumentViewer()">&times;</button>
      </div>
      <div class="doc-viewer-body">
        <div class="doc-viewer-loading">Loading document...</div>
      </div>
    </div>
  `;

  // Close on backdrop click
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeDocumentViewer();
  });

  document.body.appendChild(overlay);

  // Close on Escape
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      closeDocumentViewer();
      document.removeEventListener('keydown', escHandler);
    }
  };
  document.addEventListener('keydown', escHandler);

  // Fetch document
  try {
    const resp = await fetch(HUB_API + '/api/v1/documents/' + encodeURIComponent(docId));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const doc = await resp.json();

    const bodyEl = overlay.querySelector('.doc-viewer-body');

    // Professional header with metadata
    const agent = doc.from_agent || 'unknown';
    const docType = (doc.title || '').includes('PRD') ? 'PRD'
      : (doc.title || '').includes('Research') ? 'Research'
      : (doc.title || '').includes('Design') ? 'Design'
      : (doc.title || '').includes('Review') ? 'Review'
      : (doc.title || '').includes('Manifest') ? 'Manifest'
      : (doc.title || '').includes('Contract') ? 'Contract'
      : 'Document';
    const typeColors = {
      Research: '#10b981', PRD: '#22c55e', Design: '#f59e0b',
      Review: '#a78bfa', Manifest: '#f97316', Contract: '#8b5cf6', Document: '#64748b'
    };
    const created = doc.created_at ? new Date(doc.created_at).toLocaleString() : '';
    const sizeKB = doc.size_bytes ? (doc.size_bytes / 1024).toFixed(1) + ' KB' : '';

    const headerEl = overlay.querySelector('.doc-viewer-header');
    headerEl.innerHTML = `
      <div class="doc-viewer-header-left">
        <span class="doc-viewer-logo">NCMS</span>
        <span class="doc-viewer-type-badge" style="background:${typeColors[docType] || '#64748b'}20;color:${typeColors[docType] || '#64748b'};border-color:${typeColors[docType] || '#64748b'}40">${escapeHtml(docType)}</span>
        <span class="doc-viewer-agent-badge">${escapeHtml(agent)}</span>
      </div>
      <div class="doc-viewer-header-center">
        <span class="doc-viewer-title">${escapeHtml(doc.title || 'Untitled Document')}</span>
        <span class="doc-viewer-meta">${escapeHtml(created)}${sizeKB ? ' · ' + sizeKB : ''}</span>
      </div>
      <div class="doc-viewer-header-right">
        ${doc.url ? '<button class="doc-viewer-download" id="doc-viewer-download-btn">Download</button>' : ''}
        <button class="doc-viewer-close" onclick="closeDocumentViewer()">&times;</button>
      </div>
    `;

    // Bind download after rendering
    const downloadBtn = overlay.querySelector('#doc-viewer-download-btn');
    if (downloadBtn && doc.url) {
      downloadBtn.onclick = () => {
        const a = document.createElement('a');
        a.href = doc.url;
        a.download = doc.title || 'document';
        a.target = '_blank';
        a.click();
      };
    }

    // Compare button (if doc has a previous version)
    if (doc.parent_doc_id) {
      const compareBtn = document.createElement('button');
      compareBtn.className = 'doc-viewer-compare-btn';
      compareBtn.textContent = 'Compare with previous version';
      compareBtn.onclick = () => showDocDiff(doc.id, doc.parent_doc_id, bodyEl);
      const headerRight = overlay.querySelector('.doc-viewer-header-right');
      if (headerRight) headerRight.insertBefore(compareBtn, headerRight.firstChild);
    }

    // Render content as markdown
    const content = doc.content || doc.body || '';
    if (content) {
      bodyEl.innerHTML = simpleMarkdown(content);
    } else {
      bodyEl.innerHTML = '<div class="doc-viewer-error">No content available for this document.</div>';
    }
  } catch (e) {
    const bodyEl = overlay.querySelector('.doc-viewer-body');
    if (bodyEl) {
      bodyEl.innerHTML = '<div class="doc-viewer-error">Failed to load document: ' + escapeHtml(String(e.message || e)) + '</div>';
    }
  }
}

async function showDocDiff(newDocId, oldDocId, bodyEl) {
  bodyEl.innerHTML = '<div class="doc-viewer-loading">Loading diff...</div>';
  try {
    const [newResp, oldResp] = await Promise.all([
      fetch(HUB_API + '/api/v1/documents/' + encodeURIComponent(newDocId)),
      fetch(HUB_API + '/api/v1/documents/' + encodeURIComponent(oldDocId)),
    ]);
    if (!newResp.ok || !oldResp.ok) throw new Error('Failed to fetch documents');
    const newDoc = await newResp.json();
    const oldDoc = await oldResp.json();

    const newLines = (newDoc.content || '').split('\n');
    const oldLines = (oldDoc.content || '').split('\n');

    // Simple line-by-line diff
    let html = '<div class="doc-diff">';
    html += `<div class="doc-diff-header">
      <span class="diff-old">v${oldDoc.version || '?'} (${(oldDoc.size_bytes / 1024).toFixed(1)} KB)</span>
      <span class="diff-arrow">&rarr;</span>
      <span class="diff-new">v${newDoc.version || '?'} (${(newDoc.size_bytes / 1024).toFixed(1)} KB)</span>
    </div>`;

    const maxLines = Math.max(newLines.length, oldLines.length);
    for (let i = 0; i < maxLines; i++) {
      const oldLine = oldLines[i];
      const newLine = newLines[i];
      if (oldLine === newLine) {
        html += `<div class="diff-line diff-same"><span class="diff-num">${i + 1}</span>${escapeHtml(newLine || '')}</div>`;
      } else if (oldLine === undefined) {
        html += `<div class="diff-line diff-added"><span class="diff-num">+${i + 1}</span>${escapeHtml(newLine)}</div>`;
      } else if (newLine === undefined) {
        html += `<div class="diff-line diff-removed"><span class="diff-num">-${i + 1}</span>${escapeHtml(oldLine)}</div>`;
      } else {
        html += `<div class="diff-line diff-removed"><span class="diff-num">-${i + 1}</span>${escapeHtml(oldLine)}</div>`;
        html += `<div class="diff-line diff-added"><span class="diff-num">+${i + 1}</span>${escapeHtml(newLine)}</div>`;
      }
    }
    html += '</div>';
    bodyEl.innerHTML = html;
  } catch (e) {
    bodyEl.innerHTML = '<div class="doc-viewer-error">Diff failed: ' + escapeHtml(e.message) + '</div>';
  }
}

function closeDocumentViewer() {
  const overlay = document.getElementById('doc-viewer-overlay');
  if (overlay) overlay.remove();
}

// ── SSE Event Listeners ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Auto-update projects on relevant SSE events
  document.addEventListener('ncms:document-published', async () => {
    if (document.getElementById('projects-view')?.style.display !== 'none') {
      // Re-fetch details for any expanded projects so new documents appear
      for (const p of (state.projects || [])) {
        if (p._expanded && p.project_id) {
          try {
            const resp = await fetch(HUB_API + '/api/v1/projects/' + encodeURIComponent(p.project_id));
            if (resp.ok) {
              const detail = await resp.json();
              p.documents = detail.documents || [];
              p.document_links = detail.document_links || [];
              p.review_scores = detail.review_scores || [];
              p.quality_score = detail.quality_score;
            }
          } catch (e) { /* ignore */ }
        }
      }
      renderProjects();
    }
  });

  document.addEventListener('ncms:pipeline-node', (e) => {
    if (typeof handlePipelineProgressEvent === 'function') {
      handlePipelineProgressEvent(e.detail);
    }
  });
});
