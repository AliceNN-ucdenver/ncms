// ── NCMS Dashboard — Events, Modal, Pipeline, Admission ─────────────
// Event detail modal, conversation threads, bus state reconstruction,
// admission scoring panel, pipeline observability.

// ── Detail Modal ─────────────────────────────────────────────────────
function showDetail(eventId) {
  const event = state.eventStore[eventId];
  if (!event) return;

  const d = event.data || {};
  let fieldsHTML = '';

  if (event.type === 'bus.ask') {
    fieldsHTML += modalField('Question', d.question || '(empty)');
    fieldsHTML += modalField('Domains', tagsHTML(d.domains));
    fieldsHTML += modalField('Routed To', tagsHTML(d.targets));
    fieldsHTML += buildThreadHTML(d.ask_id, event);
  } else if (event.type === 'bus.response') {
    fieldsHTML += modalField('Answer', d.answer || '(not available)');
    fieldsHTML += modalField('Source Mode', d.source_mode);
    fieldsHTML += modalField('Confidence', confidenceHTML(d.confidence));
    fieldsHTML += buildThreadHTML(d.ask_id, event);
  } else if (event.type === 'bus.announce') {
    fieldsHTML += modalField('Event', d.event);
    fieldsHTML += modalField('Content', d.content || '(not available)');
    fieldsHTML += modalField('Severity', d.severity);
    fieldsHTML += modalField('Domains', tagsHTML(d.domains));
    fieldsHTML += modalField('Recipients', tagsHTML(d.recipients));
  } else if (event.type === 'bus.surrogate') {
    fieldsHTML += modalField('Answer', d.answer || '(not available)');
    fieldsHTML += modalField('Confidence', confidenceHTML(d.confidence));
    fieldsHTML += modalField('Snapshot Age', formatAge(d.snapshot_age_seconds));
    fieldsHTML += buildThreadHTML(d.ask_id, event);
  } else if (event.type === 'memory.stored') {
    fieldsHTML += modalField('Content', d.content || '');
    fieldsHTML += modalField('Type', d.type);
    fieldsHTML += modalField('Domains', tagsHTML(d.domains));
    fieldsHTML += modalField('Entities Extracted', String(d.entity_count || 0));
  } else if (event.type === 'memory.searched') {
    fieldsHTML += modalField('Query', d.query || '');
    fieldsHTML += modalField('Results', String(d.result_count));
    if (d.top_score != null) {
      fieldsHTML += modalField('Top Score', d.top_score.toFixed(3));
    }
  } else if (event.type === 'admission.scored') {
    fieldsHTML += modalField('Score', d.score != null ? d.score.toFixed(3) : 'N/A');
    fieldsHTML += modalField('Route', `<span class="admission-route route-${d.route || 'unknown'}">${(d.route || 'unknown').replace(/_/g, ' ')}</span>`);
    if (d.features) {
      let featHTML = '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:4px;font-size:11px">';
      Object.entries(d.features).forEach(([k, v]) => {
        featHTML += `<span><span style="color:var(--text-muted)">${k}:</span> ${typeof v === 'number' ? v.toFixed(3) : v}</span>`;
      });
      featHTML += '</div>';
      fieldsHTML += modalField('Features', featHTML);
    }
  } else if (event.type === 'episode.created') {
    fieldsHTML += modalField('Episode ID', d.episode_id || '');
    fieldsHTML += modalField('Title', d.title || '(untitled)');
    if (d.anchor_type) fieldsHTML += modalField('Anchor Type', d.anchor_type);
  } else if (event.type === 'episode.assigned') {
    fieldsHTML += modalField('Episode ID', d.episode_id || '');
    fieldsHTML += modalField('Fragment ID', d.fragment_id || '');
    fieldsHTML += modalField('Match Score', d.match_score != null ? d.match_score.toFixed(3) : 'N/A');
  } else if (event.type === 'episode.closed') {
    fieldsHTML += modalField('Episode ID', d.episode_id || '');
    fieldsHTML += modalField('Reason', d.reason || '');
    fieldsHTML += modalField('Members', String(d.member_count || 0));
  } else {
    fieldsHTML += modalField('Data', '<pre>' + escapeHtml(JSON.stringify(d, null, 2)) + '</pre>');
  }

  let sourceBanner = '';
  if (event.type === 'bus.response') {
    sourceBanner = '<div class="source-banner source-live">&#x2713; Live Agent Response</div>';
  } else if (event.type === 'bus.surrogate') {
    const age = d.snapshot_age_seconds ? ` &middot; snapshot ${formatAge(d.snapshot_age_seconds)} old` : '';
    sourceBanner = `<div class="source-banner source-snapshot">&#x2601; Snapshot Response${age}</div>`;
  }

  const busStateHTML = renderBusStateSection(event);

  const modal = document.getElementById('modal-root');
  modal.innerHTML = `
    <div class="modal-overlay" onclick="closeModal(event)">
      <div class="modal-content" onclick="event.stopPropagation()">
        <button class="modal-close" onclick="closeModal()">&times;</button>
        <div class="modal-type">${event.type}</div>
        <div class="modal-agent">${event.agent_id || 'System'} &middot; ${formatTimeFull(event.timestamp)}</div>
        ${sourceBanner}
        ${fieldsHTML}
        ${busStateHTML}
      </div>
    </div>`;
}

