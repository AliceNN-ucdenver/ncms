# ADR-001: Initial Architecture for IMDB Lite Application

## Status

proposed

## Date

2026-02-23

## Deciders

Architecture Team

## Context

The IMDB Lite Application application requires an initial architecture that balances scalability, maintainability, and security requirements. The team needs to establish foundational patterns and technology choices.

## Decision

Adopt a service-oriented architecture with CALM (Common Architecture Language Model) for architecture-as-code, enabling automated governance and compliance tracking.

## Consequences

Positive: Architecture decisions are tracked as code, enabling automated validation. CALM provides machine-readable architecture artifacts that integrate with governance tooling.

Negative: Initial learning curve for CALM JSON format. Requires tooling support for diagram generation.

## Alternatives

1. Traditional documentation-only approach (Confluence/wiki) — rejected due to drift risk
2. C4 model with Structurizr — considered but CALM provides better governance integration
3. ArchiMate — too heavyweight for the current team size

## References

- FINOS CALM specification: https://calm.finos.org
- BTABoK Architecture Decision Record: https://iasa-global.github.io/btabok/architecture_decision_record.html

## Characteristics

reversibility: 2
cost: 3
risk: 3
complexity: 2
effort: 3
