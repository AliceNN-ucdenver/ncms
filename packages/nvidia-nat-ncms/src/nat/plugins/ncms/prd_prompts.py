# SPDX-License-Identifier: Apache-2.0
"""Prompts for the PRD agent. Edit these to customize agent behavior."""

SYNTHESIZE_PRD_PROMPT = """\
You are a senior product owner writing a Product Requirements Document (PRD). \
Synthesize the source research and expert input into a structured, actionable PRD. \
Ground security and architecture sections in the expert input provided.

## Topic
{topic}

## Source Document (Researcher's Report)
{source_content}

## Expert Input

### Architect
{architect_input}

### Security
{security_input}

Write the PRD with these sections:

# {topic} — Product Requirements Document

## Problem Statement and Scope
(Define the problem being solved, boundaries, and what is out of scope)

## Goals and Non-Goals
### Goals
(Numbered list of measurable goals)
### Non-Goals
(Explicit list of what this effort will NOT address)

## Functional Requirements
(Numbered requirements, each with acceptance criteria)

## Non-Functional Requirements
### Performance
(Latency, throughput, concurrency targets)
### Scalability
(Growth projections, scaling strategy)
### Compliance
(Regulatory requirements, standards adherence)

## Security Requirements
(Grounded in security expert input — threats, controls, mitigations)

## Architecture Alignment
(Grounded in architect expert input — patterns, decisions, constraints)

## Risk Matrix
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
(Identify key risks with likelihood, impact, and mitigation strategies)

## Success Metrics
(Numbered list of measurable success criteria)

## References
(Numbered list of sources referenced)
"""

MANIFEST_PROMPT = """\
Based on this PRD, generate a structured requirements manifest as JSON.

PRD:
{prd_content}

Return ONLY valid JSON:
{{"endpoints": [{{"method": "POST", "path": "/auth/login", "description": "..."}}], \
"security_requirements": ["token_revocation", ...], \
"technology_constraints": ["TypeScript", ...], \
"quality_targets": {{"latency_p99_ms": 200}}}}"""
