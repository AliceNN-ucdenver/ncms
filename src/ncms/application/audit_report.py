"""Audit-report markdown renderers — extracted from
:meth:`DocumentService.export_audit_report` so the orchestrator
stays under the B+ MI bar.

Every helper is a pure function returning a ``list[str]`` of markdown
lines, ready to be ``"\\n".join(...)``-ed by the caller.  Section
ordering / inclusion logic stays in :class:`DocumentService`.
"""

from __future__ import annotations

import json as _json


def render_compliance_section(compliance: dict) -> list[str]:
    md = ["\n---\n\n## Compliance Score"]
    md.append(f"\n**Composite:** {compliance['composite_score']}%\n")
    md.append("| Signal | Score | Weight |")
    md.append("|--------|-------|--------|")
    for key, val in compliance["breakdown"].items():
        md.append(f"| {key} | {val['score']}% | {val['weight'] * 100:.0f}% |")
    return md


def render_integrity_section(integrity: dict) -> list[str]:
    verified_str = "Yes" if integrity["verified"] else "NO \u2014 CHAIN BROKEN"
    md = ["\n---\n\n## Integrity Verification"]
    md.append(f"\n**Hash Chain Verified:** {verified_str}")
    md.append(f"**Records Checked:** {integrity['records_checked']}")
    for table, result in integrity["tables"].items():
        status = "OK" if result["verified"] else f"BROKEN at row {result['break_at']}"
        md.append(f"- {table}: {status} ({result['records_checked']} records)")
    return md


def render_document_inventory(docs: list) -> list[str]:
    md = ["\n---\n\n## Document Inventory"]
    md.append("\n| Doc Type | Title | Agent | Version | Size | Content Hash |")
    md.append("|----------|-------|-------|---------|------|-------------|")
    for d in docs:
        h = d.content_hash[:12] if d.content_hash else "N/A"
        md.append(
            f"| {d.doc_type} | {d.title[:50]} | {d.from_agent}"
            f" | v{d.version} | {d.size_bytes:,} bytes | `{h}` |"
        )
    return md


def render_traceability_section(all_links: list) -> list[str]:
    md = ["\n---\n\n## Traceability Chain"]
    if all_links:
        md.append("\n| Source | Target | Link Type |")
        md.append("|--------|--------|-----------|")
        for link in all_links:
            md.append(
                f"| {link.source_doc_id[:12]} | {link.target_doc_id[:12]} | {link.link_type} |"
            )
    else:
        md.append("\nNo document links found.")
    return md


def render_review_history(scores: list) -> list[str]:
    md = ["\n---\n\n## Review History"]
    if scores:
        md.append("\n| Reviewer | Round | Score | Severity |")
        md.append("|----------|-------|-------|----------|")
        for s in scores:
            md.append(
                f"| {s.reviewer_agent} | {s.review_round} | {s.score}% | {s.severity or 'N/A'} |"
            )
    else:
        md.append("\nNo review scores recorded.")
    return md


def render_guardrail_findings(violations: list) -> list[str]:
    md = ["\n---\n\n## Guardrail Findings"]
    if violations:
        md.append("\n| Escalation | Policy | Rule | Message | Time |")
        md.append("|-----------|--------|------|---------|------|")
        for v in violations:
            md.append(f"| {v[3]} | {v[0]} | {v[1]} | {(v[2] or '')[:60]} | {v[4]} |")
    else:
        md.append("\nNo guardrail violations.")
    return md


def render_agent_configs(configs: list) -> list[str]:
    md = ["\n---\n\n## Agent Configurations at Pipeline Start"]
    if configs:
        md.append("\n| Agent | Model | Thinking | Max Tokens | Time |")
        md.append("|-------|-------|----------|-----------|------|")
        for c in configs:
            thinking = "ON" if c[2] else "OFF"
            md.append(f"| {c[0]} | {(c[1] or '?')[:30]} | {thinking} | {c[3] or '?'} | {c[4]} |")
    else:
        md.append("\nNo config snapshots recorded.")
    return md


def render_llm_call_log(llm_calls: list) -> list[str]:
    md = ["\n---\n\n## LLM Call Log"]
    if llm_calls:
        md.append("\n| Agent | Node | Prompt | Response | Duration | Model |")
        md.append("|-------|------|--------|----------|----------|-------|")
        for c in llm_calls:
            md.append(
                f"| {c[0]} | {c[1]} | {c[2]:,} chars"
                f" | {c[3]:,} chars | {c[4]:,}ms"
                f" | {(c[5] or '?')[:25]} |"
            )
    else:
        md.append("\nNo LLM calls recorded.")
    return md


def _extract_research_doc_meta(docs: list) -> dict | None:
    for d in docs:
        if d.doc_type == "research" and d.metadata:
            _m = d.metadata if isinstance(d.metadata, dict) else _json.loads(d.metadata or "{}")
            if "research_methodology" in _m:
                return _m["research_methodology"]
    return None


def _render_research_event(evt: dict) -> list[str]:
    md: list[str] = []
    try:
        data = _json.loads(evt["extra"])
        if data.get("type") == "research_plan":
            md.append(f"\n### Query Plan ({evt['timestamp']})")
            for engine in ("web", "arxiv", "patent", "community"):
                queries = data.get(engine, [])
                if queries:
                    md.append(f"\n**{engine.title()}** ({len(queries)} queries):")
                    for q in queries:
                        md.append(f"- {q}")
        elif data.get("type") == "research_results":
            engine = data.get("engine", "?")
            count = data.get("result_count", 0)
            top = data.get("top_results", [])
            ts = evt["timestamp"]
            md.append(f"\n### {engine.title()} Results: {count} items ({ts})")
            for t in top[:3]:
                md.append(f"- {t.get('title', '?')}")
    except (_json.JSONDecodeError, KeyError):
        pass
    return md


def _render_embedded_methodology(research_doc_meta: dict) -> list[str]:
    md = ["\n### Embedded Methodology (from research document)"]
    plan = research_doc_meta.get("query_plan", {})
    summary = research_doc_meta.get("results_summary", {})
    for engine in ("web", "arxiv", "patent", "community"):
        queries = plan.get(engine, [])
        count = summary.get(engine, 0)
        if isinstance(count, dict):
            count = count.get("count", 0)
        md.append(f"- **{engine.title()}**: {len(queries)} queries, {count} results")
    return md


def render_research_methodology(timeline: list, docs: list) -> list[str]:
    research_events = [e for e in timeline if e["type"] == "research" and e["extra"]]
    research_doc_meta = _extract_research_doc_meta(docs)
    if not research_events and not research_doc_meta:
        return []

    md = ["\n---\n\n## Research Methodology"]
    for evt in research_events:
        md.extend(_render_research_event(evt))
    if research_doc_meta:
        md.extend(_render_embedded_methodology(research_doc_meta))
    return md


def render_audit_timeline(timeline: list) -> list[str]:
    md = ["\n---\n\n## Audit Timeline (last 50 events)"]
    md.append("\n| Time | Type | Agent | Detail |")
    md.append("|------|------|-------|--------|")
    for evt in timeline[-50:]:
        detail = (evt["detail"] or "")[:80]
        md.append(f"| {evt['timestamp']} | {evt['type']} | {evt['agent']} | {detail} |")
    return md
