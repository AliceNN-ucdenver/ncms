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
const CARD_H_BASE = 82;
const CARD_H_PER_SCORE = 14;  // extra height per reviewer score line
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

  // Build per-reviewer score lookup: doc_id -> [{reviewer, score, round}]
  const reviewerScoreMap = {};
  for (const s of (reviewScores || [])) {
    if (!reviewerScoreMap[s.document_id]) reviewerScoreMap[s.document_id] = [];
    if (s.score != null) {
      reviewerScoreMap[s.document_id].push({
        reviewer: s.reviewer_agent || '?',
        score: s.score,
        round: s.review_round || 1,
      });
    }
  }

  // Build nodes sorted by type order then creation time
  const nodes = documents.map(d => {
    const cfg = DOC_TYPE_CONFIG[d.doc_type] || { icon: '\uD83D\uDCC4', color: '#64748b', label: d.doc_type || 'Doc', order: 9 };
    const scores = scoreMap[d.id] || [];
    const avgScore = scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
    const perReviewer = reviewerScoreMap[d.id] || [];
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
      perReviewer,
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

  // Compute per-node card height (taller when review scores exist)
  for (const n of nodes) {
    n.cardH = CARD_H_BASE + (n.perReviewer.length > 0 ? n.perReviewer.length * CARD_H_PER_SCORE : 0);
  }

  // Assign positions — use max card height per column for vertical stacking
  let maxColH = 0;
  columns.forEach(col => {
    let colH = 0;
    col.nodes.forEach(n => { colH += n.cardH + CARD_GAP_Y; });
    colH -= CARD_GAP_Y;
    maxColH = Math.max(maxColH, colH);
  });

  const totalW = columns.length * (CARD_W + CARD_GAP_X) - CARD_GAP_X + MARGIN.left + MARGIN.right;
  const totalH = maxColH + MARGIN.top + MARGIN.bottom;
  const width = Math.max(totalW, container.clientWidth || 400);
  const height = Math.max(totalH, 92);

  // Center columns horizontally
  const startX = MARGIN.left + (width - totalW) / 2;

  columns.forEach((col, ci) => {
    const colX = startX + ci * (CARD_W + CARD_GAP_X);
    let yOffset = MARGIN.top;
    col.nodes.forEach(n => {
      n.x = colX;
      n.y = yOffset;
      yOffset += n.cardH + CARD_GAP_Y;
    });
  });

  // Build edges from explicit links
  const edges = [];
  const edgeSet = new Set(); // deduplicate
  for (const link of (links || [])) {
    let src = docMap[link.source_doc_id];
    let tgt = docMap[link.target_doc_id];
    if (src && tgt) {
      const cfg = LINK_TYPE_CONFIG[link.link_type] || { color: '#6e7681', dash: '', label: link.link_type };
      // Reverse derived_from so arrows flow left-to-right (producer → product)
      // "PRD derived_from Research" → arrow: Research → PRD
      if (link.link_type === 'derived_from') {
        [src, tgt] = [tgt, src];
      }
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

  // Draw edges — straight lines between card edges
  svg.selectAll('.doc-flow-edge')
    .data(edges)
    .enter()
    .append('line')
    .attr('class', 'doc-flow-edge')
    .attr('x1', d => d.source.x + CARD_W)
    .attr('y1', d => d.source.y + d.source.cardH / 2)
    .attr('x2', d => d.target.x)
    .attr('y2', d => d.target.y + d.target.cardH / 2)
    .attr('stroke', d => d.color)
    .attr('stroke-width', d => d.link_type === '_flow' ? 1 : 1.5)
    .attr('stroke-dasharray', d => d.dash || null)
    .attr('opacity', d => d.link_type === '_flow' ? 0.4 : 0.8)
    .attr('marker-end', d => 'url(#arr-' + d.color.replace('#', '') + ')');

  // Edge labels with background pill (skip inferred flow)
  const labelGroups = svg.selectAll('.doc-flow-edge-label-g')
    .data(edges.filter(e => e.label))
    .enter()
    .append('g')
    .attr('class', 'doc-flow-edge-label-g')
    .attr('transform', d => {
      const mx = (d.source.x + CARD_W + d.target.x) / 2;
      const my = (d.source.y + d.source.cardH / 2 + d.target.y + d.target.cardH / 2) / 2;
      return `translate(${mx},${my})`;
    });

  // Background pill
  labelGroups.append('rect')
    .attr('rx', 3).attr('ry', 3)
    .attr('fill', '#1a1f2e')
    .attr('stroke', d => d.color + '40')
    .attr('stroke-width', 0.5)
    .each(function(d) {
      // Size based on text length
      const w = d.label.length * 5.5 + 10;
      d3.select(this).attr('x', -w/2).attr('y', -8).attr('width', w).attr('height', 14);
    });

  // Label text
  labelGroups.append('text')
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .attr('fill', d => d.color)
    .attr('font-size', '8px')
    .attr('font-weight', '600')
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
    .attr('height', d => d.cardH)
    .attr('rx', 8).attr('ry', 8)
    .attr('fill', '#1a1f2e')
    .attr('stroke', d => d.color + '60')
    .attr('stroke-width', 1.5);

  // Left color bar
  nodeGroup.append('rect')
    .attr('width', 4)
    .attr('height', d => d.cardH - 8)
    .attr('x', 0).attr('y', 4)
    .attr('rx', 2)
    .attr('fill', d => d.color);

  // Type label + icon (line 1, left)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 16)
    .attr('fill', d => d.color)
    .attr('font-size', '10px')
    .attr('font-weight', '700')
    .text(d => d.icon + ' ' + d.label + (d.version > 1 ? ' v' + d.version : ''));

  // Size badge (line 1, right)
  nodeGroup.append('text')
    .attr('x', CARD_W - 10).attr('y', 16)
    .attr('text-anchor', 'end')
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => d.size_bytes > 0 ? (d.size_bytes / 1024).toFixed(1) + ' KB' : '');

  // Title (line 2)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 33)
    .attr('fill', 'var(--text-primary)')
    .attr('font-size', '11px')
    .attr('font-weight', '500')
    .text(d => {
      let t = d.title;
      const dashIdx = t.indexOf(' \u2014 ');
      if (dashIdx > 0 && dashIdx < t.length - 5) t = t.substring(dashIdx + 3);
      return t.length > 24 ? t.substring(0, 22) + '..' : t;
    });

  // Agent (line 3, left)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 48)
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => d.from_agent);

  // Entities (line 3, right)
  nodeGroup.append('text')
    .attr('x', CARD_W - 10).attr('y', 48)
    .attr('text-anchor', 'end')
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => d.entity_count > 0 ? d.entity_count + ' ent' : '');

  // Timestamp (line 4)
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 62)
    .attr('fill', '#a0aec0')
    .attr('font-size', '9px')
    .text(d => {
      if (!d.created_at) return '';
      try {
        const dt = new Date(d.created_at);
        return dt.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } catch { return ''; }
    });

  // Per-reviewer score lines (below timestamp, inside the card)
  nodeGroup.each(function (d) {
    if (d.perReviewer.length === 0) return;
    const g = d3.select(this);
    // Separator line
    g.append('line')
      .attr('x1', 10).attr('x2', CARD_W - 10)
      .attr('y1', CARD_H_BASE - 14).attr('y2', CARD_H_BASE - 14)
      .attr('stroke', 'var(--border)').attr('stroke-width', 0.5);
    // Each reviewer on its own line
    d.perReviewer.forEach((rs, i) => {
      const yPos = CARD_H_BASE - 4 + i * CARD_H_PER_SCORE;
      const sc = rs.score;
      const color = sc >= 80 ? '#10b981' : sc >= 60 ? '#f59e0b' : '#ef4444';
      // Reviewer name
      g.append('text')
        .attr('x', 12).attr('y', yPos)
        .attr('fill', 'var(--text-muted)')
        .attr('font-size', '10px')
        .text(rs.reviewer);
      // Score value
      g.append('text')
        .attr('x', CARD_W - 12).attr('y', yPos)
        .attr('text-anchor', 'end')
        .attr('fill', color)
        .attr('font-size', '10px')
        .attr('font-weight', '700')
        .text(sc + '%');
      // Mini bar
      g.append('rect')
        .attr('x', 70).attr('y', yPos - 7)
        .attr('width', Math.max(1, (sc / 100) * 60)).attr('height', 4)
        .attr('rx', 2)
        .attr('fill', color)
        .attr('opacity', 0.5);
    });
  });
}
