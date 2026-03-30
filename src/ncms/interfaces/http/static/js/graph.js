// ── NCMS Dashboard — Knowledge Graph Visualization ───────────────────
// D3 force-directed graph, entity detail panel, filters, topics.

const ENTITY_COLORS = {
  technology: '#39d2c0',
  service: '#58a6ff',
  endpoint: '#3fb950',
  keyword: '#f778ba',
  concept: '#bc8cff',
  database: '#d29922',
  component: '#58a6ff',
  table: '#d29922',
  protocol: '#39d2c0',
  library: '#bc8cff',
  framework: '#39d2c0',
};
const DEFAULT_ENTITY_COLOR = '#8b949e';

const MEMORY_COLORS = {
  fact: '#6e7681',
  insight: '#fbbf24',
  'code-snippet': '#39d2c0',
  configuration: '#d29922',
  'architecture-decision': '#bc8cff',
};
const DEFAULT_MEMORY_COLOR = '#6e7681';

const NODE_TYPE_COLORS = {
  atomic: '#58a6ff',
  entity_state: '#3fb950',
  episode: '#d29922',
  abstract: '#bc8cff',
};

let graphSimulation = null;
let graphData = null;
let graphFilterState = {};

function getEntityColor(type) {
  return ENTITY_COLORS[type] || DEFAULT_ENTITY_COLOR;
}

function getMemoryColor(type, nodeType) {
  if (nodeType && NODE_TYPE_COLORS[nodeType]) return NODE_TYPE_COLORS[nodeType];
  return MEMORY_COLORS[type] || DEFAULT_MEMORY_COLOR;
}

function getNodeColor(d) {
  return d.group === 'entity' ? getEntityColor(d.type) : getMemoryColor(d.type, d.node_type);
}

// ── Open / Close ────────────────────────────────────────────────────
function openGraphView() {
  const overlay = document.getElementById('graph-overlay');
  overlay.style.display = 'flex';
  loadGraphData();
}

function closeGraphView() {
  const overlay = document.getElementById('graph-overlay');
  overlay.style.display = 'none';
  if (graphSimulation) {
    graphSimulation.stop();
    graphSimulation = null;
  }
  const svg = d3.select('#graph-svg');
  svg.selectAll('*').remove();
  document.getElementById('graph-detail-panel').style.display = 'none';
  document.getElementById('topics-panel').style.display = 'none';
  document.getElementById('graph-search').value = '';
}

// ── Load Data ───────────────────────────────────────────────────────
async function loadGraphData() {
  try {
    const resp = await fetch('/api/graph');
    graphData = await resp.json();
    buildFilters(graphData.nodes);
    buildLegend(graphData.nodes);
    renderGraph(graphData);
  } catch (e) {
    console.error('Failed to load graph data:', e);
  }
}

// ── Filters ─────────────────────────────────────────────────────────
function buildFilters(nodes) {
  const entityTypes = [...new Set(nodes.filter(n => n.group === 'entity').map(n => n.type))].sort();
  const container = document.getElementById('graph-filters');

  graphFilterState = {};
  entityTypes.forEach(t => { graphFilterState[t] = true; });
  graphFilterState['_memory'] = true;

  let html = '';
  entityTypes.forEach(t => {
    const color = getEntityColor(t);
    html += `<label class="graph-filter-item">
      <input type="checkbox" checked onchange="toggleFilter('${t}', this.checked)">
      <span class="graph-filter-dot" style="background:${color}"></span>
      ${t}
    </label>`;
  });
  html += `<label class="graph-filter-item">
    <input type="checkbox" checked onchange="toggleFilter('_memory', this.checked)">
    <span class="graph-filter-dot" style="background:#6e7681"></span>
    memories
  </label>`;

  container.innerHTML = html;
}

function toggleFilter(type, checked) {
  graphFilterState[type] = checked;
  applyFilters();
}

function applyFilters() {
  const svg = d3.select('#graph-svg');

  svg.selectAll('.graph-node').each(function(d) {
    let visible;
    if (d.group === 'memory') {
      visible = graphFilterState['_memory'] !== false;
    } else {
      visible = graphFilterState[d.type] !== false;
    }
    d3.select(this).style('opacity', visible ? 1 : 0.05)
      .style('pointer-events', visible ? 'all' : 'none');
  });

  svg.selectAll('.graph-link').each(function(d) {
    const sourceVisible = d.source.group === 'memory'
      ? graphFilterState['_memory'] !== false
      : graphFilterState[d.source.type] !== false;
    const targetVisible = d.target.group === 'memory'
      ? graphFilterState['_memory'] !== false
      : graphFilterState[d.target.type] !== false;
    d3.select(this).style('opacity', (sourceVisible && targetVisible) ? 0.3 : 0.02);
  });
}

