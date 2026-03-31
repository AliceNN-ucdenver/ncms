// ── NCMS Dashboard — App Core ────────────────────────────────────────
// Shared state, SSE connection, stats, init, utils, time-travel replay.
// Loaded first; other scripts depend on globals defined here.

// ── State ────────────────────────────────────────────────────────────
const state = {
  agents: {},
  eventCount: 0,
  agentActivities: {},   // agent_id -> [event, ...]
  eventStore: {},        // event.id -> event (for modal lookup)
  askIndex: {},          // ask_id -> { askEvent, responses: [event, ...] }
  pipelines: {},         // pipeline_id -> { type, stages, agent_id }
  activePipeline: null,
  expandedStage: null,
  admissionFeed: [],
  episodes: {},
  approvals: [],         // [{plan_id, title, content, from_agent, timestamp, status}]
  documents: [],
  agentChats: {},        // agent_id -> [{content, type, agentName, timestamp}]
  activeChatAgent: null, // currently open chat agent_id
};

const MAX_ACTIVITIES = 25;
const MAX_PIPELINES = 20;

// Stage order arrays
const STORE_STAGES = [
  'start', 'persist', 'bm25_index', 'splade_index',
  'entity_extraction', 'graph_linking',
  'contradiction', 'complete'
];
const SEARCH_STAGES = [
  'start', 'bm25', 'splade', 'rrf_fusion',
  'entity_extraction', 'graph_expansion', 'actr_scoring',
  'complete'
];

const STAGE_LABELS = {
  start: 'Start',
  persist: 'SQLite',
  bm25_index: 'BM25 Index',
  splade_index: 'SPLADE',
  entity_extraction: 'Entities',
  graph_linking: 'Graph Link',
  contradiction: 'Contradict',
  complete: 'Done',
  bm25: 'BM25',
  splade: 'SPLADE',
  rrf_fusion: 'RRF Fuse',
  graph_expansion: 'Graph Exp.',
  actr_scoring: 'ACT-R',
};

// Hub API base URL (hub on :9080, dashboard on :8420)
const HUB_API = window.location.protocol + '//' + window.location.hostname + ':9080';

// ── SSE Connection ───────────────────────────────────────────────────
let eventSource = null;
let reconnectTimeout = null;

function connectSSE() {
  if (eventSource) eventSource.close();

  eventSource = new EventSource('/api/events/stream');

  eventSource.onopen = () => {
    document.getElementById('connection-dot').classList.remove('disconnected');
  };

  eventSource.onerror = () => {
    document.getElementById('connection-dot').classList.add('disconnected');
    eventSource.close();
    clearTimeout(reconnectTimeout);
    reconnectTimeout = setTimeout(connectSSE, 3000);
  };

  const eventTypes = [
    'agent.registered', 'agent.deregistered', 'agent.status',
    'bus.ask', 'bus.response', 'bus.announce', 'bus.surrogate',
    'memory.stored', 'memory.searched',
    'episode.created', 'episode.assigned', 'episode.closed',
    'admission.scored',
    'document.published',
    'pipeline.node',
    'project.created', 'project.archived',
  ];

  eventTypes.forEach(type => {
    eventSource.addEventListener(type, (e) => {
      const parsed = JSON.parse(e.data);
      if (typeof timeline !== 'undefined' && timeline.mode === 'historical') {
        timeline.bufferedLiveEvents.push(parsed);
        return;
      }
      handleEvent(parsed);
    });
  });

  // Pipeline stage events
  const pipelineStages = [
    ...STORE_STAGES.map(s => `pipeline.store.${s}`),
    ...SEARCH_STAGES.map(s => `pipeline.search.${s}`),
  ];
  pipelineStages.forEach(type => {
    eventSource.addEventListener(type, (e) => {
      const parsed = JSON.parse(e.data);
      if (typeof timeline !== 'undefined' && timeline.mode === 'historical') {
        timeline.bufferedLiveEvents.push(parsed);
        return;
      }
      handlePipelineEvent(parsed);
    });
  });
}

