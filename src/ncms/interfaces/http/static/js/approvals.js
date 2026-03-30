// ── NCMS Dashboard — Approval Queue ──────────────────────────────────
// Human-in-the-loop approval panel for builder plan review.
// Supports: Approve, Reject, Suggest Changes, Delegate to Agent.

function handleApprovalAnnouncement(data) {
  const content = data.content || '';
  if (!content.includes('AWAITING_APPROVAL')) return;

  const planIdMatch = content.match(/plan_id=(\w+)/);
  const titleMatch = content.match(/Title:\s*(.+)/);
  const fromMatch = content.match(/From:\s*(.+)/);
  const submittedMatch = content.match(/Submitted:\s*(.+)/);
  const planBodyMatch = content.match(/---\n([\s\S]*)/);

  const approval = {
    plan_id: planIdMatch ? planIdMatch[1] : 'unknown',
    title: titleMatch ? titleMatch[1].trim() : 'Untitled Plan',
    from_agent: fromMatch ? fromMatch[1].trim() : data.from_agent || 'unknown',
    timestamp: submittedMatch ? submittedMatch[1].trim() : new Date().toISOString(),
    plan_content: planBodyMatch ? planBodyMatch[1].trim() : content,
    status: 'pending',
    review_count: 0,
    expanded: true,  // auto-expand new approvals
  };

  const existing = state.approvals.find(a => a.plan_id === approval.plan_id);
  if (existing) {
    // Re-submission after changes requested
    existing.plan_content = approval.plan_content;
    existing.status = 'pending';
    existing.timestamp = approval.timestamp;
    existing.expanded = true;
    renderApprovalQueue();
    return;
  }

  state.approvals.unshift(approval);
  renderApprovalQueue();

  // Open the floating approval panel and re-render
  openApprovalPanel();
  updateApprovalBadge();
}

async function loadPendingApprovals() {
  try {
    const resp = await fetch(HUB_API + '/api/v1/memories/search?q=AWAITING_APPROVAL&domain=human-approval&limit=20');
    if (!resp.ok) return;
    const data = await resp.json();
    const results = data.results || [];

    for (const r of results) {
      const memory = r.memory || r;
      const content = memory.content || '';
      if (!content.includes('AWAITING_APPROVAL')) continue;

      const planIdMatch = content.match(/plan_id=(\w+)/);
      const titleMatch = content.match(/Title:\s*(.+)/);
      const fromMatch = content.match(/From:\s*(.+)/);
      const submittedMatch = content.match(/Submitted:\s*(.+)/);
      const planBodyMatch = content.match(/---\n([\s\S]*)/);

      const planId = planIdMatch ? planIdMatch[1] : null;
      if (!planId) continue;
      if (state.approvals.some(a => a.plan_id === planId)) continue;

      state.approvals.push({
        plan_id: planId,
        title: titleMatch ? titleMatch[1].trim() : 'Untitled Plan',
        from_agent: fromMatch ? fromMatch[1].trim() : memory.source_agent || 'unknown',
        timestamp: submittedMatch ? submittedMatch[1].trim() : memory.created_at || '',
        plan_content: planBodyMatch ? planBodyMatch[1].trim() : content,
        status: 'pending',
        review_count: 0,
        expanded: true,
      });
    }

    renderApprovalQueue();
  } catch (e) {
    console.debug('Failed to load pending approvals:', e);
  }
}

function toggleApprovalContent(planId) {
  const approval = state.approvals.find(a => a.plan_id === planId);
  if (approval) {
    approval.expanded = !approval.expanded;
    renderApprovalQueue();
  }
}

function renderApprovalQueue() {
  const container = document.getElementById('approval-queue');
  if (!container) return;

  if (state.approvals.length === 0) {
    container.innerHTML = '<div class="approval-empty">No pending approvals. Builder plans will appear here when submitted.</div>';
    return;
  }

  let html = '';
  for (const approval of state.approvals) {
    const statusClass = approval.status.split(' ')[0]; // handle "delegated to X"
    const isActionable = approval.status === 'pending';
    const isExpanded = approval.expanded !== false;

    // Build delegate options: online agents except the submitter and human
    const agentOptions = Object.values(state.agents)
      .filter(a => a.status === 'online' && a.agent_id !== 'human' && a.agent_id !== approval.from_agent)
      .map(a => `<option value="${escapeHtml(a.agent_id)}">${escapeHtml(a.agent_id)}</option>`)
      .join('');

    const contentHtml = simpleMarkdown(approval.plan_content);
    const contentLen = approval.plan_content.length;
    const toggleLabel = isExpanded ? 'Collapse' : `Expand Plan (${contentLen} chars)`;

    html += `<div class="approval-card ${statusClass}">
      <div class="approval-header">
        <div style="flex:1">
          <div class="approval-title">${escapeHtml(approval.title)}</div>
          <div class="approval-meta">
            <span class="approval-plan-id">${escapeHtml(approval.plan_id)}</span>
            <span>from ${escapeHtml(approval.from_agent)}</span>
            <span>${formatTimeFull(approval.timestamp)}</span>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <button class="approval-btn-toggle" onclick="toggleApprovalContent('${approval.plan_id}')">${toggleLabel}</button>
          <span class="approval-status-badge ${statusClass}">${escapeHtml(approval.status)}</span>
          ${approval.review_count > 0 ? `<span class="approval-review-count">Round ${approval.review_count + 1}</span>` : ''}
          ${approval.review_count >= 3 ? `<span class="approval-escalation-warning">Escalation recommended</span>` : ''}
        </div>
      </div>`;

    if (isExpanded) {
      html += `<div class="approval-content">${contentHtml}</div>`;
    }

    if (isActionable) {
      html += `<div class="approval-actions-row">
        <button class="approval-btn approve" onclick="submitApprovalAction('${approval.plan_id}', 'APPROVED')">Approve</button>
        <button class="approval-btn reject" onclick="submitApprovalAction('${approval.plan_id}', 'REJECTED')">Reject</button>
        <button class="approval-btn suggest" onclick="submitApprovalAction('${approval.plan_id}', 'CHANGES_REQUESTED')">Suggest Changes</button>`;

      if (agentOptions) {
        html += `
        <div class="approval-actions-divider"></div>
        <select class="approval-delegate-select" id="delegate-${approval.plan_id}">
          ${agentOptions}
        </select>
        <button class="approval-btn delegate" onclick="delegateApproval('${approval.plan_id}')">Delegate Review</button>`;
      }

      html += `</div>
      <div style="margin-top:8px">
        <input type="text" class="approval-comment" id="comment-${approval.plan_id}"
               placeholder="Optional comment..." style="width:100%">
      </div>`;
    }

    html += '</div>';
  }

  container.innerHTML = html;

  // Update badge count on human agent card
  if (typeof updateApprovalBadge === 'function') updateApprovalBadge();
}

