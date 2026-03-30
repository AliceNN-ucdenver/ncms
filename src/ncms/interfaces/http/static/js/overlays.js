// ── NCMS Dashboard — Overlays (Episodes, State History) ──────────────

// ── Episode Timeline ────────────────────────────────────────────────
function openEpisodeView() {
  document.getElementById('episode-overlay').style.display = 'flex';
  loadEpisodeData();
}

function closeEpisodeView() {
  document.getElementById('episode-overlay').style.display = 'none';
}

async function loadEpisodeData() {
  const area = document.getElementById('episode-timeline-area');
  try {
    const resp = await fetch('/api/episodes');
    const episodes = await resp.json();

    if (!episodes || episodes.length === 0) {
      area.innerHTML = '<div class="episode-empty">No episodes found. Enable episode formation with NCMS_EPISODES_ENABLED=true</div>';
      return;
    }

    let html = '';
    episodes.forEach(ep => {
      const isOpen = ep.status === 'open';
      const statusClass = isOpen ? 'open' : 'closed';
      const title = ep.title || '(untitled episode)';
      const created = ep.created_at ? formatTimeFull(ep.created_at) : '';
      const closed = ep.closed_at ? ' &middot; closed ' + formatTimeFull(ep.closed_at) : '';

      html += `<div class="episode-card" onclick="loadEpisodeDetail('${ep.episode_id}')">
        <div class="episode-card-header">
          <span class="episode-status-dot ${statusClass}"></span>
          <span class="episode-card-title">${escapeHtml(title)}</span>
        </div>
        <div class="episode-card-meta">
          <span>${ep.member_count || 0} members</span>
          <span>${created}${closed}</span>
          <span style="text-transform:uppercase;font-size:10px;font-weight:600;color:${isOpen ? 'var(--accent-green)' : 'var(--text-muted)'}">${ep.status}</span>
        </div>
      </div>`;
    });

    area.innerHTML = html;
  } catch (err) {
    area.innerHTML = `<div class="episode-empty">Error loading episodes: ${err.message}</div>`;
  }
}

async function loadEpisodeDetail(episodeId) {
  const panel = document.getElementById('episode-detail-panel');
  panel.style.display = '';

  try {
    const resp = await fetch(`/api/episodes/${episodeId}`);
    const detail = await resp.json();

    let html = `<div class="episode-detail-title">${escapeHtml(detail.title || '(untitled)')}</div>`;
    html += `<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">
      Status: <strong>${detail.status}</strong> &middot; ${detail.member_count} members
    </div>`;

    if (detail.members && detail.members.length > 0) {
      detail.members.forEach(m => {
        html += `<div class="episode-member">
          <div class="episode-member-type">${m.node_type || 'atomic'}</div>
          <div class="episode-member-content">${escapeHtml(m.content || '(no content)')}</div>
        </div>`;
      });
    } else {
      html += '<div style="color:var(--text-muted);font-size:12px">No members</div>';
    }

    panel.innerHTML = html;
  } catch (err) {
    panel.innerHTML = `<div style="color:var(--accent-red);font-size:12px">Error: ${err.message}</div>`;
  }
}

// ── State History Timeline ──────────────────────────────────────────
function openStateHistoryView() {
  document.getElementById('state-history-overlay').style.display = 'flex';
  loadEntitiesWithStates();
}

function closeStateHistoryView() {
  document.getElementById('state-history-overlay').style.display = 'none';
}

