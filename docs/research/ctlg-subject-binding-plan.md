# CTLG Subject Binding Plan

## Problem

CTLG cue tagging and grammar composition can identify relations like current,
first, last, and predecessor, but stress queries often use deictic subjects:
`this decision`, `the decision record`, `the current choice`. Those strings do
not name a memory subject. The 2026-04-27 subject-anchor smoke showed the gold
subject was present in the scored pool for most queries, but the naive subject
selector almost never picked it first.

CTLG therefore needs a subject binding layer between lexical retrieval and
grammar dispatch.

## Design Shape

Keep the CTLG adapter focused on cue labels. Add a separate subject resolver
that ranks candidate subjects for a query and returns:

```json
{
  "subject": "sdev-adr_jph-python-django-framework",
  "confidence": 0.82,
  "margin": 0.21,
  "evidence": {
    "best_rank": 4,
    "subject_count": 5,
    "relation_supported": true,
    "grammar_answer_in_pool": true
  }
}
```

The resolver should abstain when confidence or margin is too low. CTLG should
only compose when both cue grammar and subject binding are confident.

## Phase 1: Oracle Ceiling

Add benchmark-only `oracle_subject_*` CTLG shadow diagnostics.

Acceptance:

- If oracle subject improves recall, subject binding is the main blocker.
- If oracle subject does not improve recall, fix grammar edges or cue labels
  before training a resolver.

## Phase 2: Rule Resolver

Build a deterministic resolver over the captured scored pool. Candidate
features:

- `best_rank`: first rank where subject appears.
- `subject_count`: number of candidates with the same subject.
- `harmonic_rank`: sum of `1 / rank` for candidates with the subject.
- `relation_supported`: grammar dispatch returns a confident trace.
- `answer_in_pool`: grammar answer exists in the scored candidate pool.
- `answer_rank`: rank of grammar answer when present.
- `cue_relation`: current, first, last, predecessor, successor, trace.
- `query_overlap`: lexical overlap between query and subject/title text.

Initial score:

```text
score =
  0.25 * harmonic_rank_norm +
  0.20 * subject_count_norm +
  0.15 * inverse_best_rank_norm +
  0.25 * relation_supported +
  0.15 * answer_in_pool
```

Acceptance:

- Improves over naive subject anchor on the CTLG stress mini.
- No net regression versus baseline when run in shadow with conservative gates.
- Logs top subjects, scores, selected subject, confidence margin, and abstain
  reason per query.

## Phase 3: Learned Resolver

If Phase 2 shows lift, generate SDG training rows:

- input: query text, cue relation, top candidate subject summaries, candidate
  ranks, subject counts, and candidate snippets.
- label: correct subject or abstain.
- hard negatives: high-count wrong subjects, high-rank wrong subjects, and
  subjects whose grammar dispatch returns a plausible but wrong answer.

Train this as a sibling adapter, not as a sixth head on the five-head SLM.

Acceptance:

- Held-out subject accuracy beats deterministic resolver.
- Abstention precision is high on unanswerable and generic factual queries.
- Shadow CTLG improves right recall before any production merge.

## Production Gate

Do not enable CTLG composition in live search until:

- oracle ceiling is positive,
- non-oracle resolver has positive shadow lift,
- abstention prevents generic query damage,
- diagnostics explain every composed and abstained query.