// ── Legend ───────────────────────────────────────────────────────────
function buildLegend(nodes) {
  const entityTypes = [...new Set(nodes.filter(n => n.group === 'entity').map(n => n.type))].sort();
  const legend = document.getElementById('graph-legend');
  let html = '<span style="font-size:10px;color:var(--text-muted);font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Legend:</span>';

  entityTypes.forEach(t => {
    html += `<span class="graph-legend-item">
      <span class="graph-legend-swatch" style="background:${getEntityColor(t)}"></span>${t}
    </span>`;
  });

  const hasNodeTypes = nodes.some(n => n.group === 'memory' && n.node_type);
  if (hasNodeTypes) {
    Object.entries(NODE_TYPE_COLORS).forEach(([nt, color]) => {
      html += `<span class="graph-legend-item">
        <span class="graph-legend-swatch" style="background:${color};width:7px;height:7px"></span>${nt.replace(/_/g, ' ')}
      </span>`;
    });
  } else {
    html += `<span class="graph-legend-item">
      <span class="graph-legend-swatch" style="background:#6e7681;width:7px;height:7px"></span>memory
    </span>`;
  }
  html += `<span class="graph-legend-item">
    <span class="graph-legend-swatch" style="background:#fbbf24;width:7px;height:7px;box-shadow:0 0 6px #fbbf24"></span>insight
  </span>`;

  html += `<span class="graph-legend-item">
    <span class="graph-legend-line" style="background:#58a6ff"></span>entity→entity
  </span>`;
  html += `<span class="graph-legend-item">
    <span class="graph-legend-line" style="background:#6e7681;border-top:1px dashed #6e7681;height:0"></span>entity→memory
  </span>`;

  legend.innerHTML = html;
}

// ── D3 Rendering ────────────────────────────────────────────────────
function renderGraph(data) {
  const svgEl = document.getElementById('graph-svg');
  const rect = svgEl.getBoundingClientRect();
  const width = rect.width || 800;
  const height = rect.height || 600;

  const svg = d3.select('#graph-svg');
  svg.selectAll('*').remove();

  const defs = svg.append('defs');
  const glowFilter = defs.append('filter').attr('id', 'glow-insight');
  glowFilter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
  const merge = glowFilter.append('feMerge');
  merge.append('feMergeNode').attr('in', 'blur');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');

  const g = svg.append('g');

  const zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on('zoom', (event) => {
      g.attr('transform', event.transform);
    });
  svg.call(zoom);

  const link = g.selectAll('.graph-link')
    .data(data.links)
    .join('line')
    .attr('class', 'graph-link')
    .attr('stroke', d => d.type === 'linked' ? '#6e7681' : '#58a6ff')
    .attr('stroke-width', d => d.type === 'linked' ? 0.8 : 1.2)
    .attr('stroke-dasharray', d => d.type === 'linked' ? '3,3' : 'none')
    .attr('opacity', 0.3);

  const node = g.selectAll('.graph-node')
    .data(data.nodes)
    .join('g')
    .attr('class', 'graph-node')
    .style('cursor', 'pointer')
    .on('click', (event, d) => onNodeClick(d))
    .call(d3.drag()
      .on('start', dragStarted)
      .on('drag', dragged)
      .on('end', dragEnded));

  node.filter(d => d.group === 'entity')
    .append('circle')
    .attr('r', 8)
    .attr('fill', d => getEntityColor(d.type))
    .attr('stroke', d => d.type === 'keyword' ? '#f778ba' : 'none')
    .attr('stroke-width', d => d.type === 'keyword' ? 2 : 0)
    .attr('stroke-dasharray', d => d.type === 'keyword' ? '3,2' : 'none');

  node.filter(d => d.group === 'entity')
    .append('text')
    .text(d => d.name.length > 18 ? d.name.slice(0, 15) + '...' : d.name)
    .attr('x', 12)
    .attr('y', 4)
    .attr('fill', '#e6edf3')
    .attr('font-size', '9px')
    .style('pointer-events', 'none');

  node.filter(d => d.group === 'memory')
    .append('circle')
    .attr('r', 5)
    .attr('fill', d => d.is_insight ? '#fbbf24' : getMemoryColor(d.type, d.node_type))
    .attr('filter', d => d.is_insight ? 'url(#glow-insight)' : 'none')
    .attr('opacity', 0.8);

  node.filter(d => d.group === 'memory' && (d.has_contradictions || d.is_contradicted))
    .append('circle')
    .attr('r', 7)
    .attr('fill', 'none')
    .attr('stroke', '#f85149')
    .attr('stroke-width', 1.5)
    .attr('stroke-dasharray', '2,2');

  graphSimulation = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d => d.id).distance(60))
    .force('charge', d3.forceManyBody().strength(-120))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => d.group === 'entity' ? 16 : 10))
    .on('tick', () => {
      link
        .attr('x1', d => d.source.x)
        .attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x)
        .attr('y2', d => d.target.y);
      node.attr('transform', d => `translate(${d.x},${d.y})`);
    });
}

