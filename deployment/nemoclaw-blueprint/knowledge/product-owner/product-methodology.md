# Product Research Methodology

## Research Process

1. **Define the scope:** What specific aspect of the product needs research?
2. **Search broadly first:** Use general queries to understand the landscape
3. **Search deeply second:** Use specific queries for standards, best practices, and security
4. **Cross-reference:** Validate findings across multiple sources
5. **Synthesize:** Combine research into actionable requirements

## Research Query Strategies

For any product area, search for:
- "[topic] best practices 2025 2026" — current recommendations
- "[topic] OWASP guidelines" — security baseline
- "[topic] architecture patterns" — design approaches
- "[topic] common pitfalls" — what to avoid
- "[topic] industry standards" — compliance requirements

## Quality Criteria for PRDs

A good PRD:
- Cites specific sources with URLs
- Includes measurable acceptance criteria
- Addresses security from the start (not as an afterthought)
- Defines what is out of scope (non-goals)
- Is written for both technical and non-technical stakeholders

## Handoff to Builder

After publishing the PRD:
1. Announce completion to the implementation domain
2. The builder will use the PRD as input for low-level design
3. Architect and security agents will validate against their knowledge
4. Human approves the plan before implementation
