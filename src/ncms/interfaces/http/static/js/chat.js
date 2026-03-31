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
  if (!md) return '';

  // Escape HTML first
  let html = md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // Code blocks (preserve content, don't process inside)
  const codeBlocks = [];
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<pre><code class="lang-${lang || 'text'}">${code}</code></pre>`);
    return `%%CODEBLOCK_${idx}%%`;
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Headings
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Horizontal rules
  html = html.replace(/^---+$/gm, '<hr>');

  // Bold and italic
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Links
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  // Bare URLs (not already in links)
  html = html.replace(/(?<!")(?<!=)(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');

  // Blockquotes
  html = html.replace(/^&gt;\s*(.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/<\/blockquote>\n?<blockquote>/g, '<br>');

  // Tables — detect header row (first row before separator)
  html = html.replace(/^\|(.+)\|$/gm, (match) => {
    const cells = match.split('|').filter(c => c.trim()).map(c => c.trim());
    if (cells.every(c => /^[-:]+$/.test(c))) return '%%TABLE_SEP%%';
    return '%%TABLE_ROW%%' + cells.map(c => `%%TD%%${c}%%/TD%%`).join('') + '%%/TABLE_ROW%%';
  });

  // Process table rows — first row before separator becomes header
  html = html.replace(
    /%%TABLE_ROW%%([\s\S]*?)%%\/TABLE_ROW%%\s*%%TABLE_SEP%%/g,
    (_, cells) => {
      const headerCells = cells.replace(/%%TD%%/g, '<th>').replace(/%%\/TD%%/g, '</th>');
      return '<thead><tr>' + headerCells + '</tr></thead><tbody>';
    }
  );
  html = html.replace(/%%TABLE_ROW%%([\s\S]*?)%%\/TABLE_ROW%%/g, (_, cells) => {
    const tdCells = cells.replace(/%%TD%%/g, '<td>').replace(/%%\/TD%%/g, '</td>');
    return '<tr>' + tdCells + '</tr>';
  });

  // Wrap consecutive table rows
  html = html.replace(/(<thead>[\s\S]*?<\/tbody>(?:\s*<tr>[\s\S]*?<\/tr>)*)/g, '<table>$1</tbody></table>');
  // Clean up orphan table rows
  html = html.replace(/(<tr>(?:[\s\S]*?<\/tr>\s*)+)/g, (match) => {
    if (match.includes('<table>')) return match;
    return '<table><tbody>' + match + '</tbody></table>';
  });

  // Numbered lists (1. 2. 3.)
  html = html.replace(/^\d+\.\s+(.+)$/gm, '<oli>$1</oli>');
  html = html.replace(/(<oli>[\s\S]*?<\/oli>)/g, (match) => {
    const items = match.replace(/<\/?oli>/g, '').split('</oli><oli>');
    return '<ol>' + items.map(i => `<li>${i.trim()}</li>`).join('') + '</ol>';
  });

  // Unordered lists
  html = html.replace(/^[*-] (.+)$/gm, '<uli>$1</uli>');
  html = html.replace(/(<uli>[\s\S]*?<\/uli>)/g, (match) => {
    const items = match.replace(/<\/?uli>/g, '').split('</uli><uli>');
    return '<ul>' + items.map(i => `<li>${i.trim()}</li>`).join('') + '</ul>';
  });

  // Paragraphs
  html = html.replace(/\n\n+/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');

  // Clean up empty paragraphs and adjacent lists
  html = html.replace(/<\/ul><br><ul>/g, '');
  html = html.replace(/<\/ol><br><ol>/g, '');
  html = html.replace(/<p><\/p>/g, '');
  html = html.replace(/<p>(<h[1-4]>)/g, '$1');
  html = html.replace(/(<\/h[1-4]>)<\/p>/g, '$1');
  html = html.replace(/<p>(<hr>)<\/p>/g, '$1');
  html = html.replace(/<p>(<table>)/g, '$1');
  html = html.replace(/(<\/table>)<\/p>/g, '$1');
  html = html.replace(/<p>(<ul>|<ol>)/g, '$1');
  html = html.replace(/(<\/ul>|<\/ol>)<\/p>/g, '$1');
  html = html.replace(/<p>(<blockquote>)/g, '$1');
  html = html.replace(/(<\/blockquote>)<\/p>/g, '$1');

  // Restore code blocks
  for (let i = 0; i < codeBlocks.length; i++) {
    html = html.replace(`%%CODEBLOCK_${i}%%`, codeBlocks[i]);
  }

  // Remove HTML comments (project_id, version markers)
  html = html.replace(/&lt;!--[\s\S]*?--&gt;/g, '');

  return '<div class="md-rendered"><p>' + html + '</p></div>';
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
