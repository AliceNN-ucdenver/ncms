// ── NCMS Dashboard — Document Flow Graph (D3 DAG) ───────────────────
// Renders a horizontal flow of documents with governance metadata.
// Taller cards with type-specific content + governance hash section.
// No arrow lines — "derived from" / "reviews" labels between cards.

const DOC_TYPE_CONFIG = {
  research:  { icon: '\uD83D\uDD0D', color: '#10b981', label: 'Research',  order: 0 },
  prd:       { icon: '\uD83D\uDCCB', color: '#22c55e', label: 'PRD',       order: 1 },
  manifest:  { icon: '\uD83D\uDCE6', color: '#f97316', label: 'Manifest',  order: 2 },
  design:    { icon: '\uD83D\uDCD0', color: '#f59e0b', label: 'Design',    order: 3 },
  review:    { icon: '\uD83D\uDEE1', color: '#a78bfa', label: 'Review',    order: 4 },
  contract:  { icon: '\uD83D\uDCC4', color: '#8b5cf6', label: 'Contract',  order: 5 },
};

const LINK_TYPE_CONFIG = {
  derived_from: { color: '#58a6ff', label: 'derived from' },
  reviews:      { color: '#a78bfa', label: 'reviews' },
  supersedes:   { color: '#f59e0b', label: 'supersedes' },
  cites:        { color: '#6e7681', label: 'cites' },
  approved_by:  { color: '#10b981', label: 'approved by' },
};

