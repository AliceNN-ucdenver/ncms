// ── NCMS Dashboard — Document Flow Graph (D3 DAG) ───────────────────
// Renders a directed acyclic graph of documents in a project,
// showing the traceability chain: Research -> PRD -> Design -> Review -> Contract
// with typed icons, score gauges, and version chains.

const DOC_TYPE_CONFIG = {
  research:  { icon: '\uD83D\uDD0D', color: '#10b981', label: 'Research' },
  prd:       { icon: '\uD83D\uDCCB', color: '#22c55e', label: 'PRD' },
  manifest:  { icon: '\uD83D\uDCE6', color: '#f97316', label: 'Manifest' },
  design:    { icon: '\uD83D\uDCD0', color: '#f59e0b', label: 'Design' },
  review:    { icon: '\uD83D\uDEE1', color: '#a78bfa', label: 'Review' },
  contract:  { icon: '\uD83D\uDCC4', color: '#8b5cf6', label: 'Contract' },
};

const LINK_TYPE_CONFIG = {
  derived_from: { color: '#58a6ff', dash: '', label: 'derived from' },
  reviews:      { color: '#a78bfa', dash: '6,3', label: 'reviews' },
  supersedes:   { color: '#f59e0b', dash: '3,3', label: 'supersedes' },
  cites:        { color: '#6e7681', dash: '2,2', label: 'cites' },
  approved_by:  { color: '#10b981', dash: '', label: 'approved by' },
};

/**
 * Render a D3 document flow DAG inside a container element.
 * @param {string} containerId - DOM element ID to render into
 * @param {Object[]} documents - Array of document objects from project summary
 * @param {Object[]} links - Array of document_links from project summary
 * @param {Object[]} reviewScores - Array of review scores
 */
