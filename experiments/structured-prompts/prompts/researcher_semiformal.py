# Semi-formal certificate version of the researcher synthesis prompt.
# Adapted from Meta's "Agentic Code Reasoning" (arXiv:2603.01896).
#
# Key difference from standard RCTRO: the agent must fill in a structured
# certificate with explicit premises, cross-source analysis, evidence gaps,
# and formal conclusions. Every claim must trace to a specific source.

SYNTHESIZE_SEMIFORMAL_PROMPT = """\
You are a market research analyst. Synthesize the following search results \
into a structured markdown research report using the SEMI-FORMAL RESEARCH \
CERTIFICATE format below. Every claim must be traceable to a specific source.

Topic: {topic}

## Search Results

{search_results}

---

IMPORTANT: You MUST follow this certificate structure exactly. Fill in every \
bracketed field with specific evidence from the search results above.

# {topic} — Market Research Report

## Source Premises
State what each source establishes. Every source used later must appear here.

- **S1**: [Source title](URL) establishes: [specific claim with data/quote]
- **S2**: [Source title](URL) establishes: [specific claim with data/quote]
- **S3**: [Source title](URL) establishes: [specific claim with data/quote]
- **S4**: [Source title](URL) establishes: [specific claim with data/quote]
- **S5**: [Source title](URL) establishes: [specific claim with data/quote]
(Continue for all relevant sources)

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

## Formal Conclusions
Each conclusion must cite at least 2 supporting premises:
1. **C1**: [conclusion] — supported by S[N], S[N] because [specific reasoning]
2. **C2**: [conclusion] — supported by S[N], S[N] because [specific reasoning]
(Continue for all major conclusions)

## Recommendations
Numbered list. Each recommendation must trace to at least one formal conclusion:
1. [recommendation] — based on C[N] and evidence from S[N]
2. [recommendation] — based on C[N] and evidence from S[N]

## References
Numbered list with title and URL for each source (matching S1-SN above).
"""
