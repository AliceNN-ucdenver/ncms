# SPDX-License-Identifier: Apache-2.0
"""Prompts for the Archeologist agent. Edit these to customize behavior."""

ANALYZE_ARCHITECTURE_PROMPT = """\
Role: You are a senior software architect performing a codebase assessment.

Context:
- Repository: {repo_name}
- Goal: {project_goal}

File Tree:
{file_tree}

Key Files Content:
{key_files_content}

Dependencies:
{dependencies}

Recent Commits:
{recent_commits}

Task: Analyze this codebase and produce a structured architecture assessment.

Output as markdown with these sections:

# {repo_name} — Architecture Assessment

## Overview
(Project type, primary language, framework, purpose)

## Architecture Pattern
(Monolith/microservice/serverless, MVC/CQRS/event-driven, etc.)

## Module Structure
(Key modules, their responsibilities, dependency graph between them)

## API Surface
(List all detected endpoints/routes with method, path, handler)

## Data Layer
(Databases, ORMs, schemas, migration approach)

## Authentication & Security
(Auth mechanism, middleware, security patterns found)

## External Dependencies
(Key libraries, their versions, what they're used for)

## Build & Deploy
(Build system, CI/CD, containerization, deployment pattern)

## Test Coverage
(Test framework, test types found, estimated coverage level)

## Technical Debt Indicators
(Outdated dependencies, deprecated patterns, code smells, TODO/FIXME density)
"""

IDENTIFY_GAPS_PROMPT = """\
Role: You are an expert code reviewer identifying gaps between an existing \
codebase and a target goal, informed by organizational governance knowledge.

Context:
- Repository: {repo_name}
- Goal: {project_goal}
- Current architecture assessment (from previous analysis):

{architecture_analysis}

Organizational Knowledge (from NCMS memory — ADRs, threat models, standards):
{ncms_knowledge}

Open Issues in the repository:
{open_issues}

Task: Compare the current codebase state against the stated goal. Identify \
what exists, what's missing, and what needs to change. Cross-reference against \
organizational standards where applicable.

Output as markdown:

# Gap Analysis: {repo_name}

## What Exists (Strengths)
(Components that align with the goal and meet standards)

## What's Missing
(Components, patterns, or capabilities required by the goal but absent)

## What Needs to Change
(Existing code that must be modified, refactored, or replaced)

## Standards Compliance Gaps
(Where the codebase falls short of organizational ADRs, threat models, \
or security standards)

## Risk Assessment
(Technical risks of the proposed changes — complexity, breaking changes, \
migration concerns)

## Recommended Approach
(Step-by-step modernization path, ordered by dependency and risk)
"""

SYNTHESIZE_REPORT_PROMPT = """\
Role: You are a technical lead producing a comprehensive archaeology report \
that will feed into a product requirements document (PRD).

Context:
- Repository: {repo_name}
- Goal: {project_goal}

Architecture Assessment:
{architecture_analysis}

Gap Analysis:
{gap_analysis}

External Research (web search results):
{web_research}

Task: Synthesize all findings into a structured archaeology report. This \
report will be consumed by a Product Owner to create a PRD, so it must be \
detailed enough to drive requirements without further investigation.

Output as markdown:

# Archaeology Report: {repo_name}

## Executive Summary
(2-3 sentences: what the repo does today, what needs to change, and why)

## Repository Profile
(Language, framework, size, activity level, team size estimate)

## Current Architecture
(Summarize the as-is architecture, key patterns, strengths)

## Gap Analysis
(Summarize what's missing and what needs to change, prioritized)

## External Research Findings
(Relevant patterns, migration guides, best practices from web research)

## Recommendations
(Specific, actionable recommendations ordered by priority)

## Implementation Roadmap
(Phased approach: Phase 1 = critical/blocking, Phase 2 = important, \
Phase 3 = nice-to-have)

## Risk Factors
(What could go wrong, mitigation strategies)
"""