function renderDocFlowGraph(containerId, documents, links, reviewScores) {
  const container = document.getElementById(containerId);
  if (!container || !documents || documents.length === 0) return;

  // Clear previous
  container.innerHTML = '';

  const width = container.clientWidth || 700;
  const height = Math.max(250, documents.length * 55 + 60);

  // Build score lookup: doc_id -> avg score
  const scoreMap = {};
  for (const s of (reviewScores || [])) {
    const did = s.document_id;
    if (!scoreMap[did]) scoreMap[did] = [];
    if (s.score != null) scoreMap[did].push(s.score);
  }

  // Build node data
  const docMap = {};
  const nodes = documents.map((d, i) => {
    const cfg = DOC_TYPE_CONFIG[d.doc_type] || { icon: '\uD83D\uDCC4', color: '#64748b', label: d.doc_type || 'Doc' };
    const scores = scoreMap[d.id] || [];
    const avgScore = scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
    const node = {
      id: d.id,
      title: d.title || 'Untitled',
      doc_type: d.doc_type || 'unknown',
      version: d.version || 1,
      from_agent: d.from_agent || '',
      size_bytes: d.size_bytes || 0,
      content_hash: d.content_hash || '',
      entity_count: d.entity_count || 0,
      created_at: d.created_at || '',
      icon: cfg.icon,
      color: cfg.color,
      label: cfg.label,
      avgScore: avgScore,
      index: i,
    };
    docMap[d.id] = node;
    return node;
  });

  // Assign x/y positions as a left-to-right DAG by doc_type order
  const typeOrder = ['research', 'prd', 'manifest', 'design', 'review', 'contract'];
  const typeGroups = {};
  for (const n of nodes) {
    const t = n.doc_type;
    if (!typeGroups[t]) typeGroups[t] = [];
    typeGroups[t].push(n);
  }

  const margin = { top: 30, right: 30, bottom: 30, left: 30 };
  const usableW = width - margin.left - margin.right;
  const usableH = height - margin.top - margin.bottom;

  // Count columns
  const activeCols = typeOrder.filter(t => typeGroups[t] && typeGroups[t].length > 0);
  const colWidth = activeCols.length > 1 ? usableW / (activeCols.length - 1) : usableW / 2;

  for (let ci = 0; ci < activeCols.length; ci++) {
    const col = activeCols[ci];
    const group = typeGroups[col];
    const rowH = usableH / (group.length + 1);
    for (let ri = 0; ri < group.length; ri++) {
      group[ri].x = margin.left + ci * colWidth;
      group[ri].y = margin.top + (ri + 1) * rowH;
    }
  }

  // Build edge data — only for links where both docs are in this project
  const edges = [];
  for (const link of (links || [])) {
    const src = docMap[link.source_doc_id];
    const tgt = docMap[link.target_doc_id];
    if (src && tgt) {
      const cfg = LINK_TYPE_CONFIG[link.link_type] || { color: '#6e7681', dash: '', label: link.link_type };
      edges.push({ source: src, target: tgt, link_type: link.link_type, ...cfg });
    }
  }

  // Create SVG
  const svg = d3.select('#' + containerId)
    .append('svg')
    .attr('width', width)
    .attr('height', height)
    .attr('class', 'doc-flow-svg');

  // Arrowhead markers
  const defs = svg.append('defs');
  for (const [type, cfg] of Object.entries(LINK_TYPE_CONFIG)) {
    defs.append('marker')
      .attr('id', 'arrow-' + type)
      .attr('viewBox', '0 0 10 6')
      .attr('refX', 10)
      .attr('refY', 3)
      .attr('markerWidth', 8)
      .attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,0 L10,3 L0,6 Z')
      .attr('fill', cfg.color);
  }

  // Draw edges
  svg.selectAll('.doc-flow-edge')
    .data(edges)
    .enter()
    .append('line')
    .attr('class', 'doc-flow-edge')
    .attr('x1', d => d.source.x)
    .attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x)
    .attr('y2', d => d.target.y)
    .attr('stroke', d => d.color)
    .attr('stroke-width', 1.5)
    .attr('stroke-dasharray', d => d.dash || null)
    .attr('marker-end', d => 'url(#arrow-' + d.link_type + ')');

  // Edge labels
  svg.selectAll('.doc-flow-edge-label')
    .data(edges)
    .enter()
    .append('text')
    .attr('class', 'doc-flow-edge-label')
    .attr('x', d => (d.source.x + d.target.x) / 2)
    .attr('y', d => (d.source.y + d.target.y) / 2 - 6)
    .attr('text-anchor', 'middle')
    .attr('fill', d => d.color)
    .attr('font-size', '9px')
    .attr('opacity', 0.7)
    .text(d => d.label);

  // Draw nodes
  const nodeGroup = svg.selectAll('.doc-flow-node')
    .data(nodes)
    .enter()
    .append('g')
    .attr('class', 'doc-flow-node')
    .attr('transform', d => `translate(${d.x},${d.y})`)
    .style('cursor', 'pointer')
    .on('click', (event, d) => {
      if (typeof openDocumentViewer === 'function') openDocumentViewer(d.id);
    });

  // Node background circle
  nodeGroup.append('circle')
    .attr('r', 22)
    .attr('fill', d => d.color + '20')
    .attr('stroke', d => d.color)
    .attr('stroke-width', 2);

  // Score gauge ring (outer arc)
  nodeGroup.each(function (d) {
    if (d.avgScore == null) return;
    const scoreColor = d.avgScore >= 80 ? '#10b981' : d.avgScore >= 60 ? '#f59e0b' : '#ef4444';
    const arc = d3.arc()
      .innerRadius(24)
      .outerRadius(27)
      .startAngle(0)
      .endAngle((d.avgScore / 100) * 2 * Math.PI);
    d3.select(this).append('path')
      .attr('d', arc())
      .attr('fill', scoreColor)
      .attr('opacity', 0.8);
    // Score text
    d3.select(this).append('text')
      .attr('y', -30)
      .attr('text-anchor', 'middle')
      .attr('fill', scoreColor)
      .attr('font-size', '10px')
      .attr('font-weight', '700')
      .text(d.avgScore + '%');
  });

  // Type icon (emoji)
  nodeGroup.append('text')
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .attr('font-size', '18px')
    .text(d => d.icon);

  // Title below node
  nodeGroup.append('text')
    .attr('y', 36)
    .attr('text-anchor', 'middle')
    .attr('fill', 'var(--text-primary)')
    .attr('font-size', '11px')
    .attr('font-weight', '500')
    .text(d => {
      const t = d.title.length > 25 ? d.title.substring(0, 23) + '..' : d.title;
      return d.version > 1 ? t + ' v' + d.version : t;
    });

  // Agent + size badge below title
  nodeGroup.append('text')
    .attr('y', 48)
    .attr('text-anchor', 'middle')
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => {
      const parts = [];
      if (d.from_agent) parts.push(d.from_agent);
      if (d.size_bytes > 0) parts.push((d.size_bytes / 1024).toFixed(1) + 'KB');
      return parts.join(' \u00B7 ');
    });

  // Column headers
  svg.selectAll('.doc-flow-col-header')
    .data(activeCols)
    .enter()
    .append('text')
    .attr('class', 'doc-flow-col-header')
    .attr('x', (d, i) => margin.left + i * colWidth)
    .attr('y', 16)
    .attr('text-anchor', 'middle')
    .attr('fill', d => (DOC_TYPE_CONFIG[d] || {}).color || '#64748b')
    .attr('font-size', '11px')
    .attr('font-weight', '600')
    .attr('text-transform', 'uppercase')
    .attr('letter-spacing', '0.5px')
    .text(d => (DOC_TYPE_CONFIG[d] || {}).label || d);
}