function closeModal(e) {
  if (e && e.target && !e.target.classList.contains('modal-overlay')) return;
  document.getElementById('modal-root').innerHTML = '';
}

function modalField(label, value) {
  return `<div class="modal-field">
    <div class="modal-field-label">${label}</div>
    <div class="modal-field-value">${value}</div>
  </div>`;
}

function tagsHTML(items) {
  if (!items || items.length === 0) return '<span style="color: var(--text-muted)">none</span>';
  return '<div class="modal-tags">' +
    items.map(t => `<span class="modal-tag">${escapeHtml(t)}</span>`).join('') +
    '</div>';
}

function confidenceHTML(confidence) {
  if (confidence == null) return 'N/A';
  const pct = (confidence * 100).toFixed(1);
  const cls = confidence >= 0.7 ? 'confidence-high' :
              confidence >= 0.4 ? 'confidence-med' : 'confidence-low';
  return `<span class="modal-confidence ${cls}">${pct}%</span>`;
}

// ── Conversation Thread (for modal) ──────────────────────────────────
function buildThreadHTML(askId, currentEvent) {
  if (!askId) return '';
  const thread = state.askIndex[askId];
  if (!thread) return '';

  let html = '<div class="thread-section">';
  html += '<div class="thread-label">Conversation</div>';

  const askEvt = thread.askEvent;
  const askData = askEvt.data || {};
  const isCurrentAsk = (currentEvent.id === askEvt.id);
  html += `<div class="thread-message" onclick="showDetail('${askEvt.id}')" style="${isCurrentAsk ? 'border-color: var(--accent-blue);' : ''}">
    <div class="thread-avatar ask">?</div>
    <div class="thread-body">
      <div class="thread-agent">
        ${escapeHtml(askEvt.agent_id || 'unknown')}
        <span class="thread-agent-badge badge-asker">asked</span>
      </div>
      <div class="thread-text">${escapeHtml(askData.question || '(empty)')}</div>
      <div class="thread-meta">${formatTimeFull(askEvt.timestamp)}${askData.domains ? ' &middot; ' + askData.domains.join(', ') : ''}</div>
    </div>
  </div>`;

  for (const resp of thread.responses) {
    const rd = resp.data || {};
    const isSurrogate = resp.type === 'bus.surrogate';
    const isCurrent = (currentEvent.id === resp.id);
    const avatarClass = isSurrogate ? 'surrogate' : 'response';
    const badgeClass = isSurrogate ? 'badge-surrogate' : 'badge-responder';
    const badgeText = isSurrogate ? 'surrogate' : 'responded';
    const confPct = rd.confidence != null ? ` &middot; ${(rd.confidence * 100).toFixed(0)}%` : '';

    html += `<div class="thread-message" onclick="showDetail('${resp.id}')" style="${isCurrent ? 'border-color: var(--accent-green);' : ''}">
      <div class="thread-avatar ${avatarClass}">${isSurrogate ? '&#x2601;' : '&#x2713;'}</div>
      <div class="thread-body">
        <div class="thread-agent">
          ${escapeHtml(resp.agent_id || 'unknown')}
          <span class="thread-agent-badge ${badgeClass}">${badgeText}</span>
        </div>
        <div class="thread-text">${escapeHtml(rd.answer || '(no answer text)')}</div>
        <div class="thread-meta">${formatTimeFull(resp.timestamp)}${confPct}${isSurrogate && rd.snapshot_age_seconds ? ' &middot; snapshot ' + formatAge(rd.snapshot_age_seconds) + ' old' : ''}</div>
      </div>
    </div>`;
  }

  if (thread.responses.length === 0) {
    html += '<div style="color: var(--text-muted); font-size: 12px; font-style: italic; padding: 8px 0;">Awaiting response...</div>';
  }

  html += '</div>';
  return html;
}

