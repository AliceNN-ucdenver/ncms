// ── NCMS Dashboard — Documents Sidebar ───────────────────────────────
// Right sidebar showing published documents grouped by agent.

async function loadDocuments() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/documents');
    if (!resp.ok) return;
    const docs = await resp.json();
    state.documents = docs;
    renderDocuments();
  } catch (e) {
    console.debug('Failed to load documents:', e);
  }
}

function renderDocuments() {
  const container = document.getElementById('documents-panel');
  if (!container) return;

  if (!state.documents || state.documents.length === 0) {
    container.innerHTML = '<div class="documents-empty">No documents yet</div>';
    return;
  }

  // Group by agent
  const grouped = {};
  for (const doc of state.documents) {
    const agent = doc.from_agent || 'unknown';
    if (!grouped[agent]) grouped[agent] = [];
    grouped[agent].push(doc);
  }

  let html = '';
  for (const [agent, docs] of Object.entries(grouped)) {
    html += `<div class="doc-agent-group">
      <div class="doc-agent-label">${escapeHtml(agent)} <span class="doc-count">(${docs.length})</span></div>`;

    for (const doc of docs) {
      const isExpanded = doc._expanded === true;
      const versionMatch = (doc.title || '').match(/\b(v\d+)\b/);
      const versionBadge = versionMatch
        ? `<span class="doc-version-badge">${versionMatch[1]}</span>`
        : '';
      const reviewMatch = (doc.title || '').match(/Review Report/i);
      const reviewBadge = reviewMatch
        ? `<span class="doc-review-badge">Review</span>`
        : '';

      // Entity tags from GLiNER extraction
      const entities = doc.entities || [];
      let entityHTML = '';
      if (entities.length > 0) {
        entityHTML = '<div class="doc-entity-tags">';
        for (const ent of entities.slice(0, 8)) {
          const typeCls = `entity-type-${(ent.type || 'concept').replace(/\s+/g, '-')}`;
          entityHTML += `<span class="doc-entity-tag ${typeCls}">${escapeHtml(ent.name)}</span>`;
        }
        if (entities.length > 8) {
          entityHTML += `<span class="doc-entity-more">+${entities.length - 8}</span>`;
        }
        entityHTML += '</div>';
      }

      html += `<div class="document-card">
        <div class="document-card-header" onclick="toggleDocContent('${doc.document_id}')">
          <span class="document-title">${escapeHtml(doc.title || 'Untitled')}</span>
          <span style="display:flex;gap:4px;align-items:center">
            ${versionBadge}${reviewBadge}
            <span class="document-time">${formatTime(doc.created_at || '')}</span>
          </span>
        </div>
        ${entityHTML}`;

      if (isExpanded) {
        if (doc._content) {
          html += `<div class="document-content">${simpleMarkdown(doc._content)}</div>`;
        } else {
          html += `<div class="document-content"><em style="color:var(--text-muted)">Loading...</em></div>`;
          loadDocContent(doc.document_id);
        }
        const escapedTitle = (doc.title || 'Untitled').replace(/'/g, "\\'").replace(/"/g, '&quot;');
        html += `<div class="document-actions">
          <a class="document-download-btn" href="${escapeHtml(doc.url || '#')}" target="_blank" download>Download</a>
          ${getDocRouteButtons(doc, escapedTitle)}
          ${doc.plan_id ? `<button class="document-plan-link" onclick="showApprovalForPlan('${escapeHtml(doc.plan_id)}')">View Plan</button>` : ''}
        </div>`;
      }

      html += '</div>';
    }
    html += '</div>';
  }

  container.innerHTML = html;
}

async function loadDocContent(docId) {
  try {
    const resp = await fetch(HUB_API + '/api/v1/documents/' + docId);
    if (!resp.ok) return;
    const data = await resp.json();
    const doc = state.documents.find(d => d.document_id === docId);
    if (doc) {
      doc._content = data.content || '';
      renderDocuments();
    }
  } catch (e) {
    console.debug('Failed to load document content:', e);
  }
}

function toggleDocContent(docId) {
  const doc = state.documents.find(d => d.document_id === docId);
  if (doc) {
    doc._expanded = !doc._expanded;
    renderDocuments();
  }
}

function getDocRouteButtons(doc, escapedTitle) {
  const agent = (doc.from_agent || '').toLowerCase();
  const id = doc.document_id;
  let buttons = '';
  if (agent === 'researcher') {
    buttons += `<button class="doc-action-btn send-to-po" onclick="sendDocToAgent('product_owner', '${id}', '${escapedTitle}', 'Create a PRD based on this market research')">📋 Send to Product Owner</button>`;
  }
  if (agent === 'researcher' || agent === 'product_owner') {
    buttons += `<button class="doc-action-btn send-to-builder" onclick="sendDocToAgent('builder', '${id}', '${escapedTitle}', 'Create a detailed implementation design based on this PRD')">📐 Send to Builder</button>`;
  }
  if (agent === 'builder') {
    buttons += `<button class="doc-action-btn send-to-architect" onclick="sendDocToAgent('architect', '${id}', '${escapedTitle}', 'Review this implementation design for architectural compliance')">🏗️ Send to Architect</button>`;
    buttons += `<button class="doc-action-btn send-to-security" onclick="sendDocToAgent('security', '${id}', '${escapedTitle}', 'Review this implementation design for security compliance')">🔒 Send to Security</button>`;
  }
  return buttons;
}

function sendDocToAgent(agentId, docId, title, prompt) {
  if (typeof openAgentChat === 'function') {
    openAgentChat(agentId);
  }
  setTimeout(() => {
    const input = document.getElementById('chat-overlay-input');
    if (input) {
      input.value = prompt + ': "' + title + '" (doc_id: ' + docId + '). Use ask_knowledge to consult domain experts, then publish your output document.';
      input.focus();
    }
  }, 300);
}

function sendDocToBuilder(docId, title) {
  // Open builder chat overlay
  if (typeof openAgentChat === 'function') {
    openAgentChat('builder');
  }
  // Pre-fill the chat input after a short delay (overlay needs to render)
  setTimeout(() => {
    const input = document.getElementById('chat-overlay-input');
    if (input) {
      input.value = 'Create a detailed implementation design based on PRD: "' + title + '" (doc_id: ' + docId + '). Use ask_knowledge to consult the architect and security agents, then call create_design with the results.';
      input.focus();
    }
  }, 300);
}

function formatDocSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
