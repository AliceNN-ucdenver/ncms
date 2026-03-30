// ── NCMS Dashboard — Agent Cards & Activity ──────────────────────────
// Agent rendering, activity feeds, connector animations.

// ── Event Handling ───────────────────────────────────────────────────
function handleEvent(event) {
  state.eventCount++;
  state.eventStore[event.id] = event;
  const d = event.data || {};

  // Forward to chat progress indicator if a chat is waiting for a response
  if (state._chatProgressCallback) {
    try {
      state._chatProgressCallback({
        event_type: event.type,
        source_agent: event.agent_id || d.source_agent || d.from_agent || '',
        content: d.content || d.question || '',
        tool: d.tool || d.function_name || '',
        query: d.query || '',
        input: d.input || '',
        name: d.name || '',
      });
    } catch (_) { /* ignore progress errors */ }
  }

  // Auto-refresh documents when a new one is published
  if (event.type === 'document.published') {
    if (typeof loadDocuments === 'function') loadDocuments();
  }

  // Agent lifecycle events
  if (event.type.startsWith('agent.')) {
    handleAgentEvent(event);
  }

  // Index asks and responses for conversation threading
  if (event.type === 'bus.ask' && d.ask_id) {
    state.askIndex[d.ask_id] = { askEvent: event, responses: [] };
  }
  if ((event.type === 'bus.response' || event.type === 'bus.surrogate') && d.ask_id) {
    const thread = state.askIndex[d.ask_id];
    if (thread) {
      thread.responses.push(event);
      const askingAgent = thread.askEvent.agent_id;
      if (askingAgent && askingAgent !== event.agent_id) {
        const replyEntry = Object.assign({}, event, { _isReply: true, _replyFrom: event.agent_id });
        state.eventStore[event.id + '_reply'] = replyEntry;
        addAgentActivity(askingAgent, replyEntry);
        animateFlow(askingAgent, 'receive');
      }
    }
  }

  // Route events to agent activity lists
  if (event.agent_id) {
    addAgentActivity(event.agent_id, event);
  }

  // Also add to target/recipient agents
  if (event.type === 'bus.ask' && d.targets) {
    d.targets.forEach(t => {
      if (t !== event.agent_id) {
        addAgentActivity(t, event);
        animateFlow(t, 'receive');
      }
    });
  }
  if (event.type === 'bus.announce' && d.recipients) {
    d.recipients.forEach(r => {
      if (r !== event.agent_id) {
        addAgentActivity(r, event);
        animateFlow(r, 'receive');
      }
    });
  }

  // Animate SEND on the originating agent's connector
  if (event.agent_id && !event._isReply) {
    const isSending = ['bus.ask', 'bus.response', 'bus.announce', 'bus.surrogate'].includes(event.type);
    animateFlow(event.agent_id, isSending ? 'send' : 'neutral');
  }

  // Episode events
  if (event.type === 'episode.created' && d.episode_id) {
    state.episodes[d.episode_id] = {
      title: d.title || '(untitled)',
      status: 'open',
      member_count: 0,
      created_at: event.timestamp,
    };
  }
  if (event.type === 'episode.assigned' && d.episode_id) {
    const ep = state.episodes[d.episode_id];
    if (ep) ep.member_count = (ep.member_count || 0) + 1;
  }
  if (event.type === 'episode.closed' && d.episode_id) {
    const ep = state.episodes[d.episode_id];
    if (ep) {
      ep.status = 'closed';
      ep.member_count = d.member_count || ep.member_count;
    }
  }

  // Admission scoring events
  if (event.type === 'admission.scored') {
    state.admissionFeed.unshift({
      score: d.score || 0,
      route: d.route || 'unknown',
      features: d.features || {},
      agent_id: event.agent_id,
      timestamp: event.timestamp,
      content: d.content || '',
      id: event.id,
    });
    if (state.admissionFeed.length > 30) state.admissionFeed.pop();
    if (!_replaying) {
      document.getElementById('admission-panel').style.display = '';
      renderAdmissionFeed();
    }
  }

  // Check for approval-related announcements
  if (event.type === 'bus.announce' && d.content && d.content.includes('AWAITING_APPROVAL')) {
    if (typeof handleApprovalAnnouncement === 'function') {
      handleApprovalAnnouncement(d);
    }
  }

  if (!_replaying) updateStats();
}

function handleAgentEvent(event) {
  const agentId = event.agent_id;
  if (!agentId) return;

  if (event.type === 'agent.registered') {
    state.agents[agentId] = {
      agent_id: agentId,
      domains: event.data.domains || [],
      status: 'online',
    };
  } else if (event.type === 'agent.deregistered') {
    if (state.agents[agentId]) {
      state.agents[agentId].status = 'offline';
    }
  } else if (event.type === 'agent.status') {
    if (state.agents[agentId]) {
      state.agents[agentId].status = event.data.status;
    }
  }

  if (!_replaying) {
    renderAgents();
    updateChatTargets();
  }
}