// ── Utils ────────────────────────────────────────────────────────────
function formatTime(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString('en-US', {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch { return ''; }
}

function formatTimeFull(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('en-US', {
      hour12: false, month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch { return isoStr; }
}

function formatAge(seconds) {
  if (!seconds && seconds !== 0) return 'unknown';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatDurationMs(ms) {
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return totalSec + 's';
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min < 60) return min + 'm ' + sec + 's';
  const hr = Math.floor(min / 60);
  const rmin = min % 60;
  return hr + 'h ' + rmin + 'm';
}

// ── Stats ────────────────────────────────────────────────────────────
async function updateStats() {
  try {
    const resp = await fetch('/api/stats');
    const data = await resp.json();
    document.getElementById('stat-memories').textContent = data.memory_count;
    document.getElementById('stat-entities').textContent = data.entity_count;
    document.getElementById('stat-agents').textContent = `${data.agents_online}/${data.agent_count}`;
    document.getElementById('stat-events').textContent = data.event_count;
  } catch (e) { /* ignore */ }
}

function updateStatsLocal() {
  const online = Object.values(state.agents).filter(a => a.status === 'online').length;
  const total = Object.keys(state.agents).length;
  document.getElementById('stat-agents').textContent = online + '/' + total;
  document.getElementById('stat-events').textContent = state.eventCount;
}

// ── Time-Travel Replay ───────────────────────────────────────────
const timeline = {
  mode: 'live',
  allEvents: [],
  loaded: false,
  frozenState: null,
  bufferedLiveEvents: [],
  currentIndex: -1,
};

let _replaying = false;

function resetState() {
  state.agents = {};
  state.eventCount = 0;
  state.agentActivities = {};
  state.eventStore = {};
  state.askIndex = {};
  state.pipelines = {};
  state.activePipeline = null;
  state.expandedStage = null;
  state.admissionFeed = [];
  state.episodes = {};
}

function cloneState() {
  return JSON.parse(JSON.stringify({
    agents: state.agents,
    eventCount: state.eventCount,
    agentActivities: state.agentActivities,
    pipelines: state.pipelines,
    activePipeline: state.activePipeline,
    admissionFeed: state.admissionFeed,
    episodes: state.episodes,
  }));
}

function restoreFromSnapshot(snapshot) {
  state.agents = snapshot.agents;
  state.eventCount = snapshot.eventCount;
  state.agentActivities = snapshot.agentActivities;
  state.eventStore = {};
  state.askIndex = {};
  state.pipelines = snapshot.pipelines;
  state.activePipeline = snapshot.activePipeline;
  state.expandedStage = null;
  state.admissionFeed = snapshot.admissionFeed;
  state.episodes = snapshot.episodes;
}

async function enterHistoricalMode() {
  if (timeline.mode === 'historical') return;
  timeline.mode = 'historical';
  timeline.frozenState = cloneState();
  timeline.bufferedLiveEvents = [];

  document.getElementById('timeline-live-badge').style.display = 'none';
  document.getElementById('timeline-historical-badge').style.display = '';
  document.getElementById('timeline-return-btn').style.display = '';
  document.getElementById('timeline-history-banner').classList.add('visible');
  document.getElementById('timeline-event-index').style.display = '';

  if (!timeline.loaded) {
    document.getElementById('timeline-event-count').textContent = 'Loading history...';
    try {
      const resp = await fetch('/api/events/history?limit=10000');
      const data = await resp.json();
      timeline.allEvents = data.events || [];
      timeline.loaded = true;
    } catch (e) {
      document.getElementById('timeline-event-count').textContent = 'Failed to load';
      return;
    }
  }

  const scrubber = document.getElementById('timeline-scrubber');
  scrubber.max = Math.max(timeline.allEvents.length - 1, 1);
  document.getElementById('timeline-event-count').textContent =
    timeline.allEvents.length + ' events';
}

function replayToPosition(index) {
  if (!timeline.allEvents.length) return;
  if (index < 0) index = 0;
  if (index >= timeline.allEvents.length) index = timeline.allEvents.length - 1;
  timeline.currentIndex = index;

  resetState();
  _replaying = true;

  for (let i = 0; i <= index; i++) {
    const evt = timeline.allEvents[i];
    if (evt.type && evt.type.startsWith('pipeline.')) {
      handlePipelineEvent(evt);
    } else {
      handleEvent(evt);
    }
  }

  _replaying = false;

  renderAgents();
  updateChatTargets();
  for (const agentId of Object.keys(state.agents)) {
    renderAgentActivity(agentId);
  }
  renderAdmissionFeed();
  renderPipelines();
  updateStatsLocal();

  const evt = timeline.allEvents[index];
  if (evt) {
    const evtTime = new Date(evt.timestamp);
    const now = new Date();
    const diffMs = now - evtTime;
    document.getElementById('timeline-offset').textContent =
      'T\u2212' + formatDurationMs(diffMs);
    const bannerTs = document.getElementById('timeline-banner-timestamp');
    if (bannerTs) {
      bannerTs.textContent = evtTime.toLocaleString();
    }
  }
  const indexEl = document.getElementById('timeline-event-index');
  if (indexEl) {
    indexEl.textContent = 'Event ' + (index + 1) + ' / ' + timeline.allEvents.length;
  }
}

function returnToLive() {
  if (timeline.mode === 'live') return;
  timeline.mode = 'live';

  if (timeline.frozenState) {
    restoreFromSnapshot(timeline.frozenState);
    timeline.frozenState = null;
  }

  for (const evt of timeline.bufferedLiveEvents) {
    if (evt.type && evt.type.startsWith('pipeline.')) {
      handlePipelineEvent(evt);
    } else {
      handleEvent(evt);
    }
  }
  timeline.bufferedLiveEvents = [];

  document.getElementById('timeline-live-badge').style.display = '';
  document.getElementById('timeline-historical-badge').style.display = 'none';
  document.getElementById('timeline-return-btn').style.display = 'none';
  document.getElementById('timeline-event-count').textContent = '';
  document.getElementById('timeline-history-banner').classList.remove('visible');
  document.getElementById('timeline-event-index').style.display = 'none';
  document.getElementById('timeline-event-index').textContent = '';

  const scrubber = document.getElementById('timeline-scrubber');
  scrubber.max = 1000;
  scrubber.value = 1000;

  renderAgents();
  for (const agentId of Object.keys(state.agents)) {
    renderAgentActivity(agentId);
  }
  renderAdmissionFeed();
  renderPipelines();
  updateStats();
}

// ── Bootstrap & Init ─────────────────────────────────────────────────
async function bootstrapAgents() {
  try {
    const resp = await fetch('/api/agents');
    if (!resp.ok) return;
    const agents = await resp.json();
    for (const agent of agents) {
      state.agents[agent.agent_id] = {
        agent_id: agent.agent_id,
        domains: agent.domains || [],
        status: agent.status || 'online',
      };
    }
    renderAgents();
    updateChatTargets();
  } catch (e) { /* ignore */ }
}

// Register the human agent with the hub for approval routing
async function registerHumanAgent() {
  try {
    await fetch(HUB_API + '/api/v1/bus/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: 'human',
        domains: ['human-approval'],
        subscribe_to: ['approval-response', 'identity-service', 'implementation'],
      }),
    });
  } catch (e) {
    console.debug('Human agent registration skipped (hub may not be running)');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  connectSSE();
  updateStats();
  bootstrapAgents();
  registerHumanAgent();
  // Load any pending approvals from memory
  if (typeof loadPendingApprovals === 'function') {
    loadPendingApprovals();
  }
  if (typeof loadDocuments === 'function') {
    loadDocuments();
  }
  setInterval(() => {
    if (timeline.mode === 'live') updateStats();
  }, 5000);

  // Timeline scrubber
  const scrubber = document.getElementById('timeline-scrubber');
  let scrubDebounce = null;
  scrubber.addEventListener('input', () => {
    const val = parseInt(scrubber.value);
    const max = parseInt(scrubber.max);
    if (val >= max && timeline.mode === 'historical') {
      returnToLive();
      return;
    }
    if (timeline.mode === 'live') {
      enterHistoricalMode().then(() => {
        const s = document.getElementById('timeline-scrubber');
        const newMax = Math.max(timeline.allEvents.length - 1, 1);
        s.max = newMax;
        const idx = Math.round((val / 1000) * newMax);
        s.value = idx;
        replayToPosition(idx);
      });
      return;
    }
    clearTimeout(scrubDebounce);
    scrubDebounce = setTimeout(() => replayToPosition(val), 30);
  });

  // Escape key handling
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (timeline.mode === 'historical') {
        returnToLive();
        return;
      }
      const chatOverlay = document.getElementById('chat-overlay');
      if (chatOverlay && chatOverlay.style.display !== 'none') {
        closeAgentChat();
        return;
      }
      const approvalFloat = document.getElementById('approval-float');
      if (approvalFloat && approvalFloat.style.display !== 'none') {
        closeApprovalPanel();
        return;
      }
      const graphOverlay = document.getElementById('graph-overlay');
      if (graphOverlay && graphOverlay.style.display !== 'none') {
        closeGraphView();
      } else {
        closeModal();
      }
    }
  });

  // Escape key closes overlays
  // (tab navigation removed — chat is per-agent overlay, approvals float from human card)
});