// ── Bus State Reconstruction ─────────────────────────────────────────
function reconstructBusStateAt(targetTimestamp) {
  const agentEvents = Object.values(state.eventStore)
    .filter(e => e.type && e.type.startsWith('agent.') && e.timestamp <= targetTimestamp)
    .sort((a, b) => a.timestamp < b.timestamp ? -1 : 1);

  const agents = {};
  agentEvents.forEach(e => {
    const id = e.agent_id;
    if (!id) return;
    const d = e.data || {};
    if (e.type === 'agent.registered') {
      agents[id] = { domains: d.domains || [], status: 'online' };
    } else if (e.type === 'agent.deregistered') {
      if (agents[id]) agents[id].status = 'offline';
    } else if (e.type === 'agent.status') {
      if (agents[id]) agents[id].status = d.status || 'unknown';
    }
  });

  const domainMap = {};
  Object.entries(agents).forEach(([id, a]) => {
    if (a.status === 'offline') return;
    (a.domains || []).forEach(d => {
      if (!domainMap[d]) domainMap[d] = [];
      domainMap[d].push(id);
    });
  });

  const windowStart = new Date(new Date(targetTimestamp).getTime() - 30000).toISOString();
  const busTypes = ['bus.ask', 'bus.response', 'bus.announce', 'bus.surrogate'];
  const recentActivity = Object.values(state.eventStore)
    .filter(e => busTypes.includes(e.type) && e.timestamp >= windowStart && e.timestamp <= targetTimestamp)
    .sort((a, b) => a.timestamp < b.timestamp ? -1 : 1);

  return { agents, domainMap, recentActivity };
}

function renderBusStateSection(event) {
  const busState = reconstructBusStateAt(event.timestamp);
  const agentEntries = Object.entries(busState.agents).filter(([_, a]) => a.status !== 'offline');

  if (agentEntries.length === 0 && busState.recentActivity.length === 0) {
    return '';
  }

  let html = '<div class="bus-state-section">';
  html += '<div class="bus-state-heading">&#x1F4E1; Bus State at this moment</div>';

  if (agentEntries.length > 0) {
    agentEntries.forEach(([id, a]) => {
      const statusColor = a.status === 'online' ? 'var(--accent-green)' :
                          a.status === 'sleeping' ? 'var(--accent-amber)' : 'var(--accent-red)';
      const domains = (a.domains || []).map(d =>
        `<span class="modal-tag" style="font-size:9px;padding:1px 5px">${escapeHtml(d)}</span>`
      ).join('');
      html += `<div class="bus-agent-row">
        <span class="agent-name">${escapeHtml(id)}</span>
        <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${statusColor}"></span>
        <span style="font-size:10px;color:var(--text-muted)">${a.status}</span>
        ${domains}
      </div>`;
    });
  }

  const domainEntries = Object.entries(busState.domainMap);
  if (domainEntries.length > 0) {
    html += '<div style="margin-top:8px">';
    html += '<div class="bus-state-heading" style="font-size:10px;margin-bottom:4px">Domain Routing</div>';
    domainEntries.forEach(([domain, agentIds]) => {
      html += `<div class="bus-domain-group">
        <span class="bus-domain-label">${escapeHtml(domain)}</span>
        <span class="bus-domain-agents">${agentIds.map(a => escapeHtml(a)).join(', ')}</span>
      </div>`;
    });
    html += '</div>';
  }

  if (busState.recentActivity.length > 0) {
    html += '<div style="margin-top:10px">';
    html += '<div class="bus-state-heading" style="font-size:10px;margin-bottom:4px">Recent Bus Activity</div>';
    busState.recentActivity.forEach(e => {
      const d = e.data || {};
      const icon = e.type === 'bus.ask' ? '?' :
                   e.type === 'bus.response' ? '&#x2713;' :
                   e.type === 'bus.announce' ? '!' : '&#x2601;';
      const iconColor = e.type === 'bus.ask' ? 'var(--accent-blue)' :
                        e.type === 'bus.response' ? 'var(--accent-green)' :
                        e.type === 'bus.announce' ? 'var(--accent-amber)' : 'var(--accent-pink)';
      const text = d.question || d.answer || d.content || d.event || e.type;
      const truncated = typeof text === 'string' && text.length > 60 ? text.slice(0, 60) + '...' : text;
      html += `<div class="bus-timeline-item" onclick="showDetail('${e.id}')">
        <span class="bus-timeline-icon" style="color:${iconColor}">${icon}</span>
        <span>${escapeHtml(e.agent_id || '')} ${escapeHtml(String(truncated))}</span>
        <span class="bus-timeline-time">${formatTime(e.timestamp)}</span>
      </div>`;
    });
    html += '</div>';
  } else {
    html += '<div class="bus-no-activity">No bus activity in the 30s window</div>';
  }

  html += '</div>';
  return html;
}