// ── Agent Activity ───────────────────────────────────────────────────
function addAgentActivity(agentId, event) {
  if (!state.agentActivities[agentId]) {
    state.agentActivities[agentId] = [];
  }

  const existing = state.agentActivities[agentId];
  if (existing.length > 0 && existing[0].id === event.id) return;

  existing.unshift(event);
  if (existing.length > MAX_ACTIVITIES) {
    existing.pop();
  }
  if (!_replaying) renderAgentActivity(agentId);
}

function renderAgentActivity(agentId) {
  const list = document.getElementById(`activity-${agentId}`);
  if (!list) return;

  const activities = state.agentActivities[agentId] || [];
  if (activities.length === 0) {
    list.innerHTML = '<div class="no-activity">No activity yet</div>';
    return;
  }

  list.innerHTML = activities.map(e => activityItemHTML(e)).join('');
}

function activityItemHTML(event) {
  const d = event.data || {};
  const time = formatTime(event.timestamp);
  const agentId = event.agent_id || d.source_agent || '';
  const traceLink = agentId
    ? `<a class="trace-link" href="http://localhost:6006/projects/ncms-${encodeURIComponent(agentId.replace(/_/g, '-'))}/traces" target="_blank" rel="noopener" title="View traces in Phoenix" onclick="event.stopPropagation()">&#x1F50D;</a>`
    : '';
  let icon, text, fromLabel = '';

  if (event._isReply) {
    const replyFrom = event._replyFrom || event.agent_id;
    const isSurrogate = event.type === 'bus.surrogate';
    icon = isSurrogate
      ? '<span class="activity-icon icon-surrogate">&#x21A9;</span>'
      : '<span class="activity-icon icon-reply">&#x21A9;</span>';
    text = d.answer || (isSurrogate ? `Surrogate (${(d.confidence * 100).toFixed(0)}%)` : 'Response');
    const fromClass = isSurrogate ? 'activity-from from-snapshot' : 'activity-from';
    const snapshotTag = isSurrogate ? '<span class="snapshot-badge">snapshot</span>' : '';
    fromLabel = `<div class="${fromClass}">${escapeHtml(replyFrom)}${snapshotTag}</div>`;
    const shortText = text.length > 45 ? text.slice(0, 42) + '...' : text;
    const realId = event.id.replace(/_reply$/, '');
    return `<div class="activity-item" onclick="showDetail('${realId}')">
      ${icon}
      <div style="flex:1;min-width:0;overflow:hidden">
        ${fromLabel}
        <span class="activity-text" title="${escapeHtml(text)}">${escapeHtml(shortText)}</span>
      </div>
      <span class="activity-time">${time}${traceLink}</span>
    </div>`;
  }

  switch (event.type) {
    case 'bus.ask':
      icon = '<span class="activity-icon icon-ask">?</span>';
      text = d.question || 'Asked a question';
      break;
    case 'bus.response':
      icon = '<span class="activity-icon icon-response">&#x2713;</span>';
      text = d.answer || `Responded (${d.source_mode}, ${(d.confidence * 100).toFixed(0)}%)`;
      break;
    case 'bus.announce':
      icon = '<span class="activity-icon icon-announce">!</span>';
      text = d.content || `Announced: ${d.event}`;
      break;
    case 'bus.surrogate':
      icon = '<span class="activity-icon icon-surrogate">&#x2601;</span>';
      text = d.answer || `Surrogate (${(d.confidence * 100).toFixed(0)}%)`;
      const surShort = text.length > 40 ? text.slice(0, 37) + '...' : text;
      return `<div class="activity-item" onclick="showDetail('${event.id}')">
        ${icon}
        <div style="flex:1;min-width:0;overflow:hidden">
          <span class="activity-text" title="${escapeHtml(text)}">${escapeHtml(surShort)}<span class="snapshot-badge">snapshot</span></span>
        </div>
        <span class="activity-time">${time}${traceLink}</span>
      </div>`;
    case 'memory.stored':
      icon = '<span class="activity-icon icon-memory">&#x2B22;</span>';
      text = d.content || 'Memory stored';
      break;
    case 'memory.searched':
      icon = '<span class="activity-icon icon-search">&#x2315;</span>';
      text = `Search: "${d.query}" (${d.result_count})`;
      break;
    case 'agent.registered':
      icon = '<span class="activity-icon icon-agent">+</span>';
      text = 'Registered';
      break;
    case 'agent.deregistered':
      icon = '<span class="activity-icon icon-agent">&minus;</span>';
      text = 'Deregistered';
      break;
    case 'agent.status':
      icon = '<span class="activity-icon icon-agent">&#x25CF;</span>';
      text = `Status: ${d.status}`;
      break;
    default:
      icon = '<span class="activity-icon icon-agent">&#x2022;</span>';
      text = event.type;
  }

  const shortText = text.length > 50 ? text.slice(0, 47) + '...' : text;

  return `<div class="activity-item" onclick="showDetail('${event.id}')">
    ${icon}
    <span class="activity-text" title="${escapeHtml(text)}">${escapeHtml(shortText)}</span>
    <span class="activity-time">${time}${traceLink}</span>
  </div>`;
}

