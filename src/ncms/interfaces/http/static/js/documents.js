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
      html += `<div class="document-card">
        <div class="document-card-header" onclick="toggleDocContent('${doc.document_id}')">
          <span class="document-title">${escapeHtml(doc.title || 'Untitled')}</span>
          <span class="document-time">${formatTime(doc.created_at || '')}</span>
        </div>`;

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
          <button class="doc-action-btn send-to-builder" onclick="sendDocToBuilder('${doc.document_id}', '${escapedTitle}')">📐 Send to Builder</button>
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
