// ── NCMS Dashboard — Audit Timeline ──────────────────────────────────
// Chronological table of all audit events for a project.
// Loaded from GET /api/v1/projects/{id}/audit-timeline

const AUDIT_TYPE_CONFIG = {
  pipeline:  { color: '#58a6ff', label: 'Pipeline', icon: '\u2699' },
  llm_call:  { color: '#a78bfa', label: 'LLM',      icon: '\uD83E\uDDE0' },
  review:    { color: '#10b981', label: 'Review',   icon: '\u2B50' },
  bus:       { color: '#22c55e', label: 'Bus',      icon: '\uD83D\uDCE8' },
  guardrail: { color: '#f59e0b', label: 'Guard',    icon: '\u26A0' },
  approval:  { color: '#8b5cf6', label: 'Approval', icon: '\u2714' },
  grounding: { color: '#6e7681', label: 'Ground',   icon: '\uD83D\uDD17' },
  config:    { color: '#64748b', label: 'Config',   icon: '\u2699' },
  research:  { color: '#06b6d4', label: 'Research', icon: '\uD83D\uDD0D' },
};

async function loadAuditTimeline(projectId, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.innerHTML = '<div class="audit-loading">Loading audit timeline...</div>';

  try {
    const resp = await fetch(HUB_API + '/api/v1/projects/' + encodeURIComponent(projectId) + '/audit-timeline');
    if (!resp.ok) {
      container.innerHTML = '<div class="audit-error">Failed to load timeline</div>';
      return;
    }
    const events = await resp.json();
    renderAuditTimeline(events, container, projectId);
  } catch (e) {
    container.innerHTML = '<div class="audit-error">Error: ' + escapeHtml(e.message) + '</div>';
  }
}

function renderAuditTimeline(events, container, projectId) {
  if (!events || events.length === 0) {
    container.innerHTML = '<div class="audit-empty">No audit events for this project</div>';
    return;
  }

  // Filter controls
  const types = [...new Set(events.map(e => e.type))];
  const agents = [...new Set(events.map(e => e.agent).filter(a => a))];

  let html = '<div class="audit-controls">';
  html += '<div class="audit-filters">';
  html += types.map(t => {
    const cfg = AUDIT_TYPE_CONFIG[t] || { color: '#64748b', label: t, icon: '' };
    return `<button class="audit-filter-chip active" data-type="${t}" onclick="toggleAuditFilter(this)" style="--chip-color:${cfg.color}">${cfg.icon} ${cfg.label}</button>`;
  }).join('');
  html += '</div>';
  html += `<span class="audit-count">${events.length} events</span>`;
  html += '</div>';

  // Table
  html += '<div class="audit-table-wrap"><table class="audit-table">';
  html += '<thead><tr><th>Time</th><th>Type</th><th>Agent</th><th>Detail</th></tr></thead>';
  html += '<tbody>';

  for (const evt of events) {
    const cfg = AUDIT_TYPE_CONFIG[evt.type] || { color: '#64748b', label: evt.type, icon: '' };
    const time = formatAuditTime(evt.timestamp);
    const detail = escapeHtml(evt.detail || '');
    let extra = '';
    if (evt.type === 'research' && evt.extra) {
      try {
        const data = JSON.parse(evt.extra);
        const uid = 'rd-' + Math.random().toString(36).substring(2, 8);
        if (data.type === 'research_plan') {
          const counts = ['web','arxiv','patent','community']
            .map(e => `${e}: ${(data[e]||[]).length}`).join(', ');
          let expanded = ['web','arxiv','patent','community'].filter(e => (data[e]||[]).length)
            .map(e => `<div style="margin:4px 0"><b>${e}</b>:<br>${data[e].map(q =>
              `<span style="display:block;padding:1px 0;color:#94a3b8">\u2022 ${escapeHtml(q)}</span>`
            ).join('')}</div>`).join('');
          extra = `<div class="audit-extra">
            <span class="audit-expand-summary" onclick="document.getElementById('${uid}').style.display=document.getElementById('${uid}').style.display==='none'?'block':'none'" style="cursor:pointer">
              Query plan: ${counts} <span style="color:#58a6ff">\u25BC</span>
            </span>
            <div id="${uid}" style="display:none;margin-top:4px;padding:6px 8px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:10px">${expanded}</div>
          </div>`;
        } else if (data.type === 'research_results') {
          const top = (data.top_results || []).map(r => escapeHtml(r.title || r.url || '?'));
          const fb = data.used_fallback ? ' <span style="color:#f59e0b">[fallback]</span>' : '';
          const summary = `<b>${data.engine}</b>: ${data.result_count} results${fb}`;
          let expanded = '';
          if (top.length) expanded += top.map(t => `<span style="display:block;padding:1px 0;color:#94a3b8">\u2022 ${t}</span>`).join('');
          if (data.query_matched) expanded += `<div style="margin-top:4px;color:#10b981">Matched: ${escapeHtml(data.query_matched)}</div>`;
          if (data.abstracts_fetched != null) expanded += `<div style="color:#94a3b8">Abstracts: ${data.abstracts_fetched}</div>`;
          extra = `<div class="audit-extra">
            <span class="audit-expand-summary" onclick="document.getElementById('${uid}').style.display=document.getElementById('${uid}').style.display==='none'?'block':'none'" style="cursor:pointer">
              ${summary} <span style="color:#58a6ff">\u25BC</span>
            </span>
            <div id="${uid}" style="display:none;margin-top:4px;padding:6px 8px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:10px">${expanded}</div>
          </div>`;
        }
      } catch(e) { extra = evt.extra ? `<div class="audit-extra">${escapeHtml(evt.extra)}</div>` : ''; }
    } else {
      extra = evt.extra ? `<div class="audit-extra">${escapeHtml(evt.extra)}</div>` : '';
    }

    html += `<tr class="audit-row" data-type="${evt.type}">
      <td class="audit-time">${time}</td>
      <td><span class="audit-type-badge" style="background:${cfg.color}20;color:${cfg.color};border-color:${cfg.color}40">${cfg.icon} ${cfg.label}</span></td>
      <td class="audit-agent">${escapeHtml(evt.agent)}</td>
      <td class="audit-detail">${detail}${extra}</td>
    </tr>`;
  }

  html += '</tbody></table></div>';
  container.innerHTML = html;
}

function toggleAuditFilter(btn) {
  btn.classList.toggle('active');
  const type = btn.dataset.type;
  const rows = btn.closest('.audit-timeline-container').querySelectorAll(`.audit-row[data-type="${type}"]`);
  const show = btn.classList.contains('active');
  rows.forEach(r => r.style.display = show ? '' : 'none');
}

function formatAuditTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return ''; }
}
