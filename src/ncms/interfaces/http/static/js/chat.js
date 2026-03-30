// ── NCMS Dashboard — Agent Chat Overlay ──────────────────────────────
// Click-to-chat overlay: each agent has its own persistent chat history.

// Strip LLM thinking tags from responses
function stripThinkTags(text) {
  if (!text) return text;
  // Remove <think>...</think> blocks (including content)
  return text.replace(/<think>[\s\S]*?<\/think>\s*/gi, '')
             .replace(/<\/?think>\s*/gi, '')
             .trim();
}

function simpleMarkdown(md) {
  let html = md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^\|(.+)\|$/gm, (match) => {
      const cells = match.split('|').filter(c => c.trim()).map(c => c.trim());
      if (cells.every(c => /^[-:]+$/.test(c))) return '';
      return '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>';
    })
    .replace(/^[*-] (.+)$/gm, '<li>$1</li>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');
  html = html.replace(/(<tr>[\s\S]*?<\/tr>)/g, '<table>$1</table>');
  html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  html = html.replace(/<\/ul><ul>/g, '');
  html = html.replace(/<\/table><table>/g, '');
  return '<p>' + html + '</p>';
}

// ── Open chat overlay for a specific agent ──
function openAgentChat(agentId) {
  // Don't open chat for human agent (it has the approvals badge)
  if (agentId === 'human') return;

  const agent = state.agents[agentId];
  if (!agent) return;

  // Close approval panel if open (avoid overlap)
  if (typeof closeApprovalPanel === 'function') closeApprovalPanel();

  state.activeChatAgent = agentId;

  // Ensure chat history exists
  if (!state.agentChats[agentId]) {
    state.agentChats[agentId] = [];
  }

  // Update header
  const nameEl = document.getElementById('chat-overlay-name');
  const domainsEl = document.getElementById('chat-overlay-domains');
  nameEl.textContent = agentId;
  domainsEl.innerHTML = (agent.domains || [])
    .map(d => `<span class="domain-tag">${escapeHtml(d)}</span>`).join('');

  // Render existing messages
  renderChatMessages(agentId);

  // Show overlay
  const overlay = document.getElementById('chat-overlay');
  overlay.style.display = 'flex';
  setTimeout(() => overlay.classList.add('open'), 10);

  // Focus input
  document.getElementById('chat-overlay-input').focus();
}

// ── Close chat overlay ──
function closeAgentChat() {
  const overlay = document.getElementById('chat-overlay');
  overlay.classList.remove('open');
  setTimeout(() => { overlay.style.display = 'none'; }, 200);
  state.activeChatAgent = null;
}

// ── Render messages for an agent ──
function renderChatMessages(agentId) {
  const container = document.getElementById('chat-overlay-messages');
  const messages = state.agentChats[agentId] || [];

  if (messages.length === 0) {
    container.innerHTML = '<div class="chat-empty">Start a conversation with this agent</div>';
    return;
  }

  container.innerHTML = messages.map(msg => {
    if (msg.type === 'user') {
      return `<div class="chat-msg user"><div class="chat-content">${escapeHtml(msg.content)}</div></div>`;
    } else if (msg.type === 'thinking') {
      return `<div class="chat-msg thinking"><div class="chat-content">${escapeHtml(msg.content)}</div></div>`;
    } else {
      return `<div class="chat-msg agent">
        <div class="chat-content">${simpleMarkdown(msg.content)}</div>
      </div>`;
    }
  }).join('');

  container.scrollTop = container.scrollHeight;
}

