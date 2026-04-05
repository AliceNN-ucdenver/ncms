# SPDX-License-Identifier: Apache-2.0
"""Prompts for the research agent.

Uses semi-formal certificate format (adapted from Meta's "Agentic Code
Reasoning", arXiv:2603.01896) with Chain-of-Thought reasoning enabled.
The certificate structure forces explicit source premises, cross-source
analysis with confidence ratings, evidence gap identification, and
formal conclusions with citation chains.
"""

PLAN_QUERIES_PROMPT = """\
You are an expert research query planner. Given a topic, generate search queries \
optimized for four different search engines. Each engine has different constraints.

Topic: {topic}

Return a JSON object with exactly these four keys:

"web" — 5 queries for Tavily web search. Tavily works like Google — use natural \
  language queries with specific terms. Include year (2025/2026) for recency. \
  Cover these angles:
  1. Market size, growth projections, key vendors, competitive landscape
  2. Specific standards (NIST, OWASP, ISO), frameworks, compliance requirements
  3. Threat landscape, attack vectors, vulnerability data, breach statistics
  4. Architecture patterns, technology stacks, integration approaches
  5. Case studies with measurable outcomes (ROI, latency, conversion)

"arxiv" — 3 queries for ArXiv academic paper search. ArXiv uses keyword matching \
  against paper titles and abstracts. Use SHORT technical phrases (3-6 words). \
  Focus on formal methods, protocol analysis, security proofs, benchmarks. \
  Example: "zero-trust authentication formal verification"

"patent" — 3 queries for USPTO patent search. USPTO uses AND operators between \
  keywords. Use exactly 2-3 technical keywords joined by AND. More than 3 terms \
  often returns zero results. No stop words (for/the/of/with). Order from most \
  specific to broadest so we can fall back. \
  Example: "authentication AND cryptographic AND protocol"

"community" — 3 queries for HackerNews Algolia search. HN search matches against \
  story titles which are SHORT and casual. Use 2-3 word phrases the way a \
  developer would title a Show HN or Ask HN post. \
  Good: "SSO auth", "zero trust", "identity API" \
  Bad: "identity federation adoption metrics" (too formal, zero results)

Return ONLY a JSON object, no markdown, no explanation.
"""

GAP_ANALYSIS_PROMPT = """\
You are a research analyst reviewing initial search results for: {topic}

Here is what the first round of searches found:
{search_results}

Perform a structured gap analysis:

PREMISES: For each major finding, count how many independent sources support it.

EVIDENCE GAPS: Identify exactly 3 topics where:
- A finding has only 1 supporting source (needs independent confirmation)
- Two sources contradict each other (needs resolution)
- An important sub-topic has zero coverage (needs new research)

For each gap, write a targeted web search query with domain terminology \
and year markers.

Return ONLY a JSON array of 3 search query strings.
"""

SYNTHESIZE_PROMPT = """\
You are a market research analyst. Synthesize the following search results \
into a structured markdown research report using the SEMI-FORMAL RESEARCH \
CERTIFICATE format below. Every claim must be traceable to a specific source.

Topic: {topic}

## Search Results

{search_results}

---

IMPORTANT: Follow this certificate structure exactly. Fill in every \
bracketed field with specific evidence from the search results above.

# {topic} — Market Research Report

## Source Premises
State what each source establishes. Every source used later must appear here.

- **S1**: [Source title](URL) establishes: [specific claim with data/quote]
- **S2**: [Source title](URL) establishes: [specific claim with data/quote]
(Continue for all relevant sources — web and academic)

## Executive Summary
Write 3-4 sentences. Each sentence must cite at least one source premise (S1, S2, etc.).

## Cross-Source Analysis

### Standards and Best Practices
For each finding, state:
- **Finding**: [specific finding]
- **Supporting sources**: S[N], S[N] — because [why these sources agree]
- **Contradicting sources**: S[N] or NONE
- **Confidence**: HIGH (3+ sources) / MEDIUM (2 sources) / LOW (1 source)

### Security and Compliance
Same format: finding, supporting sources, contradictions, confidence.

### Implementation Patterns
Same format: finding, supporting sources, contradictions, confidence.

### Market Landscape
Same format: finding, supporting sources, contradictions, confidence.

## Evidence Gaps
Topics where fewer than 2 independent sources confirm a finding:
- [gap 1]: Only supported by S[N]. Additional research needed on [specific question].
- [gap 2]: ...

## Jobs-to-be-Done Analysis
Based on community discussions and market evidence:
- **Primary job:** [what users are hiring current solutions to do]
- **Underserved outcomes:** [where current solutions fail — cite community evidence]
- **Overserved outcomes:** [where current solutions over-deliver — opportunity to simplify]

## Patent Landscape
If patent data is available in the search results:
- **Related patents:** [P1-PN with titles, assignees, filing dates]
- **Coverage gaps:** [areas with user demand but no patent coverage]
- **Freedom to operate:** [assessment of patent density in the target space]
(If no patent data is available, state "No patent data available for this analysis.")

## Whitespace Analysis
Synthesize all sources to identify market opportunities:
- **Unmet jobs:** [intersection of community pain + limited patent coverage + no dominant product]
- **Market opportunity:** [quantified from web research data + patent gaps]
- **Recommended focus:** [specific product opportunity with supporting evidence]

## Formal Conclusions
Each conclusion must cite at least 2 supporting premises:
1. **C1**: [conclusion] — supported by S[N], S[N] because [specific reasoning]
2. **C2**: [conclusion] — supported by S[N], S[N] because [specific reasoning]

## Recommendations
Numbered list. Each recommendation must trace to at least one formal conclusion:
1. [recommendation] — based on C[N] and evidence from S[N]
2. [recommendation] — based on C[N] and evidence from S[N]

## References
Numbered list with title and URL for each source (matching S1-SN above).
"""