// ── Agent Rendering ──────────────────────────────────────────────────
function renderAgents() {
  const allAgents = Object.values(state.agents);
  const panel = document.getElementById('agents-area');

  // Filter human out of the main grid — it goes in the header
  const agents = allAgents.filter(a => a.agent_id !== 'human');
  const human = allAgents.find(a => a.agent_id === 'human');

  if (agents.length === 0 && !human) {
    panel.innerHTML = '<div class="waiting-message">Waiting for agents to connect...</div>';
  } else if (agents.length === 0) {
    panel.innerHTML = '<div class="waiting-message">Waiting for agents to connect...</div>';
  } else {
    panel.innerHTML = agents.map(a => agentColumnHTML(a)).join('');
  }

  // Render human badge in header
  renderHumanBadge(human);
}

function renderHumanBadge(human) {
  let badge = document.getElementById('human-badge');
  if (!badge) {
    // Create badge container in the header stats bar
    const statsBar = document.getElementById('stats-bar');
    if (!statsBar) return;
    badge = document.createElement('div');
    badge.id = 'human-badge';
    badge.className = 'human-header-badge';
    statsBar.insertBefore(badge, statsBar.firstChild);
  }

  const isOnline = human && human.status === 'online';
  const pendingCount = state.approvals
    ? state.approvals.filter(a => a.status === 'pending').length
    : 0;
  const pendingClass = pendingCount > 0 ? 'pulse' : '';

  badge.innerHTML = `
    <span class="human-dot ${isOnline ? 'online' : 'offline'}"></span>
    <span class="human-label">Human</span>
    <span class="human-approval-count ${pendingClass}" onclick="toggleApprovalPanel(event)" title="View approvals">
      ${pendingCount > 0 ? pendingCount + ' pending' : 'No approvals'}
    </span>
  `;
}

function agentColumnHTML(agent) {
  const activities = state.agentActivities[agent.agent_id] || [];
  const activityHTML = activities.length > 0
    ? activities.map(e => activityItemHTML(e)).join('')
    : '<div class="no-activity">No activity yet</div>';

  const isHuman = agent.agent_id === 'human';

  // Approval badge for human agent
  const pendingCount = isHuman
    ? state.approvals.filter(a => a.status === 'pending').length
    : 0;
  const badgeHTML = pendingCount > 0
    ? `<span class="approval-badge pulse" onclick="toggleApprovalPanel(event)" title="View approvals">${pendingCount}</span>`
    : (isHuman ? `<span class="approval-badge" onclick="toggleApprovalPanel(event)" title="View approvals">0</span>` : '');

  // Chat icon for non-human agents
  const chatIcon = !isHuman
    ? `<button class="agent-chat-btn" onclick="event.stopPropagation();openAgentChat('${agent.agent_id}')" title="Chat with ${agent.agent_id}">&#x1F4AC;</button>`
    : '';

  return `<div class="agent-column">
    <div class="agent-node" id="agent-${agent.agent_id}">
      <div class="agent-header">
        <span class="agent-name">${agent.agent_id}</span>
        <div style="display:flex;gap:6px;align-items:center">
          ${chatIcon}
          ${badgeHTML}
          <span class="status-badge status-${agent.status}">${agent.status}</span>
        </div>
      </div>
      <div class="agent-domains">
        ${(agent.domains || []).map(d => `<span class="domain-tag">${d}</span>`).join('')}
      </div>
      <div class="activity-list" id="activity-${agent.agent_id}">
        ${activityHTML}
      </div>
    </div>
    <div class="agent-connector" id="connector-${agent.agent_id}">
      <div class="flow-dot" id="flow-dot-${agent.agent_id}"></div>
    </div>
  </div>`;
}

// ── Connector Animation ──────────────────────────────────────────────
function animateFlow(agentId, direction) {
  if (!agentId || _replaying) return;
  const connector = document.getElementById(`connector-${agentId}`);
  const dot = document.getElementById(`flow-dot-${agentId}`);
  if (!connector) return;

  connector.classList.remove('flow-send', 'flow-receive');

  if (direction === 'send') {
    connector.classList.add('flow-send');
    if (dot) {
      dot.className = 'flow-dot sending';
      void dot.offsetWidth;
      dot.classList.add('animate-send');
    }
  } else if (direction === 'receive') {
    connector.classList.add('flow-receive');
    if (dot) {
      dot.className = 'flow-dot receiving';
      void dot.offsetWidth;
      dot.classList.add('animate-receive');
    }
  }

  setTimeout(() => {
    connector.classList.remove('flow-send', 'flow-receive');
    if (dot) dot.className = 'flow-dot';
  }, 600);
}
