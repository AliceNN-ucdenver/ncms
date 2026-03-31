// ── NCMS Dashboard — Prompt Editor ──────────────────────────────────
// Full-screen overlay for viewing/editing agent prompts.
// Loads from HUB_API /api/v1/prompts, saves via POST.

const PROMPT_AGENTS = ['researcher', 'product_owner', 'builder', 'architect', 'security'];

let _promptEditorState = {
  prompts: [],
  selected: null,  // { agent_id, prompt_type }
};

function openPromptEditor() {
  const existing = document.getElementById('prompt-editor-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'prompt-editor-overlay';
  overlay.className = 'editor-overlay';
  overlay.innerHTML = `
    <div class="editor-modal">
      <div class="editor-header">
        <span class="editor-title">&#x270F; Prompt Editor</span>
        <button class="editor-close-btn" onclick="closePromptEditor()">&times;</button>
      </div>
      <div class="editor-content">
        <div class="editor-sidebar" id="prompt-editor-sidebar">
          <div class="editor-sidebar-loading">Loading prompts...</div>
        </div>
        <div class="editor-main" id="prompt-editor-main">
          <div class="editor-placeholder">Select a prompt from the sidebar</div>
        </div>
      </div>
    </div>
  `;

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closePromptEditor();
  });

  document.body.appendChild(overlay);
  loadPrompts();
}

function closePromptEditor() {
  const overlay = document.getElementById('prompt-editor-overlay');
  if (overlay) overlay.remove();
}

async function loadPrompts() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/prompts');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _promptEditorState.prompts = Array.isArray(data) ? data : (data.prompts || []);
  } catch (e) {
    _promptEditorState.prompts = [];
    console.debug('Failed to load prompts:', e);
  }
  renderPromptSidebar();
}

function renderPromptSidebar() {
  const sidebar = document.getElementById('prompt-editor-sidebar');
  if (!sidebar) return;

  const prompts = _promptEditorState.prompts;
  if (prompts.length === 0) {
    sidebar.innerHTML = '<div class="editor-sidebar-empty">No prompts found</div>';
    return;
  }

  // Group by agent_id
  const grouped = {};
  for (const p of prompts) {
    const agent = p.agent_id || 'unknown';
    if (!grouped[agent]) grouped[agent] = [];
    grouped[agent].push(p);
  }

  let html = '';
  for (const agent of PROMPT_AGENTS) {
    const items = grouped[agent];
    if (!items || items.length === 0) continue;

    html += `<div class="editor-sidebar-group">
      <div class="editor-sidebar-group-label">${escapeHtml(agent)}</div>`;

    for (const p of items) {
      const type = p.prompt_type || 'default';
      const sel = _promptEditorState.selected;
      const isActive = sel && sel.agent_id === agent && sel.prompt_type === type;
      html += `<div class="editor-sidebar-item${isActive ? ' active' : ''}"
                    onclick="selectPrompt('${escapeHtml(agent)}', '${escapeHtml(type)}')">
        <span class="editor-sidebar-item-type">${escapeHtml(type)}</span>
        <span class="editor-sidebar-item-version">v${p.version || 1}</span>
      </div>`;
    }
    html += '</div>';
  }

  // Any agents not in the standard list
  for (const agent of Object.keys(grouped)) {
    if (PROMPT_AGENTS.includes(agent)) continue;
    const items = grouped[agent];
    html += `<div class="editor-sidebar-group">
      <div class="editor-sidebar-group-label">${escapeHtml(agent)}</div>`;
    for (const p of items) {
      const type = p.prompt_type || 'default';
      const sel = _promptEditorState.selected;
      const isActive = sel && sel.agent_id === agent && sel.prompt_type === type;
      html += `<div class="editor-sidebar-item${isActive ? ' active' : ''}"
                    onclick="selectPrompt('${escapeHtml(agent)}', '${escapeHtml(type)}')">
        <span class="editor-sidebar-item-type">${escapeHtml(type)}</span>
        <span class="editor-sidebar-item-version">v${p.version || 1}</span>
      </div>`;
    }
    html += '</div>';
  }

  sidebar.innerHTML = html;
}

function selectPrompt(agentId, promptType) {
  _promptEditorState.selected = { agent_id: agentId, prompt_type: promptType };
  renderPromptSidebar();
  renderPromptMain();
}

function renderPromptMain() {
  const main = document.getElementById('prompt-editor-main');
  if (!main) return;

  const sel = _promptEditorState.selected;
  if (!sel) {
    main.innerHTML = '<div class="editor-placeholder">Select a prompt from the sidebar</div>';
    return;
  }

  const prompt = _promptEditorState.prompts.find(
    p => p.agent_id === sel.agent_id && p.prompt_type === sel.prompt_type
  );
  if (!prompt) {
    main.innerHTML = '<div class="editor-placeholder">Prompt not found</div>';
    return;
  }

  const content = prompt.content || '';
  const version = prompt.version || 1;
  const updatedAt = prompt.updated_at ? formatTimeFull(prompt.updated_at) : 'unknown';
  const versions = prompt.versions || [];

  let versionsHtml = '';
  if (versions.length > 0) {
    versionsHtml = '<div class="editor-versions"><div class="editor-versions-title">Version History</div>';
    for (const v of versions) {
      versionsHtml += `<div class="editor-version-item">
        <span class="editor-version-num">v${v.version || '?'}</span>
        <span class="editor-version-time">${formatTimeFull(v.updated_at || v.created_at || '')}</span>
      </div>`;
    }
    versionsHtml += '</div>';
  }

  main.innerHTML = `
    <div class="editor-main-header">
      <span class="editor-main-label">${escapeHtml(sel.agent_id)} / ${escapeHtml(sel.prompt_type)}</span>
      <span class="editor-main-meta">Version ${version} &middot; Updated ${updatedAt}</span>
    </div>
    <textarea class="editor-textarea" id="prompt-editor-textarea">${escapeHtml(content)}</textarea>
    <div class="editor-actions">
      <button class="editor-save-btn" onclick="savePrompt()">Save</button>
      <span class="editor-save-status" id="prompt-save-status"></span>
    </div>
    ${versionsHtml}
  `;
}

async function savePrompt() {
  const sel = _promptEditorState.selected;
  if (!sel) return;

  const textarea = document.getElementById('prompt-editor-textarea');
  if (!textarea) return;

  const statusEl = document.getElementById('prompt-save-status');
  if (statusEl) statusEl.textContent = 'Saving...';

  try {
    const resp = await fetch(HUB_API + '/api/v1/prompts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: sel.agent_id,
        prompt_type: sel.prompt_type,
        content: textarea.value,
      }),
    });

    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    if (statusEl) {
      statusEl.textContent = 'Saved';
      statusEl.style.color = 'var(--accent-green)';
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
    }

    // Reload prompts to reflect new version
    await loadPrompts();
    renderPromptMain();
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = 'Failed: ' + (e.message || e);
      statusEl.style.color = 'var(--accent-amber)';
    }
  }
}

// Escape key support (integrated into app.js pattern)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const overlay = document.getElementById('prompt-editor-overlay');
    if (overlay) {
      closePromptEditor();
      e.stopPropagation();
    }
  }
});
