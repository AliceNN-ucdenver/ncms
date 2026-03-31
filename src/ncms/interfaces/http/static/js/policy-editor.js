// ── NCMS Dashboard — Policy Editor ──────────────────────────────────
// Full-screen overlay for viewing/editing project policies.
// Loads from HUB_API /api/v1/policies, saves via POST.

let _policyEditorState = {
  policies: [],
};

function openPolicyEditor() {
  const existing = document.getElementById('policy-editor-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'policy-editor-overlay';
  overlay.className = 'editor-overlay';
  overlay.innerHTML = `
    <div class="editor-modal">
      <div class="editor-header">
        <span class="editor-title">&#x1F6E1; Policy Editor</span>
        <button class="editor-close-btn" onclick="closePolicyEditor()">&times;</button>
      </div>
      <div class="editor-content" style="flex-direction:column;overflow-y:auto;padding:24px;gap:20px;">
        <div class="policy-loading" id="policy-editor-body">Loading policies...</div>
      </div>
    </div>
  `;

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closePolicyEditor();
  });

  document.body.appendChild(overlay);
  loadPolicies();
}

function closePolicyEditor() {
  const overlay = document.getElementById('policy-editor-overlay');
  if (overlay) overlay.remove();
}

async function loadPolicies() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/policies');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _policyEditorState.policies = Array.isArray(data) ? data : (data.policies || []);
  } catch (e) {
    _policyEditorState.policies = [];
    console.debug('Failed to load policies:', e);
  }
  renderPolicyCards();
}

const POLICY_LABELS = {
  domain_scope: 'Domain Scope',
  technology_scope: 'Technology Scope',
  compliance_requirements: 'Compliance Requirements',
};

const POLICY_ICONS = {
  domain_scope: '&#x1F30D;',
  technology_scope: '&#x2699;',
  compliance_requirements: '&#x1F4CB;',
};

function renderPolicyCards() {
  const body = document.getElementById('policy-editor-body');
  if (!body) return;

  const policies = _policyEditorState.policies;
  if (policies.length === 0) {
    body.innerHTML = '<div class="editor-placeholder">No policies found</div>';
    return;
  }

  let html = '';
  for (const policy of policies) {
    const type = policy.policy_type || 'unknown';
    const label = POLICY_LABELS[type] || type;
    const icon = POLICY_ICONS[type] || '&#x1F4C4;';
    const content = policy.content || '';
    const version = policy.version || 1;
    const updatedAt = policy.updated_at ? formatTimeFull(policy.updated_at) : 'unknown';
    const safeType = escapeHtml(type);

    html += `<div class="policy-card" data-policy-type="${safeType}">
      <div class="policy-card-header">
        <span class="policy-card-icon">${icon}</span>
        <span class="policy-card-title">${escapeHtml(label)}</span>
        <span class="policy-card-version">v${version}</span>
        <span class="policy-card-updated">Updated ${updatedAt}</span>
      </div>
      <textarea class="editor-textarea policy-textarea" id="policy-textarea-${safeType}">${escapeHtml(content)}</textarea>
      <div class="editor-actions">
        <button class="editor-save-btn" onclick="savePolicy('${safeType}')">Save</button>
        <span class="editor-save-status" id="policy-save-status-${safeType}"></span>
      </div>
    </div>`;
  }

  body.innerHTML = html;
}

async function savePolicy(policyType) {
  const textarea = document.getElementById('policy-textarea-' + policyType);
  if (!textarea) return;

  const statusEl = document.getElementById('policy-save-status-' + policyType);
  if (statusEl) statusEl.textContent = 'Saving...';

  try {
    const resp = await fetch(HUB_API + '/api/v1/policies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        policy_type: policyType,
        content: textarea.value,
      }),
    });

    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    if (statusEl) {
      statusEl.textContent = 'Saved';
      statusEl.style.color = 'var(--accent-green)';
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
    }

    // Reload to reflect new version
    await loadPolicies();
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = 'Failed: ' + (e.message || e);
      statusEl.style.color = 'var(--accent-amber)';
    }
  }
}

// Escape key support
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const overlay = document.getElementById('policy-editor-overlay');
    if (overlay) {
      closePolicyEditor();
      e.stopPropagation();
    }
  }
});
