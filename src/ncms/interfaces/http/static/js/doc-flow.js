// ── NCMS Dashboard — Document Flow Graph (D3 DAG) ───────────────────
// Renders a horizontal flow of documents in a project showing the
// traceability chain with typed icons, score gauges, and link arrows.
// Compact card-based layout instead of spread-out circle nodes.

const DOC_TYPE_CONFIG = {
  research:  { icon: '\uD83D\uDD0D', color: '#10b981', label: 'Research',  order: 0 },
  prd:       { icon: '\uD83D\uDCCB', color: '#22c55e', label: 'PRD',       order: 1 },
  manifest:  { icon: '\uD83D\uDCE6', color: '#f97316', label: 'Manifest',  order: 2 },
  design:    { icon: '\uD83D\uDCD0', color: '#f59e0b', label: 'Design',    order: 3 },
  review:    { icon: '\uD83D\uDEE1', color: '#a78bfa', label: 'Review',    order: 4 },
  contract:  { icon: '\uD83D\uDCC4', color: '#8b5cf6', label: 'Contract',  order: 5 },
};

const LINK_TYPE_CONFIG = {
  derived_from: { color: '#58a6ff', dash: '',    label: 'derived from' },
  reviews:      { color: '#a78bfa', dash: '6,3', label: 'reviews' },
  supersedes:   { color: '#f59e0b', dash: '3,3', label: 'supersedes' },
  cites:        { color: '#6e7681', dash: '2,2', label: 'cites' },
  approved_by:  { color: '#10b981', dash: '',    label: 'approved by' },
};

// Card dimensions
const CARD_W = 170;
const CARD_H = 72;
const CARD_GAP_X = 50;
const CARD_GAP_Y = 16;
const MARGIN = { top: 10, right: 16, bottom: 10, left: 16 };

