/**
 * Learning Card — live display of consolidation, dream cycle, and
 * maintenance batch job activity.
 *
 * Listens for SSE events:
 *   consolidation.pass_complete  — full consolidation pass results
 *   consolidation.abstract_created — individual abstract creation
 *   dream.cycle_complete         — dream rehearsal/association results
 *   maintenance.task_complete    — scheduler task completion
 *   maintenance.task_error       — scheduler task failure
 *   episode.created / .closed    — episode lifecycle
 */

// ── State ────────────────────────────────────────────────────────────
const learningState = {
  consolidationRuns: 0,
  dreamRuns: 0,
  episodesCreated: 0,
  episodesClosed: 0,
  abstractsCreated: 0,
  lastConsolidation: null,   // { counts, timestamp }
  lastDream: null,           // { counts, timestamp }
  activityLog: [],           // most recent 20 activities
  activated: false,          // has any learning event arrived?
};

// ── Event Handler (called from handleEvent in agents.js) ─────────────
function handleLearningEvent(event) {
  const d = event.data || {};
  let activity = null;

  switch (event.type) {
    case 'consolidation.pass_complete':
      learningState.consolidationRuns++;
      learningState.lastConsolidation = {
        counts: d,
        timestamp: event.timestamp,
      };
      activity = _fmtConsolidation(d, event.timestamp);
      break;

    case 'consolidation.abstract_created':
      learningState.abstractsCreated++;
      activity = _fmtAbstract(d, event.timestamp);
      break;

    case 'dream.cycle_complete':
      learningState.dreamRuns++;
      learningState.lastDream = {
        counts: d,
        timestamp: event.timestamp,
      };
      activity = _fmtDream(d, event.timestamp);
      break;

    case 'maintenance.task_complete':
      if (d.task === 'consolidation' || d.task === 'dream' ||
          d.task === 'decay' || d.task === 'episode_close') {
        activity = _fmtMaintenance(d, event.timestamp);
      }
      break;

    case 'maintenance.task_error':
      if (d.task === 'consolidation' || d.task === 'dream' ||
          d.task === 'decay' || d.task === 'episode_close') {
        activity = _fmtMaintenanceError(d, event.timestamp);
      }
      break;

    case 'episode.created':
      learningState.episodesCreated++;
      activity = _fmtEpisode('created', d, event.timestamp);
      break;

    case 'episode.closed':
      learningState.episodesClosed++;
      activity = _fmtEpisode('closed', d, event.timestamp);
      break;

    default:
      return; // not a learning event
  }

  // Activate card on first event
  if (!learningState.activated) {
    _activateCard();
    learningState.activated = true;
  }

  if (activity) {
    learningState.activityLog.unshift(activity);
    if (learningState.activityLog.length > 20) learningState.activityLog.pop();
  }

  _renderLearning();
}

// ── Activation ───────────────────────────────────────────────────────
function _activateCard() {
  const card = document.getElementById('learning-card');
  const name = document.getElementById('learning-card-name');
  const metrics = document.getElementById('learning-metrics');
  if (!card) return;

  card.classList.add('learning-active');
  if (name) name.textContent = '\u{1F9E0} Learning';  // brain emoji replaces lock
  if (metrics) metrics.style.display = '';
}

// ── Render ────────────────────────────────────────────────────────────
function _renderLearning() {
  const s = learningState;

  // Metric values
  _setText('lm-consolidation', s.consolidationRuns > 0
    ? `${s.consolidationRuns} run${s.consolidationRuns !== 1 ? 's' : ''}` : '\u2014');
  _setText('lm-dream', s.dreamRuns > 0
    ? `${s.dreamRuns} cycle${s.dreamRuns !== 1 ? 's' : ''}` : '\u2014');

  const totalEp = s.episodesCreated + s.episodesClosed;
  _setText('lm-episodes', totalEp > 0
    ? `${s.episodesCreated} created / ${s.episodesClosed} closed` : '\u2014');
  _setText('lm-abstracts', s.abstractsCreated > 0
    ? String(s.abstractsCreated) : '\u2014');

  // Status line
  let statusText = 'Listening...';
  if (s.lastConsolidation) {
    const c = s.lastConsolidation.counts;
    const parts = [];
    if (c.decay) parts.push(`${c.decay} decayed`);
    if (c.knowledge) parts.push(`${c.knowledge} insights`);
    if (c.episodes) parts.push(`${c.episodes} ep summaries`);
    if (c.trajectories) parts.push(`${c.trajectories} trajectories`);
    if (c.patterns) parts.push(`${c.patterns} patterns`);
    statusText = parts.length > 0
      ? `Last: ${parts.join(', ')} (${_relTime(s.lastConsolidation.timestamp)})`
      : `Last consolidation: ${_relTime(s.lastConsolidation.timestamp)}`;
  }
  _setText('learning-card-status', statusText);

  // Activity log
  const logEl = document.getElementById('learning-activity');
  if (logEl && s.activityLog.length > 0) {
    logEl.innerHTML = s.activityLog.slice(0, 10).join('');
  }
}