// ── Send message to the active agent ──
async function sendAgentMessage() {
  const agentId = state.activeChatAgent;
  if (!agentId) return;

  const input = document.getElementById('chat-overlay-input');
  const sendBtn = document.querySelector('.chat-overlay-send');
  const question = input.value.trim();
  if (!question) return;

  // Add user message
  state.agentChats[agentId].push({ content: question, type: 'user', timestamp: new Date().toISOString() });
  input.value = '';
  renderChatMessages(agentId);

  // Add thinking indicator with live progress
  const thinkingMsg = { content: '⏳ Sending to agent...', type: 'thinking', timestamp: new Date().toISOString() };
  state.agentChats[agentId].push(thinkingMsg);
  renderChatMessages(agentId);
  sendBtn.disabled = true;

  // Listen for SSE events from this agent while waiting for the response.
  // Updates the thinking bubble with live progress (tool calls, searches, etc.)
  const progressSteps = [];
  state._chatProgressAgent = agentId;
  state._chatProgressCallback = function(eventData) {
    let step = null;
    const d = eventData || {};
    const evtType = d.event_type || d.type || '';
    const evtAgent = d.source_agent || d.agent_id || d.from_agent || '';

    // Only show events from the agent we're chatting with
    if (evtAgent && evtAgent !== agentId) return;

    if (evtType === 'bus.ask' || evtType === 'ask') {
      step = '🔍 Asking: ' + (d.question || d.content || '').substring(0, 80) + '...';
    } else if (evtType === 'bus.announce' || evtType === 'announce') {
      const c = (d.content || '').substring(0, 60);
      if (c) step = '📢 ' + c + '...';
    } else if (evtType === 'tool_call' || evtType === 'function_call') {
      const tool = d.tool || d.function_name || d.name || '';
      if (tool === 'web_search') step = '🌐 Searching: ' + (d.query || d.input || '').substring(0, 60) + '...';
      else if (tool === 'create_prd') step = '📝 Creating PRD...';
      else if (tool === 'publish_document') step = '📄 Publishing document...';
      else if (tool === 'request_approval') step = '✋ Requesting approval...';
      else if (tool) step = '🔧 ' + tool + '...';
    } else if ((d.content || '').includes('Tavily') || (d.content || '').includes('web_search')) {
      step = '🌐 Web search in progress...';
    } else if ((d.content || '').includes('publish') || (d.content || '').includes('document')) {
      step = '📄 Publishing document...';
    }

    if (step && !progressSteps.includes(step)) {
      progressSteps.push(step);
      thinkingMsg.content = '⏳ Working...\n' + progressSteps.join('\n');
      if (state.activeChatAgent === agentId) renderChatMessages(agentId);
    }
  };

  try {
    const resp = await fetch(HUB_API + '/api/v1/agent/' + agentId + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_message: question }),
      signal: AbortSignal.timeout(300000),
    });

    // Remove thinking indicator and clear progress listener
    state._chatProgressAgent = null;
    state._chatProgressCallback = null;
    const msgs = state.agentChats[agentId];
    const thinkIdx = msgs.findLastIndex(m => m.type === 'thinking');
    if (thinkIdx >= 0) msgs.splice(thinkIdx, 1);

    const data = await resp.json();
    if (data.answered) {
      msgs.push({
        content: stripThinkTags(data.content) || '(empty response)',
        type: 'agent',
        agentName: data.from_agent || agentId,
        timestamp: new Date().toISOString(),
      });
    } else {
      msgs.push({
        content: data.error || 'Agent did not respond. It may be offline or timed out.',
        type: 'thinking',
        timestamp: new Date().toISOString(),
      });
    }
  } catch (err) {
    state._chatProgressAgent = null;
    state._chatProgressCallback = null;
    const msgs = state.agentChats[agentId];
    const thinkIdx = msgs.findLastIndex(m => m.type === 'thinking');
    if (thinkIdx >= 0) msgs.splice(thinkIdx, 1);
    msgs.push({ content: 'Error: ' + err.message, type: 'thinking', timestamp: new Date().toISOString() });
  } finally {
    sendBtn.disabled = false;
    if (state.activeChatAgent === agentId) {
      renderChatMessages(agentId);
      document.getElementById('chat-overlay-input').focus();
    }
  }
}

// Legacy compat — delegates use this
function addChatMessage(content, type, agentName) {
  // If a chat overlay is open, add to that agent's history
  const agentId = state.activeChatAgent;
  if (agentId) {
    if (!state.agentChats[agentId]) state.agentChats[agentId] = [];
    state.agentChats[agentId].push({ content, type, agentName, timestamp: new Date().toISOString() });
    renderChatMessages(agentId);
  }
}
