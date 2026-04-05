// ── NCMS Dashboard — Guardrail Approval Gate ────────────────────────
// Human-in-the-loop approval for guardrail violations.
// Agents pause at guardrail gates and poll for approve/deny.
// The dashboard fetches pending approvals from /api/v1/approvals.

// Approval state lives in state.approvals[] (shared with app.js)

const _APPROVAL_POLL_MS = 30000; // poll every 30s (SSE handles instant updates)
let _approvalPollTimer = null;

// ── Load from API ───────────────────────────────────────────────────

async function loadPendingApprovals() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/approvals?status=pending');
    if (!resp.ok) return;
    const approvals = await resp.json();

    // Merge with local state (preserve expanded flag)
    const prevMap = {};
    for (const a of (state.approvals || [])) {
      prevMap[a.id] = a;
    }

    state.approvals = approvals.map(a => ({
      ...a,
      expanded: prevMap[a.id]?.expanded ?? true,
    }));

    renderApprovalQueue();
    updateApprovalBadge();
  } catch (e) {
    console.debug('Failed to load pending approvals:', e);
  }
}

function startApprovalPolling() {
  if (_approvalPollTimer) return;
  _approvalPollTimer = setInterval(loadPendingApprovals, _APPROVAL_POLL_MS);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) {
    clearInterval(_approvalPollTimer);
    _approvalPollTimer = null;
  }
}

// ── SSE integration ─────────────────────────────────────────────────

function handleApprovalSSE(data) {
  // Called from SSE handler when type=approval_requested or approval_decided
  loadPendingApprovals();
}

// ── Render ──────────────────────────────────────────────────────────

function toggleApprovalContent(approvalId) {
  const approval = state.approvals.find(a => a.id === approvalId);
  if (approval) {
    approval.expanded = !approval.expanded;
    renderApprovalQueue();
  }
}

function renderApprovalQueue() {
  const container = document.getElementById('approval-queue');
  if (!container) return;

  const pending = (state.approvals || []).filter(a => a.status === 'pending');

  if (pending.length === 0) {
    container.innerHTML = '<div class="approval-empty">No pending approvals. Guardrail violations will appear here when agents are blocked.</div>';
    return;
  }

  let html = '';
  for (const approval of pending) {
    const violations = approval.violations || [];
    const agent = escapeHtml(approval.agent || 'unknown');
    const node = escapeHtml(approval.node || 'unknown');
    const projectId = escapeHtml(approval.project_id || '');
    const isExpanded = approval.expanded !== false;
    const created = formatTimeFull(approval.created_at || '');

    // Violation severity summary
    const blockCount = violations.filter(v => v.escalation === 'block' || v.escalation === 'reject').length;
    const warnCount = violations.filter(v => v.escalation === 'warn').length;

    html += `<div class="approval-card pending">
      <div class="approval-header" onclick="toggleApprovalContent('${escapeHtml(approval.id)}')">
        <div style="flex:1">
          <div class="approval-title">
            <span class="guardrail-icon">&#x26A0;</span>
            Guardrail Gate: ${agent} / ${node}
          </div>
          <div class="approval-meta">
            ${projectId ? `<span class="approval-plan-id">${projectId}</span>` : ''}
            <span>${blockCount} blocking, ${warnCount} warning</span>
            <span>${created}</span>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="approval-status-badge pending">Awaiting Decision</span>
        </div>
      </div>`;

    if (isExpanded) {
      // Show violation details
      html += '<div class="approval-content"><h3>Violations</h3><ul>';
      for (const v of violations) {
        const escLevel = v.escalation === 'reject' ? 'reject' : v.escalation === 'block' ? 'block' : 'warn';
        html += `<li class="violation-item violation-${escLevel}">
          <span class="violation-badge ${escLevel}">${escapeHtml(v.escalation || 'warn').toUpperCase()}</span>
          <strong>${escapeHtml(v.policy_type || '')}</strong>: ${escapeHtml(v.message || v.rule || '')}
        </li>`;
      }
      html += '</ul>';

      // Context preview
      const ctx = approval.context || {};
      if (ctx.topic) {
        html += `<div class="approval-context"><strong>Topic:</strong> ${escapeHtml(ctx.topic)}</div>`;
      }
      if (ctx.design_preview) {
        html += `<div class="approval-context"><strong>Design preview:</strong> ${escapeHtml(ctx.design_preview.substring(0, 300))}...</div>`;
      }
      html += '</div>';

      // Action buttons
      html += `<div class="approval-actions-row">
        <button class="approval-btn approve" onclick="submitGuardrailDecision('${escapeHtml(approval.id)}', 'approved')">Approve &amp; Continue</button>
        <button class="approval-btn reject" onclick="submitGuardrailDecision('${escapeHtml(approval.id)}', 'denied')">Deny &amp; Stop Pipeline</button>
      </div>
      <div style="margin-top:8px">
        <input type="text" class="approval-comment" id="comment-${escapeHtml(approval.id)}"
               placeholder="Optional comment...">
      </div>`;
    }

    html += '</div>';
  }

  container.innerHTML = html;
  updateApprovalBadge();
}

// ── Submit Decision ─────────────────────────────────────────────────

async function submitGuardrailDecision(approvalId, decision) {
  const commentInput = document.getElementById('comment-' + approvalId);
  const comment = commentInput ? commentInput.value.trim() : '';

  try {
    const resp = await fetch(HUB_API + '/api/v1/approvals/' + encodeURIComponent(approvalId) + '/decide', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision: decision,
        decided_by: 'human',
        comment: comment || null,
      }),
    });

    if (!resp.ok) {
      console.error('Failed to submit decision:', resp.status);
      return;
    }

    // Remove from local pending list
    state.approvals = state.approvals.filter(a => a.id !== approvalId);
    renderApprovalQueue();
    updateApprovalBadge();

    // Close panel if no more pending
    if (state.approvals.filter(a => a.status === 'pending').length === 0) {
      closeApprovalPanel();
    }
  } catch (err) {
    console.error('Failed to submit guardrail decision:', err);
  }
}

// ── Floating Panel Controls ─────────────────────────────────────────

function toggleApprovalPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('approval-float');
  if (panel.classList.contains('open')) {
    closeApprovalPanel();
  } else {
    openApprovalPanel();
  }
}

function openApprovalPanel() {
  if (typeof closeAgentChat === 'function') closeAgentChat();
  const panel = document.getElementById('approval-float');
  panel.style.display = 'flex';
  loadPendingApprovals();
  setTimeout(() => panel.classList.add('open'), 10);
}

function closeApprovalPanel() {
  const panel = document.getElementById('approval-float');
  panel.classList.remove('open');
  setTimeout(() => { panel.style.display = 'none'; }, 200);
}

function updateApprovalBadge() {
  const pendingCount = (state.approvals || []).filter(a => a.status === 'pending').length;
  // Update all badge elements
  const badges = document.querySelectorAll('.approval-badge');
  badges.forEach(badge => {
    badge.textContent = pendingCount;
    if (pendingCount > 0) {
      badge.classList.add('pulse');
    } else {
      badge.classList.remove('pulse');
    }
  });
  // Update the seal badge in the nav
  if (typeof updateSealBadge === 'function') {
    updateSealBadge(pendingCount);
  }
}

function showApprovalForProject(projectId) {
  // Open panel and filter/highlight the approval for this project
  openApprovalPanel();
}

// ── Init ────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadPendingApprovals();
  startApprovalPolling();
});
