# Experiment: Semi-Formal Reasoning for Software Delivery Agents

## Background

Meta's "Agentic Code Reasoning" paper (arXiv:2603.01896, Ugare & Chandra, March 2026)
introduces **semi-formal reasoning**: structured certificate templates that force LLM agents
to state explicit premises, trace evidence, and derive formal conclusions before answering.

Key results:
- Patch equivalence: 78% → 88% accuracy (+10pp) on curated examples
- Real-world patches: 93% with Opus-4.5 (vs 86% standard, 73% difflib)
- Code QA: 78.3% → 87.0% with Opus-4.5 (+8.7pp)
- Fault localization: +5-12pp Top-5 improvement

The core principle: **structured templates act as certificates** — the agent cannot skip
cases or make unsupported claims. Unlike chain-of-thought which allows free-form reasoning,
semi-formal templates require filling in specific fields with verifiable evidence.

## Hypothesis

Our pipeline agents (Researcher, Archeologist, Product Owner) currently use RCTRO-format
prompts (Role, Context, Task, Requirements, Output). These prompts tell the agent what to
produce but don't constrain HOW it reasons. The agent can (and does) make unsupported
claims, skip evidence, and produce plausible-sounding but ungrounded content.

Semi-formal certificate templates adapted for document synthesis could:
1. **Reduce hallucinated references** — forcing explicit citation of sources
2. **Improve requirement traceability** — every PRD requirement traces to a research finding
3. **Improve architecture grounding** — every design decision traces to an ADR or expert input
4. **Produce more consistent quality** — structured format prevents skipping sections

## Adapted Certificate Templates

### Researcher Certificate

The Researcher synthesizes web search results into a market research report. The standard
prompt says "synthesize results into a structured report." The semi-formal version requires:

```
RESEARCH CERTIFICATE:

PREMISES (state what each source establishes):
  S1: Source [URL] establishes [specific claim] with evidence [quote/data]
  S2: Source [URL] establishes [specific claim] with evidence [quote/data]
  ...

CROSS-SOURCE ANALYSIS:
  For each key finding:
    Finding: [finding statement]
    Supporting sources: [S1, S3, S5]
    Contradicting sources: [S2] or NONE
    Confidence: HIGH/MEDIUM/LOW based on source agreement

EVIDENCE GAPS:
  Topics where fewer than 2 independent sources confirm:
  [list with what additional research would resolve them]

FORMAL CONCLUSIONS:
  Each conclusion must cite at least 2 supporting premises:
  C1: [conclusion] — supported by S1, S3 because [specific reasoning]
  C2: [conclusion] — supported by S2, S4 because [specific reasoning]
```

### Archeologist Certificate

The Archeologist analyzes existing codebases. The semi-formal version requires:

```
ARCHITECTURE CERTIFICATE:

PREMISES (state what each code artifact establishes):
  F1: File [path] establishes [architectural fact] — evidence: [code excerpt]
  F2: Dependency [name@version] establishes [technology choice] — evidence: [manifest line]
  ...

PATTERN TRACING:
  For each identified pattern:
    Pattern: [name]
    Evidence: Files [F1, F3, F7] implement this because [specific code references]
    Counter-evidence: [any files that deviate] or NONE

GAP ANALYSIS CERTIFICATE:
  For each identified gap:
    Gap: [description]
    Premise: The goal requires [specific capability]
    Evidence: Searched [file patterns] and found [what was missing]
    Impact: [HIGH/MEDIUM/LOW] because [reasoning with code evidence]

FORMAL CONCLUSIONS:
  Each recommendation must trace to premises and gaps:
  R1: [recommendation] — addresses gap G1 based on evidence F2, F5
```

### Product Owner Certificate

The Product Owner produces a PRD from research and expert input. The semi-formal version:

```
PRD CERTIFICATE:

PREMISES (state what each input establishes):
  R1: Research finding [specific finding] — source: [research report section]
  E1: Expert input [specific advice] — source: [architect/security response]
  ...

REQUIREMENT TRACING:
  For each functional requirement:
    Requirement: FR-[N]: [requirement statement]
    Traced to: R[N], E[N] — because [specific reasoning]
    Acceptance criteria derived from: [specific data point or standard]
    NOT traced to any input: [flag if requirement is not grounded]

COVERAGE ANALYSIS:
  Research findings NOT addressed by any requirement: [list]
  Expert recommendations NOT addressed: [list]
  Requirements with no research/expert backing: [list with justification]

SECURITY TRACING:
  For each security requirement:
    Threat: [threat ID or description]
    Control: [proposed control]
    Evidence: Expert E[N] recommended this based on [specific threat model]
    Standard: [OWASP/NIST/etc reference if cited by expert]

FORMAL CONCLUSIONS:
  PRD covers [X/Y] research findings, [A/B] expert recommendations.
  Gaps: [list any uncovered items with justification for exclusion]
```

## Experiment Design

### Variables

- **Independent variable**: Prompt format (standard RCTRO vs semi-formal certificate)
- **Dependent variables**: Document quality metrics (see below)
- **Controlled variables**: Same topic, same LLM (Nemotron Nano), same NCMS knowledge base,
  same expert agents, same pipeline infrastructure

### Test Topics

Run each format on 3 topics to reduce variance:
1. "Authentication patterns for identity services" (our standard test)
2. "Rate limiting and API gateway design"
3. "Event-driven microservice communication patterns"

### Quality Metrics

For each document pair (standard vs semi-formal), evaluate:

1. **Source traceability** (0-10): Can every claim be traced to a specific source?
2. **Requirement coverage** (0-10): Does the PRD address all research findings?
3. **Factual grounding** (0-10): Are references real and correctly cited?
4. **Completeness** (0-10): Are required sections present and substantive?
5. **Consistency** (0-10): Do sections contradict each other?
6. **Actionability** (0-10): Could a developer implement from this spec?

### Evaluation Method

Claude serves as LLM judge using a structured rubric. Each document pair is evaluated
blind (judge doesn't know which is standard vs semi-formal). The judge receives both
documents labeled "Version A" and "Version B" with the rubric.

## File Structure

```
experiments/structured-prompts/
  README.md                     # This file
  prompts/
    researcher_standard.py      # Current RCTRO prompt
    researcher_semiformal.py    # Semi-formal certificate version
    prd_standard.py             # Current RCTRO prompt
    prd_semiformal.py           # Semi-formal certificate version
    archeologist_standard.py    # Current RCTRO prompt
    archeologist_semiformal.py  # Semi-formal certificate version
  run_experiment.sh             # Shell script to run experiments
  harness.py                    # Python harness that calls NCMS pipeline
  judge.py                      # LLM judge evaluation script
  results/                      # Output directory for generated documents
```

## Running the Experiment

```bash
# 1. Ensure hub is running with NCMS and agents are connected
# 2. Run the experiment (generates documents with both prompt formats)
./experiments/structured-prompts/run_experiment.sh

# 3. Judge the results (Claude evaluates document pairs)
uv run python experiments/structured-prompts/judge.py
```

## References

- Ugare, S., & Chandra, S. (2026). "Agentic Code Reasoning." arXiv:2603.01896v2.
- Meta structured prompting technique: https://venturebeat.com/orchestration/metas-new-structured-prompting-technique-makes-llms-significantly-better-at