async function loadEntitiesWithStates() {
  const list = document.getElementById('state-history-list');
  try {
    const resp = await fetch('/api/entities-with-states');
    const entities = await resp.json();

    if (!entities || entities.length === 0) {
      list.innerHTML = '<div class="state-timeline-empty">No entity states found. Enable reconciliation with NCMS_RECONCILIATION_ENABLED=true</div>';
      return;
    }

    let html = '';
    entities.forEach(e => {
      const keys = (e.state_keys || []).join(', ');
      html += `<div class="state-entity-card" id="state-card-${e.entity_id}" onclick="loadStateHistory('${escapeHtml(e.entity_id)}', this)">
        <div class="state-entity-name">${escapeHtml(e.entity_id)}</div>
        <div class="state-entity-meta">
          <span>${e.state_count} state${e.state_count !== 1 ? 's' : ''}</span>
          <span>${e.current_count} current</span>
        </div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">keys: ${escapeHtml(keys)}</div>
      </div>`;
    });

    list.innerHTML = html;
  } catch (err) {
    list.innerHTML = `<div class="state-timeline-empty">Error: ${err.message}</div>`;
  }
}

async function loadStateHistory(entityId, cardEl) {
  document.querySelectorAll('.state-entity-card.active').forEach(c => c.classList.remove('active'));
  if (cardEl) cardEl.classList.add('active');

  const area = document.getElementById('state-timeline-area');
  area.innerHTML = '<div class="state-timeline-empty">Loading state history...</div>';

  try {
    const statesResp = await fetch(`/api/entity-states/${encodeURIComponent(entityId)}`);
    const statesData = await statesResp.json();

    const allStates = statesData.current_states || [];
    const stateKeys = new Set();
    allStates.forEach(s => {
      const key = (s.metadata && s.metadata.state_key) || 'state';
      stateKeys.add(key);
    });
    if (stateKeys.size === 0) stateKeys.add('state');

    let html = `<h3 style="color:var(--text-primary);font-size:15px;margin:0 0 16px 0">${escapeHtml(entityId)}</h3>`;
    let anyHistory = false;

    for (const key of stateKeys) {
      const histResp = await fetch(`/api/entity-states/${encodeURIComponent(entityId)}/history?key=${encodeURIComponent(key)}`);
      const histData = await histResp.json();
      const states = histData.states || [];

      if (states.length === 0) continue;
      anyHistory = true;

      html += `<div class="state-key-group">
        <div class="state-key-label">${escapeHtml(key)} (${states.length} transition${states.length !== 1 ? 's' : ''})</div>`;

      states.forEach((s, i) => {
        const isCurrent = s.is_current;
        const dotClass = isCurrent ? 'current' : 'superseded';
        const cardClass = isCurrent ? ' current' : '';

        const value = (s.metadata && s.metadata.state_value) || '(unknown)';
        const scope = (s.metadata && s.metadata.state_scope) || '';
        const validFrom = s.valid_from ? formatTimeFull(s.valid_from) : '';
        const validTo = s.valid_to ? formatTimeFull(s.valid_to) : '';
        const observedAt = s.observed_at ? formatTimeFull(s.observed_at) : '';

        html += `<div class="state-transition">
          <div class="state-timeline-rail">
            <div class="state-timeline-dot ${dotClass}"></div>
            ${i < states.length - 1 ? '<div class="state-timeline-line"></div>' : ''}
          </div>
          <div class="state-transition-card${cardClass}">
            <div class="state-transition-value">${escapeHtml(String(value))}</div>
            <div class="state-transition-meta">
              ${isCurrent ? '<span class="state-current-badge">current</span>' : '<span class="state-superseded-badge">superseded</span>'}
              ${validFrom ? `<span>from: ${validFrom}</span>` : ''}
              ${validTo ? `<span>to: ${validTo}</span>` : ''}
              ${observedAt ? `<span>observed: ${observedAt}</span>` : ''}
              ${scope ? `<span>scope: ${escapeHtml(scope)}</span>` : ''}
            </div>
          </div>
        </div>`;
      });

      html += '</div>';
    }

    if (!anyHistory) {
      html += '<div class="state-timeline-empty">No state transitions found for this entity</div>';
    }

    area.innerHTML = html;
  } catch (err) {
    area.innerHTML = `<div class="state-timeline-empty">Error: ${err.message}</div>`;
  }
}