// ── Drag Handlers ───────────────────────────────────────────────────
function dragStarted(event, d) {
  if (!event.active) graphSimulation.alphaTarget(0.3).restart();
  d.fx = d.x;
  d.fy = d.y;
}

function dragged(event, d) {
  d.fx = event.x;
  d.fy = event.y;
}

function dragEnded(event, d) {
  if (!event.active) graphSimulation.alphaTarget(0);
  d.fx = null;
  d.fy = null;
}

// ── Search / Filter ─────────────────────────────────────────────────
function filterGraphNodes(query) {
  if (!graphData) return;
  const q = query.toLowerCase().trim();
  const svg = d3.select('#graph-svg');

  if (!q) {
    svg.selectAll('.graph-node').style('opacity', 1);
    svg.selectAll('.graph-link').style('opacity', 0.3);
    applyFilters();
    return;
  }

  svg.selectAll('.graph-node').each(function(d) {
    const name = (d.name || '').toLowerCase();
    const match = name.includes(q);
    d3.select(this).style('opacity', match ? 1 : 0.08);
  });

  svg.selectAll('.graph-link').style('opacity', 0.05);
}

// ── Node Click ──────────────────────────────────────────────────────
function onNodeClick(d) {
  if (d.group === 'entity') {
    showEntityDetail(d);
  } else {
    showMemoryDetail(d);
  }
}

async function showEntityDetail(d) {
  const panel = document.getElementById('graph-detail-panel');
  panel.style.display = 'block';
  panel.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:40px">Loading...</div>';

  try {
    const resp = await fetch(`/api/graph/entity/${d.id}`);
    if (!resp.ok) throw new Error('Entity not found');
    const detail = await resp.json();
    const entity = detail.entity;
    const color = getEntityColor(entity.type);

    let html = `<button class="graph-detail-close" onclick="document.getElementById('graph-detail-panel').style.display='none'">&times;</button>`;
    html += `<div class="graph-detail-name">${escapeHtml(entity.name)}</div>`;
    html += `<span class="graph-detail-type" style="background:${color}20;color:${color};border:1px solid ${color}50">${entity.type}</span>`;

    if (entity.type === 'keyword') {
      html += ` <span class="graph-badge badge-keyword-bridge">keyword bridge</span>`;
    }

    const attrs = entity.attributes || {};
    const attrKeys = Object.keys(attrs);
    if (attrKeys.length > 0) {
      html += `<div class="graph-detail-section">
        <div class="graph-detail-section-title">Attributes</div>`;
      attrKeys.forEach(k => {
        html += `<div class="graph-detail-attr">
          <span class="graph-detail-attr-key">${escapeHtml(k)}</span>
          <span class="graph-detail-attr-val">${escapeHtml(String(attrs[k]))}</span>
        </div>`;
      });
      html += '</div>';
    }

    if (detail.connected_memories.length > 0) {
      html += `<div class="graph-detail-section">
        <div class="graph-detail-section-title">Connected Memories (${detail.connected_memories.length})</div>
        <div class="graph-connected-memories-list">`;
      detail.connected_memories.forEach(m => {
        let badges = '';
        if (m.is_insight) badges += '<span class="graph-badge badge-insight">insight</span> ';
        if (m.has_contradictions) badges += '<span class="graph-badge badge-contradiction">contradicts</span> ';
        if (m.is_contradicted) badges += '<span class="graph-badge badge-contradiction">contradicted</span> ';
        html += `<div class="graph-detail-memory-item">
          ${escapeHtml(m.content)}
          <div class="graph-detail-memory-meta">
            ${badges}
            <span style="font-size:10px;color:var(--text-muted)">${m.type}${m.source_agent ? ' · ' + escapeHtml(m.source_agent) : ''}</span>
            ${m.domains && m.domains.length > 0 ? m.domains.map(d => `<span style="font-size:9px;padding:1px 5px;background:rgba(88,166,255,0.1);color:var(--accent-blue);border-radius:3px">${escapeHtml(d)}</span>`).join('') : ''}
          </div>
        </div>`;
      });
      html += '</div></div>';
    }

    if (detail.connected_entities.length > 0) {
      html += `<div class="graph-detail-section">
        <div class="graph-detail-section-title">Connected Entities (${detail.connected_entities.length})</div>`;
      detail.connected_entities.forEach(e => {
        const eColor = getEntityColor(e.type);
        const dirArrow = e.direction === 'outgoing' ? '→' : '←';
        html += `<div class="graph-detail-entity-item" onclick="navigateToEntity('${e.id}')">
          <span class="graph-detail-entity-dot" style="background:${eColor}"></span>
          <span class="graph-detail-entity-name">${escapeHtml(e.name)}</span>
          <span class="graph-detail-entity-rel">${escapeHtml(e.relationship_type)}</span>
          <span class="graph-detail-entity-dir">${dirArrow}</span>
        </div>`;
      });
      html += '</div>';
    }

    panel.innerHTML = html;
  } catch (e) {
    panel.innerHTML = `<button class="graph-detail-close" onclick="document.getElementById('graph-detail-panel').style.display='none'">&times;</button>
      <div style="color:var(--accent-red);padding:20px">Failed to load entity details</div>`;
  }
}