function renderDocFlowGraph(containerId, documents, links, reviewScores) {
  const container = document.getElementById(containerId);
  if (!container || !documents || documents.length === 0) return;
  container.innerHTML = '';

  // Score lookup
  const scoreMap = {};
  for (const s of (reviewScores || [])) {
    if (!scoreMap[s.document_id]) scoreMap[s.document_id] = [];
    if (s.score != null) scoreMap[s.document_id].push(s.score);
  }

  // Build nodes sorted by type order then creation time
  const nodes = documents.map(d => {
    const cfg = DOC_TYPE_CONFIG[d.doc_type] || { icon: '\uD83D\uDCC4', color: '#64748b', label: d.doc_type || 'Doc', order: 9 };
    const scores = scoreMap[d.id] || [];
    const avgScore = scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
    return {
      id: d.id,
      title: d.title || 'Untitled',
      doc_type: d.doc_type || 'unknown',
      version: d.version || 1,
      from_agent: d.from_agent || '',
      size_bytes: d.size_bytes || 0,
      entity_count: d.entity_count || 0,
      icon: cfg.icon,
      color: cfg.color,
      label: cfg.label,
      order: cfg.order,
      avgScore,
    };
  }).sort((a, b) => a.order - b.order);

  const docMap = {};
  nodes.forEach(n => { docMap[n.id] = n; });

  // Layout: horizontal flow, stack versions vertically
  const columns = [];
  const colMap = {};
  for (const n of nodes) {
    const key = n.doc_type + (n.version > 1 ? '' : ''); // group by type
    if (!colMap[n.doc_type]) {
      colMap[n.doc_type] = { type: n.doc_type, nodes: [] };
      columns.push(colMap[n.doc_type]);
    }
    colMap[n.doc_type].nodes.push(n);
  }

  // Assign positions
  let maxRows = 1;
  columns.forEach(col => { maxRows = Math.max(maxRows, col.nodes.length); });

  const totalW = columns.length * (CARD_W + CARD_GAP_X) - CARD_GAP_X + MARGIN.left + MARGIN.right;
  const totalH = maxRows * (CARD_H + CARD_GAP_Y) - CARD_GAP_Y + MARGIN.top + MARGIN.bottom;
  const width = Math.max(totalW, container.clientWidth || 400);
  const height = Math.max(totalH, 92);

  // Center columns horizontally
  const startX = MARGIN.left + (width - totalW) / 2;

  columns.forEach((col, ci) => {
    const colX = startX + ci * (CARD_W + CARD_GAP_X);
    col.nodes.forEach((n, ri) => {
      n.x = colX;
      n.y = MARGIN.top + ri * (CARD_H + CARD_GAP_Y);
    });
  });

  // Build edges from explicit links
  const edges = [];
  const edgeSet = new Set(); // deduplicate
  for (const link of (links || [])) {
    const src = docMap[link.source_doc_id];
    const tgt = docMap[link.target_doc_id];
    if (src && tgt) {
      const cfg = LINK_TYPE_CONFIG[link.link_type] || { color: '#6e7681', dash: '', label: link.link_type };
      const key = src.id + '->' + tgt.id;
      if (!edgeSet.has(key)) {
        edgeSet.add(key);
        edges.push({ source: src, target: tgt, link_type: link.link_type, ...cfg });
      }
    }
  }

  // Infer flow arrows between adjacent columns if no explicit link exists
  // This connects e.g. PRD → Manifest when they have no derived_from link
  for (let ci = 0; ci < columns.length - 1; ci++) {
    const fromCol = columns[ci];
    const toCol = columns[ci + 1];
    // Check if any edge already connects these columns
    const hasEdge = edges.some(e =>
      fromCol.nodes.some(n => n.id === e.source.id) &&
      toCol.nodes.some(n => n.id === e.target.id)
    );
    if (!hasEdge && fromCol.nodes.length > 0 && toCol.nodes.length > 0) {
      // Add a faint inferred flow arrow from first node of each column
      edges.push({
        source: fromCol.nodes[0],
        target: toCol.nodes[0],
        link_type: '_flow',
        color: '#3b4252',
        dash: '4,4',
        label: '',
      });
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
  const markerColors = new Set(edges.map(e => e.color));
  markerColors.forEach(c => {
    defs.append('marker')
      .attr('id', 'arr-' + c.replace('#', ''))
      .attr('viewBox', '0 0 8 6')
      .attr('refX', 8).attr('refY', 3)
      .attr('markerWidth', 7).attr('markerHeight', 5)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,0 L8,3 L0,6 Z')
      .attr('fill', c);
  });

  // Draw edges as curved paths
  svg.selectAll('.doc-flow-edge')
    .data(edges)
    .enter()
    .append('path')
    .attr('class', 'doc-flow-edge')
    .attr('d', d => {
      const sx = d.source.x + CARD_W;
      const sy = d.source.y + CARD_H / 2;
      const tx = d.target.x;
      const ty = d.target.y + CARD_H / 2;
      const mx = (sx + tx) / 2;
      return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
    })
    .attr('fill', 'none')
    .attr('stroke', d => d.color)
    .attr('stroke-width', d => d.link_type === '_flow' ? 1 : 1.5)
    .attr('stroke-dasharray', d => d.dash || null)
    .attr('opacity', d => d.link_type === '_flow' ? 0.4 : 0.8)
    .attr('marker-end', d => 'url(#arr-' + d.color.replace('#', '') + ')');

  // Edge labels (skip inferred flow)
  svg.selectAll('.doc-flow-edge-label')
    .data(edges.filter(e => e.label))
    .enter()
    .append('text')
    .attr('class', 'doc-flow-edge-label')
    .attr('x', d => (d.source.x + CARD_W + d.target.x) / 2)
    .attr('y', d => (d.source.y + d.target.y) / 2 + CARD_H / 2 - 8)
    .attr('text-anchor', 'middle')
    .attr('fill', d => d.color)
    .attr('font-size', '9px')
    .attr('opacity', 0.6)
    .text(d => d.label);

  // Draw node cards
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

  // Card background
  nodeGroup.append('rect')
    .attr('width', CARD_W)
    .attr('height', CARD_H)
    .attr('rx', 8).attr('ry', 8)
    .attr('fill', d => d.color + '12')
    .attr('stroke', d => d.color + '50')
    .attr('stroke-width', 1.5);

  // Left color bar
  nodeGroup.append('rect')
    .attr('width', 4)
    .attr('height', CARD_H - 8)
    .attr('x', 0).attr('y', 4)
    .attr('rx', 2)
    .attr('fill', d => d.color);

  // Type label + icon (top line)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 16)
    .attr('fill', d => d.color)
    .attr('font-size', '10px')
    .attr('font-weight', '700')
    .attr('text-transform', 'uppercase')
    .attr('letter-spacing', '0.5px')
    .text(d => d.icon + ' ' + d.label + (d.version > 1 ? ' v' + d.version : ''));

  // Title (middle, wraps to ~20 chars)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 33)
    .attr('fill', 'var(--text-primary)')
    .attr('font-size', '11px')
    .attr('font-weight', '500')
    .text(d => {
      // Show the meaningful part of the title (strip common prefix)
      let t = d.title;
      // Remove leading topic that repeats across all docs
      const dashIdx = t.indexOf(' \u2014 ');
      if (dashIdx > 0 && dashIdx < t.length - 5) t = t.substring(dashIdx + 3);
      return t.length > 24 ? t.substring(0, 22) + '..' : t;
    });

  // Agent + size (bottom line)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 48)
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => {
      const parts = [d.from_agent];
      if (d.size_bytes > 0) parts.push((d.size_bytes / 1024).toFixed(1) + 'KB');
      if (d.entity_count > 0) parts.push(d.entity_count + ' ent');
      const full = parts.join(' \u00B7 ');
      return full.length > 24 ? full.substring(0, 22) + '..' : full;
    });

  // Score badge (bottom-right corner)
  nodeGroup.each(function (d) {
    if (d.avgScore == null) return;
    const color = d.avgScore >= 80 ? '#10b981' : d.avgScore >= 60 ? '#f59e0b' : '#ef4444';
    const g = d3.select(this);
    g.append('rect')
      .attr('x', CARD_W - 38).attr('y', CARD_H - 20)
      .attr('width', 34).attr('height', 16)
      .attr('rx', 3)
      .attr('fill', color + '25')
      .attr('stroke', color + '60')
      .attr('stroke-width', 1);
    g.append('text')
      .attr('x', CARD_W - 21).attr('y', CARD_H - 8)
      .attr('text-anchor', 'middle')
      .attr('fill', color)
      .attr('font-size', '10px')
      .attr('font-weight', '700')
      .text(d.avgScore + '%');
  });
}
