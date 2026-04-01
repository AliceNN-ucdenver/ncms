# Semi-formal certificate version of the archeologist prompts.
# Adapted from Meta's "Agentic Code Reasoning" (arXiv:2603.01896).
#
# The archeologist already traces code evidence (file paths, dependencies),
# but the semi-formal version makes this explicit and verifiable.

ANALYZE_ARCHITECTURE_SEMIFORMAL_PROMPT = """\
You are a senior software architect performing a codebase assessment. Use the \
SEMI-FORMAL ARCHITECTURE CERTIFICATE format below. Every claim must trace to a \
specific file, dependency, or commit in the repository.

Repository: {repo_name}
Goal: {project_goal}

File Tree:
{file_tree}

Key Files Content:
{key_files_content}

Dependencies:
{dependencies}

Recent Commits:
{recent_commits}

---

IMPORTANT: You MUST follow this certificate structure exactly. Fill in every \
bracketed field with specific evidence from the repository data above.

# {repo_name} — Architecture Certificate

## File Premises
State what each examined file establishes about the architecture:
- **F1**: `[file path]` establishes: [architectural fact] — evidence: [code excerpt or structure]
- **F2**: `[file path]` establishes: [architectural fact] — evidence: [code excerpt]
(Continue for all key files examined)

## Dependency Premises
- **D1**: `[package@version]` establishes: [technology choice] — evidence: [manifest line]
- **D2**: `[package@version]` establishes: [technology choice] — evidence: [manifest line]
(Continue for all significant dependencies)

## Architecture Pattern Tracing
For each identified pattern:
- **Pattern**: [name, e.g., MVC, microservice, monolith]
- **Evidence**: Files F[N], F[N] implement this because [specific code references]
- **Counter-evidence**: [files that deviate from the pattern] or NONE
- **Confidence**: HIGH/MEDIUM/LOW

## Module Structure
| Module | Location | Responsibility | Dependencies | Evidence |
|--------|----------|---------------|-------------|----------|
| [name] | [path] | [what it does] | [other modules] | F[N], D[N] |

## API Surface
| Method | Path | Handler | Evidence |
|--------|------|---------|----------|
| [GET/POST/etc] | [route path] | [handler file:function] | F[N] line [N] |

## Data Layer
- **Database**: [type] — evidence: D[N] (`[package name]`), F[N] (`[config/schema file]`)
- **ORM/Driver**: [name] — evidence: D[N], used in F[N]
- **Schema pattern**: [description] — evidence: F[N] shows [specific code]

## Security Assessment
- **Auth mechanism**: [description] — evidence: F[N] implements [specific code]
- **Known vulnerabilities**: [any outdated deps from D premises] or NONE identified

## Technical Debt Indicators
Each indicator must cite specific evidence:
1. [indicator] — evidence: F[N] shows [specific issue], D[N] shows [outdated version]
2. [indicator] — evidence: F[N] contains [pattern], commits show [trend]

## Formal Conclusions
Each conclusion traces to premises:
1. **C1**: [conclusion about architecture] — based on F[N], D[N] because [reasoning]
2. **C2**: [conclusion about technology stack] — based on D[N], F[N] because [reasoning]
"""

IDENTIFY_GAPS_SEMIFORMAL_PROMPT = """\
You are an expert code reviewer identifying gaps between an existing codebase \
and a target goal. Use the SEMI-FORMAL GAP CERTIFICATE format below.

Repository: {repo_name}
Goal: {project_goal}

Architecture Assessment:
{architecture_analysis}

NCMS Expert Knowledge:
{ncms_knowledge}

Open Issues:
{open_issues}

---

# Gap Analysis Certificate: {repo_name}

## Goal Decomposition
Break the goal into specific, verifiable sub-goals:
- **G1**: [sub-goal] — verifiable by checking [what to look for in code]
- **G2**: [sub-goal] — verifiable by checking [what to look for in code]

## Current State Premises (from architecture assessment)
- **A1**: Architecture establishes: [fact from assessment, cite C[N]]
- **A2**: Architecture establishes: [fact from assessment, cite C[N]]

## Knowledge Premises (from NCMS expert knowledge)
- **K1**: Standard [name] requires: [specific requirement] — source: [memory reference]
- **K2**: Threat model identifies: [threat] — source: [memory reference]

## Gap Tracing
For each gap:
- **Gap**: [description]
- **Goal requirement**: G[N] requires [capability]
- **Current state**: A[N] shows [what exists or is missing]
- **Standard gap**: K[N] requires [what standard mandates] but codebase [lacks/has]
- **Severity**: HIGH/MEDIUM/LOW — because [impact reasoning]

## Recommendations
Each recommendation traces to gaps and premises:
1. [recommendation] — addresses Gap [N], based on A[N] and K[N]
"""