async function submitApprovalAction(planId, action) {
  const commentInput = document.getElementById('comment-' + planId);
  const comment = commentInput ? commentInput.value.trim() : '';

  const content = `${action}: plan_id=${planId}${comment ? ' ' + comment : ''}`;

  try {
    await fetch(HUB_API + '/api/v1/bus/announce', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: content,
        domains: ['approval-response'],
        from_agent: 'human',
      }),
    });

    const approval = state.approvals.find(a => a.plan_id === planId);
    if (approval) {
      if (action === 'APPROVED') {
        approval.status = 'approved';
      } else if (action === 'CHANGES_REQUESTED') {
        approval.status = 'changes_requested';
        approval.review_count = (approval.review_count || 0) + 1;
      } else {
        approval.status = 'rejected';
        approval.review_count = (approval.review_count || 0) + 1;
      }
    }
    renderApprovalQueue();
  } catch (err) {
    console.error('Failed to submit approval:', err);
    addChatMessage('Error submitting approval: ' + err.message, 'thinking');
  }
}

async function delegateApproval(planId) {
  const selectEl = document.getElementById('delegate-' + planId);
  if (!selectEl) return;
  const agentId = selectEl.value;
  if (!agentId) return;

  const approval = state.approvals.find(a => a.plan_id === planId);
  if (!approval) return;

  const question = `Please review this plan and provide your feedback. State whether you recommend approval, rejection, or changes.\n\nTitle: ${approval.title}\nPlan ID: ${approval.plan_id}\n\n${approval.plan_content}`;

  approval.status = 'delegated to ' + agentId;
  renderApprovalQueue();

  try {
    const resp = await fetch(HUB_API + '/api/v1/agent/' + agentId + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_message: question }),
      signal: AbortSignal.timeout(300000),
    });

    const data = await resp.json();

    if (data.answered) {
      addChatMessage(
        data.content || '(empty response)',
        'agent',
        data.from_agent || agentId,
      );
      approval.status = 'reviewed by ' + agentId;
      renderApprovalQueue();

      // Open chat with the reviewing agent to show their feedback
      openAgentChat(agentId);
    } else {
      approval.status = 'pending';
      renderApprovalQueue();
      addChatMessage(
        `Delegation to ${agentId} failed: ${data.error || 'Agent did not respond'}`,
        'thinking',
      );
    }
  } catch (err) {
    approval.status = 'pending';
    renderApprovalQueue();
    addChatMessage('Delegation error: ' + err.message, 'thinking');
  }
}

// ── Floating Panel Controls ──────────────────────────────────────────

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
  // Close chat overlay if open (avoid overlap)
  if (typeof closeAgentChat === 'function') closeAgentChat();
  const panel = document.getElementById('approval-float');
  panel.style.display = 'flex';
  renderApprovalQueue();
  setTimeout(() => panel.classList.add('open'), 10);
}

function closeApprovalPanel() {
  const panel = document.getElementById('approval-float');
  panel.classList.remove('open');
  setTimeout(() => { panel.style.display = 'none'; }, 200);
}

function updateApprovalBadge() {
  const pendingCount = state.approvals.filter(a => a.status === 'pending').length;
  const badges = document.querySelectorAll('.approval-badge');
  badges.forEach(badge => {
    badge.textContent = pendingCount;
    if (pendingCount > 0) {
      badge.classList.add('pulse');
    } else {
      badge.classList.remove('pulse');
    }
  });
}

function showApprovalForPlan(planId) {
  openApprovalPanel();
  // Expand the matching approval
  const approval = state.approvals.find(a => a.plan_id === planId);
  if (approval) {
    approval.expanded = true;
    renderApprovalQueue();
  }
}