// ── Formatters ───────────────────────────────────────────────────────
function _fmtConsolidation(d, ts) {
  const parts = [];
  if (d.decay) parts.push(`${d.decay} decay`);
  if (d.knowledge) parts.push(`${d.knowledge} knowledge`);
  if (d.episodes) parts.push(`${d.episodes} episodes`);
  if (d.trajectories) parts.push(`${d.trajectories} trajectories`);
  if (d.patterns) parts.push(`${d.patterns} patterns`);
  if (d.refresh) parts.push(`${d.refresh} refresh`);
  const detail = parts.length > 0 ? parts.join(', ') : 'no changes';
  return _activityItem('consolidation', detail, ts);
}

function _fmtAbstract(d, ts) {
  const type = (d.abstract_type || 'unknown').replace(/_/g, ' ');
  return _activityItem('abstract', `${type} (${d.source_count || '?'} sources)`, ts);
}

function _fmtDream(d, ts) {
  const parts = [];
  if (d.rehearsal) parts.push(`${d.rehearsal} rehearsed`);
  if (d.associations) parts.push(`${d.associations} assoc`);
  if (d.forgetting) parts.push(`${d.forgetting} forgotten`);
  if (d.drift) parts.push(`${d.drift} drifted`);
  const detail = parts.length > 0 ? parts.join(', ') : 'no changes';
  return _activityItem('dream', detail, ts);
}

function _fmtMaintenance(d, ts) {
  const ms = d.duration_ms ? ` (${Math.round(d.duration_ms)}ms)` : '';
  return _activityItem(d.task, `completed${ms}`, ts);
}

function _fmtMaintenanceError(d, ts) {
  const err = d.error || 'unknown error';
  return _activityItem(d.task, `<span style="color:#f87171">failed: ${err}</span>`, ts);
}

function _fmtEpisode(action, d, ts) {
  let title = d.title || d.episode_id?.slice(0, 8) || '?';
  if (title.length > 60) title = title.slice(0, 57) + '...';
  const members = d.member_count ? ` (${d.member_count} members)` : '';
  return _activityItem('episode', `${action}: ${title}${members}`, ts);
}

function _activityItem(type, detail, ts) {
  return `<div class="learning-activity-item">`
    + `<span class="la-type">${type}</span>`
    + `<span class="la-detail">${detail}</span>`
    + `<span class="la-time">${_relTime(ts)}</span>`
    + `</div>`;
}

// ── Bootstrap (rehydrate on page load) ───────────────────────────────
const LEARNING_EVENT_TYPES = new Set([
  'consolidation.pass_complete', 'consolidation.abstract_created',
  'dream.cycle_complete',
  'maintenance.task_complete', 'maintenance.task_error',
  'episode.created', 'episode.closed',
]);

async function bootstrapLearning() {
  try {
    const resp = await fetch('/api/events?limit=500');
    if (!resp.ok) return;
    const events = await resp.json();
    // Replay learning-relevant events oldest-first
    const relevant = events
      .filter(e => LEARNING_EVENT_TYPES.has(e.type))
      .reverse();
    for (const evt of relevant) {
      handleLearningEvent(evt);
    }
  } catch (_) { /* silently fail — SSE will catch up */ }
}

// ── Helpers ──────────────────────────────────────────────────────────
function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _relTime(isoTs) {
  if (!isoTs) return '';
  const diff = (Date.now() - new Date(isoTs).getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