// ── Admission Panel ─────────────────────────────────────────────────
const MAX_ADMISSION_FEED = 30;
const ADMISSION_FEATURES = [
  'utility', 'temporal_salience', 'persistence', 'state_change_signal'
];

let admissionExpanded = {};

function toggleAdmissionFeed() {
  const feed = document.getElementById('admission-feed');
  const toggle = document.getElementById('admission-toggle');
  if (feed.style.display === 'none') {
    feed.style.display = '';
    toggle.innerHTML = '&#x25B2;';
  } else {
    feed.style.display = 'none';
    toggle.innerHTML = '&#x25BC;';
  }
}

function renderAdmissionFeed() {
  const feed = document.getElementById('admission-feed');
  if (state.admissionFeed.length === 0) {
    feed.innerHTML = '<div class="admission-empty">No admission decisions yet</div>';
    return;
  }

  let html = '';
  state.admissionFeed.forEach((item, idx) => {
    const pct = Math.min(100, Math.max(0, item.score * 100));
    const barColor = item.route === 'discard' ? 'var(--accent-red)' :
                     item.route === 'ephemeral_cache' ? 'var(--accent-amber)' :
                     item.route === 'persist' ? 'var(--accent-blue)' : 'var(--accent-blue)';
    const routeClass = 'route-' + item.route;
    const truncContent = item.content && item.content.length > 40 ? item.content.slice(0, 40) + '...' : (item.content || '');

    html += `<div>
      <div class="admission-item" onclick="admissionExpanded['${idx}'] = !admissionExpanded['${idx}']; renderAdmissionFeed()">
        <div class="admission-score-bar">
          <div class="admission-score-fill" style="width:${pct}%;background:${barColor}"></div>
        </div>
        <span style="font-size:11px;color:var(--text-secondary);min-width:35px">${item.score.toFixed(2)}</span>
        <span class="admission-route ${routeClass}">${item.route.replace(/_/g, ' ')}</span>
        <span style="flex:1;font-size:11px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(truncContent)}</span>
        <span style="font-size:10px;color:var(--text-muted)">${formatTime(item.timestamp)}</span>
      </div>`;

    if (admissionExpanded[idx]) {
      html += '<div class="admission-features">';
      ADMISSION_FEATURES.forEach(f => {
        const val = (item.features[f] != null) ? item.features[f] : 0;
        const fPct = Math.min(100, Math.max(0, val * 100));
        const label = f.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).replace(/Salience|Affinity|Signal/g, '').trim();
        html += `<div class="admission-feature">
          <span class="admission-feature-label">${label.slice(0, 8)}</span>
          <div class="admission-feature-bar">
            <div class="admission-feature-fill" style="width:${fPct}%"></div>
          </div>
          <span style="font-size:9px;color:var(--text-muted)">${val.toFixed(2)}</span>
        </div>`;
      });
      html += '</div>';
    }
    html += '</div>';
  });

  feed.innerHTML = html;
}