function showMemoryDetail(d) {
  const panel = document.getElementById('graph-detail-panel');
  panel.style.display = 'block';

  const color = getMemoryColor(d.type);
  let html = `<button class="graph-detail-close" onclick="document.getElementById('graph-detail-panel').style.display='none'">&times;</button>`;
  html += `<div class="graph-detail-name">${escapeHtml(d.name)}</div>`;
  html += `<span class="graph-detail-type" style="background:${color}20;color:${color};border:1px solid ${color}50">${d.type}</span>`;

  if (d.is_insight) {
    html += ` <span class="graph-badge badge-insight">consolidation insight</span>`;
  }
  if (d.has_contradictions) {
    html += ` <span class="graph-badge badge-contradiction">has contradictions</span>`;
  }
  if (d.is_contradicted) {
    html += ` <span class="graph-badge badge-contradiction">contradicted</span>`;
  }

  if (d.source_agent) {
    html += `<div class="graph-detail-section">
      <div class="graph-detail-section-title">Source</div>
      <div class="graph-detail-attr">
        <span class="graph-detail-attr-key">Agent</span>
        <span class="graph-detail-attr-val">${escapeHtml(d.source_agent)}</span>
      </div>
    </div>`;
  }

  if (d.domains && d.domains.length > 0) {
    html += `<div class="graph-detail-section">
      <div class="graph-detail-section-title">Domains</div>
      <div style="display:flex;gap:4px;flex-wrap:wrap">
        ${d.domains.map(dm => `<span class="domain-tag">${escapeHtml(dm)}</span>`).join('')}
      </div>
    </div>`;
  }

  panel.innerHTML = html;
}

function navigateToEntity(entityId) {
  if (!graphData) return;
  const node = graphData.nodes.find(n => n.id === entityId);
  if (node) {
    showEntityDetail(node);
  }
}

// ── Topics Panel ─────────────────────────────────────────────────────
function toggleTopicsPanel() {
  const panel = document.getElementById('topics-panel');
  if (panel.style.display === 'none') {
    loadTopics();
    panel.style.display = 'block';
  } else {
    panel.style.display = 'none';
  }
}

async function loadTopics() {
  const panel = document.getElementById('topics-panel');
  panel.innerHTML = '<div class="topics-empty">Loading...</div>';
  try {
    const resp = await fetch('/api/topics');
    const data = await resp.json();
    renderTopicsPanel(data);
  } catch (e) {
    panel.innerHTML = '<div class="topics-empty">Failed to load topics</div>';
  }
}

function renderTopicsPanel(data) {
  const panel = document.getElementById('topics-panel');
  const domains = data.domains || {};
  const universal = data.universal_labels || [];
  const domainKeys = Object.keys(domains).sort();

  let html = `<div class="topics-panel-header">
    <span class="topics-panel-title">Cached Entity Topics</span>
    <button class="topics-panel-close" onclick="document.getElementById('topics-panel').style.display='none'">&times;</button>
  </div>`;

  if (domainKeys.length > 0) {
    for (const domain of domainKeys) {
      const labels = domains[domain];
      html += `<div class="topics-domain">
        <div class="topics-domain-name">${escapeHtml(domain)}</div>
        <div class="topics-label-list">
          ${labels.map(l => `<span class="topics-label">${escapeHtml(l)}</span>`).join('')}
        </div>
      </div>`;
    }
  } else {
    html += '<div class="topics-empty">No domain-specific topics cached</div>';
  }

  html += `<div class="topics-domain">
    <div class="topics-domain-name" style="color:var(--text-muted)">Universal (always included)</div>
    <div class="topics-label-list">
      ${universal.map(l => `<span class="topics-label universal">${escapeHtml(l)}</span>`).join('')}
    </div>
  </div>`;

  panel.innerHTML = html;
}