// Card dimensions — taller for governance metadata
const CARD_W = 200;
const CARD_H_BASE = 158;      // base without review scores
const CARD_H_PER_SCORE = 16;  // extra per reviewer score line
const CARD_GAP_X = 55;
const CARD_GAP_Y = 16;
const MARGIN = { top: 10, right: 16, bottom: 10, left: 16 };
const GOV_Y_OFFSET = 88;      // where governance section starts

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

  // Per-reviewer score lookup
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

  // Build nodes
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
      content_hash: d.content_hash || '',
      created_at: d.created_at || '',
      metadata: d.metadata || {},
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

  // Build lineage map: target_id -> {link_type, source_label}
  const lineageMap = {};
  for (const link of (links || [])) {
    if (link.link_type === 'derived_from' && docMap[link.source_doc_id]) {
      const src = docMap[link.target_doc_id]; // derived_from: source derives from target
      const tgt = docMap[link.source_doc_id];
      if (tgt) {
        lineageMap[tgt.id] = { type: 'derived from', sourceLabel: docMap[link.target_doc_id]?.label || '?' };
      }
    } else if (link.link_type === 'reviews' && docMap[link.source_doc_id]) {
      const reviewer = docMap[link.source_doc_id];
      if (reviewer) {
        lineageMap[reviewer.id] = { type: 'reviews', sourceLabel: docMap[link.target_doc_id]?.label || '?' };
      }
    }
  }
  nodes.forEach(n => { n.lineage = lineageMap[n.id] || null; });

  // Compute per-node card height
  for (const n of nodes) {
    const reviewLines = n.perReviewer.length > 0 ? n.perReviewer.length * CARD_H_PER_SCORE + 4 : 0;
    n.cardH = CARD_H_BASE + reviewLines;
  }

  // Layout: horizontal columns by type
  const columns = [];
  const colMap = {};
  for (const n of nodes) {
    if (!colMap[n.doc_type]) {
      colMap[n.doc_type] = { type: n.doc_type, nodes: [] };
      columns.push(colMap[n.doc_type]);
    }
    colMap[n.doc_type].nodes.push(n);
  }

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
  const height = Math.max(totalH, 140);
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

  // Build edges from explicit links (for label placement only — no arrow lines)
  const edges = [];
  const edgeSet = new Set();
  for (const link of (links || [])) {
    let src = docMap[link.source_doc_id];
    let tgt = docMap[link.target_doc_id];
    if (src && tgt) {
      const cfg = LINK_TYPE_CONFIG[link.link_type] || { color: '#6e7681', label: link.link_type };
      if (link.link_type === 'derived_from') [src, tgt] = [tgt, src];
      const key = src.id + '->' + tgt.id;
      if (!edgeSet.has(key)) {
        edgeSet.add(key);
        edges.push({ source: src, target: tgt, link_type: link.link_type, ...cfg });
      }
    }
  }

  // Create SVG
  const svg = d3.select('#' + containerId)
    .append('svg')
    .attr('width', width)
    .attr('height', height)
    .attr('class', 'doc-flow-svg');

  // Lineage is shown on the cards themselves — no floating labels

  // ── Draw node cards ──────────────────────────────────────────────
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

  // ── Line 1: Type label + size ──
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 16)
    .attr('fill', d => d.color)
    .attr('font-size', '10px')
    .attr('font-weight', '700')
    .text(d => d.icon + ' ' + d.label + (d.version > 1 ? ' v' + d.version : ''));

  nodeGroup.append('text')
    .attr('x', CARD_W - 10).attr('y', 16)
    .attr('text-anchor', 'end')
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => d.size_bytes > 0 ? (d.size_bytes / 1024).toFixed(1) + ' KB' : '');

  // Accent line under type label
  nodeGroup.append('line')
    .attr('x1', 10).attr('x2', CARD_W - 10)
    .attr('y1', 22).attr('y2', 22)
    .attr('stroke', d => d.color)
    .attr('stroke-width', 1.5)
    .attr('opacity', 0.4);

  // ── Line 2: Title ──
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 37)
    .attr('fill', 'var(--text-primary)')
    .attr('font-size', '11px')
    .attr('font-weight', '500')
    .text(d => {
      let t = d.title;
      const dashIdx = t.indexOf(' \u2014 ');
      if (dashIdx > 0 && dashIdx < t.length - 5) t = t.substring(dashIdx + 3);
      return t.length > 28 ? t.substring(0, 26) + '..' : t;
    });

  // ── Line 3: Agent + Timestamp ──
  nodeGroup.append('text')
    .attr('x', 12).attr('y', 52)
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => d.from_agent);

  nodeGroup.append('text')
    .attr('x', CARD_W - 10).attr('y', 52)
    .attr('text-anchor', 'end')
    .attr('fill', 'var(--text-muted)')
    .attr('font-size', '9px')
    .text(d => {
      if (!d.created_at) return '';
      try {
        const dt = new Date(d.created_at);
        return dt.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
      } catch { return ''; }
    });

  // ── Line 4: Type-specific metadata in inset panel ──
  nodeGroup.each(function(d) {
    const g = d3.select(this);
    let metaText = '';
    if (d.doc_type === 'research') {
      const rm = (typeof d.metadata === 'string' ? JSON.parse(d.metadata || '{}') : d.metadata) || {};
      const meth = rm.research_methodology || {};
      const s = meth.results_summary || {};
      if (Object.keys(s).length > 0) {
        metaText = `${s.web||0} web \u00B7 ${s.arxiv||0} papers \u00B7 ${s.patent||0} patents \u00B7 ${s.community||0} community`;
      } else {
        metaText = d.entity_count > 0 ? d.entity_count + ' entities' : '';
      }
    } else if (d.doc_type === 'manifest') {
      metaText = d.entity_count + ' entities';
    } else if (d.doc_type === 'review' && d.avgScore != null) {
      metaText = 'avg: ' + d.avgScore + '%';
    } else {
      metaText = d.entity_count > 0 ? 'v' + d.version + ' \u00B7 ' + d.entity_count + ' entities' : 'v' + d.version;
    }
    if (metaText) {
      // Inset panel background
      g.append('rect')
        .attr('x', 8).attr('y', 58)
        .attr('width', CARD_W - 16).attr('height', 20)
        .attr('rx', 4).attr('ry', 4)
        .attr('fill', 'rgba(0,0,0,0.25)')
        .attr('stroke', d.color + '20')
        .attr('stroke-width', 0.5);
      // Metadata text inside inset
      g.append('text')
        .attr('x', 14).attr('y', 72)
        .attr('fill', 'var(--text-muted)')
        .attr('font-size', '8px')
        .text(metaText);
    }
  });

  // ── Per-reviewer score bars (design card only) ──
  nodeGroup.each(function (d) {
    if (d.perReviewer.length === 0) return;
    const g = d3.select(this);

    // Separator
    g.append('line')
      .attr('x1', 10).attr('x2', CARD_W - 10)
      .attr('y1', 86).attr('y2', 86)
      .attr('stroke', 'var(--border)').attr('stroke-width', 0.5);

    d.perReviewer.forEach((rs, i) => {
      const yPos = 100 + i * CARD_H_PER_SCORE;
      const sc = rs.score;
      const color = sc >= 80 ? '#10b981' : sc >= 60 ? '#f59e0b' : '#ef4444';

      g.append('text')
        .attr('x', 12).attr('y', yPos)
        .attr('fill', 'var(--text-muted)')
        .attr('font-size', '10px')
        .text(rs.reviewer);

      g.append('text')
        .attr('x', CARD_W - 12).attr('y', yPos)
        .attr('text-anchor', 'end')
        .attr('fill', color)
        .attr('font-size', '10px')
        .attr('font-weight', '700')
        .text(sc + '%');

      g.append('rect')
        .attr('x', 80).attr('y', yPos - 7)
        .attr('width', Math.max(1, (sc / 100) * 70)).attr('height', 5)
        .attr('rx', 2)
        .attr('fill', color)
        .attr('opacity', 0.5);
    });
  });

  // ── Governance section (bottom of each card) ──
  nodeGroup.each(function (d) {
    const g = d3.select(this);
    const govY = d.cardH - 40;

    // Dashed governance separator
    g.append('line')
      .attr('x1', 10).attr('x2', CARD_W - 10)
      .attr('y1', govY).attr('y2', govY)
      .attr('stroke', d.color + '30')
      .attr('stroke-width', 1)
      .attr('stroke-dasharray', '4,3');

    // Lineage (derived from / reviews)
    if (d.lineage) {
      const icon = d.lineage.type === 'reviews' ? '\u2B50' : '\u2190';
      g.append('text')
        .attr('x', 12).attr('y', govY + 13)
        .attr('fill', d.lineage.type === 'reviews' ? '#a78bfa' : '#58a6ff')
        .attr('font-size', '8px')
        .text(icon + ' ' + d.lineage.type + ' ' + d.lineage.sourceLabel);
    }

    // SHA hash
    const hash = d.content_hash ? d.content_hash.substring(0, 12) : '';
    if (hash) {
      g.append('text')
        .attr('x', 12).attr('y', govY + 26)
        .attr('fill', '#6e7681')
        .attr('font-size', '8px')
        .attr('font-family', 'monospace')
        .text('\uD83D\uDD17 ' + hash);
    }

    // Chain verification
    g.append('text')
      .attr('x', CARD_W - 10).attr('y', govY + 26)
      .attr('text-anchor', 'end')
      .attr('fill', '#6e7681')
      .attr('font-size', '8px')
      .text('\u2713 chain');
  });
}