// ── Pipeline Handling ────────────────────────────────────────────────
function handlePipelineEvent(event) {
  const d = event.data || {};
  const pipelineId = d.pipeline_id;
  if (!pipelineId) return;

  if (!state.pipelines[pipelineId]) {
    state.pipelines[pipelineId] = {
      type: d.pipeline_type,
      stages: [],
      agent_id: event.agent_id,
      started: event.timestamp,
    };
  }

  const pipeline = state.pipelines[pipelineId];
  pipeline.stages.push({
    stage: d.stage,
    duration_ms: d.duration_ms,
    data: d,
    timestamp: event.timestamp,
  });

  if (d.stage === 'complete' && d.total_duration_ms != null) {
    pipeline.total_duration_ms = d.total_duration_ms;
  }

  state.activePipeline = pipelineId;

  const pipelineIds = Object.keys(state.pipelines);
  if (pipelineIds.length > MAX_PIPELINES) {
    const toRemove = pipelineIds.slice(0, pipelineIds.length - MAX_PIPELINES);
    toRemove.forEach(id => delete state.pipelines[id]);
  }

  renderPipelinePanel();
}

function buildPipelineStagesHTML(pipelineId, pipeline) {
  const stageOrder = pipeline.type === 'store' ? STORE_STAGES : SEARCH_STAGES;
  const reachedStages = new Set(pipeline.stages.map(s => s.stage));
  const stageDataMap = {};
  pipeline.stages.forEach(s => { stageDataMap[s.stage] = s; });

  const isComplete = reachedStages.has('complete');
  let lastReachedIdx = -1;
  stageOrder.forEach((s, i) => {
    if (reachedStages.has(s)) lastReachedIdx = i;
  });

  let stagesHTML = '';
  stageOrder.forEach((stageName, i) => {
    const reached = reachedStages.has(stageName);
    const isActive = !isComplete && !reached && i === lastReachedIdx + 1;
    const isSkipped = isComplete && !reached;
    let stateClass = '';
    if (reached) stateClass = 'reached';
    else if (isActive) stateClass = 'active';
    else if (isSkipped) stateClass = 'skipped';

    const label = STAGE_LABELS[stageName] || stageName;
    const stageData = stageDataMap[stageName];
    const timeStr = stageData ? `${stageData.duration_ms.toFixed(1)}ms` : '';

    if (i > 0) {
      const prevReached = reachedStages.has(stageOrder[i - 1]);
      const arrowClass = prevReached && reached ? 'pipeline-arrow reached' : 'pipeline-arrow';
      stagesHTML += `<div class="${arrowClass}"></div>`;
    }

    stagesHTML += `<div class="pipeline-stage ${stateClass}" onclick="toggleStageDetail('${pipelineId}', '${stageName}')">
      <div class="pipeline-dot"></div>
      <div class="pipeline-stage-label">${label}</div>
      ${timeStr ? `<div class="pipeline-stage-time">${timeStr}</div>` : '<div class="pipeline-stage-time">&nbsp;</div>'}
    </div>`;
  });

  return { stagesHTML, stageDataMap };
}

// Alias for time-travel replay compatibility
function renderPipelines() {
  renderPipelinePanel();
}

function renderPipelinePanel() {
  const panel = document.getElementById('pipeline-panel');
  const pipelineId = state.activePipeline;

  if (!pipelineId || !state.pipelines[pipelineId]) {
    panel.innerHTML = '<div class="pipeline-empty">No pipeline activity yet</div>';
    panel.removeAttribute('data-pipeline-id');
    return;
  }

  const pipeline = state.pipelines[pipelineId];
  const typeBadgeClass = pipeline.type === 'store' ? 'pipeline-type-store' : 'pipeline-type-search';
  const typeLabel = pipeline.type === 'store' ? 'STORE' : 'SEARCH';
  const durationStr = pipeline.total_duration_ms != null
    ? `${pipeline.total_duration_ms.toFixed(1)}ms`
    : 'running...';
  const agentStr = pipeline.agent_id ? escapeHtml(pipeline.agent_id) : '';

  const { stagesHTML, stageDataMap } = buildPipelineStagesHTML(pipelineId, pipeline);

  let detailHTML = '';
  if (state.expandedStage && state.expandedStage.pipelineId === pipelineId) {
    const sd = stageDataMap[state.expandedStage.stage];
    if (sd) {
      detailHTML = buildStageDetailHTML(sd);
    }
  }

  const currentRendered = panel.getAttribute('data-pipeline-id');

  if (currentRendered === pipelineId) {
    const dur = panel.querySelector('.pipeline-duration');
    if (dur) dur.textContent = durationStr;

    const stagesEl = panel.querySelector('.pipeline-stages');
    if (stagesEl) stagesEl.innerHTML = stagesHTML;

    const existingDetail = panel.querySelector('.pipeline-stage-detail');
    if (existingDetail) existingDetail.remove();
    if (detailHTML) {
      const card = panel.querySelector('.pipeline-card');
      if (card) card.insertAdjacentHTML('beforeend', detailHTML);
    }
  } else {
    panel.setAttribute('data-pipeline-id', pipelineId);
    panel.innerHTML = `<div class="pipeline-card pipeline-new">
      <div class="pipeline-header">
        <span class="pipeline-type-badge ${typeBadgeClass}">${typeLabel}</span>
        ${agentStr ? `<span class="pipeline-agent">${agentStr}</span>` : ''}
        <span class="pipeline-duration">${durationStr}</span>
      </div>
      <div class="pipeline-stages">${stagesHTML}</div>
      ${detailHTML}
    </div>`;
  }
}

function toggleStageDetail(pipelineId, stage) {
  if (state.expandedStage &&
      state.expandedStage.pipelineId === pipelineId &&
      state.expandedStage.stage === stage) {
    state.expandedStage = null;
  } else {
    state.expandedStage = { pipelineId, stage };
  }
  renderPipelinePanel();
}

function buildStageDetailHTML(stageInfo) {
  const d = stageInfo.data || {};
  const stageName = STAGE_LABELS[stageInfo.stage] || stageInfo.stage;

  const skipKeys = new Set([
    'pipeline_id', 'pipeline_type', 'stage', 'duration_ms', 'candidates',
  ]);
  const rows = Object.entries(d)
    .filter(([k]) => !skipKeys.has(k))
    .map(([k, v]) => {
      let displayVal = v;
      if (Array.isArray(v)) {
        displayVal = v.length > 0 ? v.join(', ') : '(none)';
      } else if (v === null || v === undefined) {
        displayVal = '—';
      } else if (typeof v === 'number') {
        displayVal = Number.isInteger(v) ? String(v) : v.toFixed(3);
      }
      const label = k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      return `<div class="pipeline-detail-row">
        <span class="pipeline-detail-key">${escapeHtml(label)}</span>
        <span class="pipeline-detail-value" title="${escapeHtml(String(v))}">${escapeHtml(String(displayVal))}</span>
      </div>`;
    }).join('');

  let candidateHTML = '';
  if (d.candidates && Array.isArray(d.candidates) && d.candidates.length > 0) {
    const isActr = stageInfo.stage === 'actr_scoring';
    const items = d.candidates.map((c, i) => {
      const score = typeof c.score === 'number' ? c.score.toFixed(3) : '—';
      let extraScores = '';
      if (isActr && c.total_activation !== undefined) {
        extraScores = `<span class="pipeline-candidate-scores">`
          + `bm25:${(c.bm25_score || 0).toFixed(2)}`
          + ` bl:${(c.base_level || 0).toFixed(2)}`
          + ` sp:${(c.spreading || 0).toFixed(2)}`
          + ` p:${(c.retrieval_prob || 0).toFixed(2)}`
          + `</span>`;
      }
      return `<div class="pipeline-candidate-item" title="${escapeHtml(c.id || '')}">
        <span class="pipeline-candidate-rank">#${i + 1}</span>
        <span class="pipeline-candidate-score">${score}</span>
        <span class="pipeline-candidate-content">${escapeHtml(c.content || c.id || '')}</span>
        ${extraScores}
      </div>`;
    }).join('');
    candidateHTML = `
      <div class="pipeline-candidates-header">Candidates (${d.candidates.length})</div>
      <div class="pipeline-candidates">${items}</div>`;
  }

  if (!rows && !candidateHTML) return '';

  return `<div class="pipeline-stage-detail">
    <div class="pipeline-stage-detail-title">${escapeHtml(stageName)} &middot; ${stageInfo.duration_ms.toFixed(2)}ms</div>
    ${rows}
    ${candidateHTML}
  </div>`;
}
