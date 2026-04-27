# CTLG Grammar Contract

**Status:** active contract
**Last revised:** 2026-04-26

This document defines the grammar boundary that CTLG must satisfy before
we train or promote another cue-tagger adapter.

CTLG is split into two cooperating signals:

- **CTLG cue tagger:** owns relation cues: causal, temporal, ordinal,
  modal, current/change, referent, subject, scope.
- **5-head SLM:** contributes grounding only: typed slots, role spans,
  topic, intent, and state-change hints. It must not reintroduce a
  shape-intent classifier or a sixth classification head.

The grammar output is `TLGQuery`. The dispatcher maps `TLGQuery.relation`
to a walker intent and preserves `referent` plus optional `secondary`
for binary temporal relations.

## Shape Contract

| MSEB shape | Grammar meaning | Required cue pattern | `TLGQuery` | Dispatch intent | Notes |
|---|---|---|---|---|---|
| `current_state` | Current terminal state for a subject or slot | `ASK_CURRENT`, optional `SCOPE`/`REFERENT` | `axis=state`, `relation=current` | `current` | `ORDINAL_LAST` alone is not `current`; it stays ordinal and maps later. |
| `origin` | Root/earliest state of a subject trajectory | `ORDINAL_FIRST` or equivalent original/root cue | `axis=ordinal`, `relation=first` | `origin` | Queries worded as "what motivated/caused..." are causal, not origin. |
| `ordinal_first` | First member of an ordered trajectory | `ORDINAL_FIRST` | `axis=ordinal`, `relation=first` | `origin` | Same walker as `origin`; kept as a distinct benchmark shape. |
| `ordinal_last` | Last/final member of an ordered trajectory | `ORDINAL_LAST` | `axis=ordinal`, `relation=last` | `current` | Grammar preserves ordinal semantics; dispatcher can use current walker. |
| `sequence` | Direct successor after a named anchor | `TEMPORAL_AFTER` + one anchor | `axis=temporal`, `relation=after_named`, `referent=X` | `sequence` | Vague "trace context through decision" is not enough; needs anchor `X`. |
| `predecessor` | Direct predecessor before a named anchor | `TEMPORAL_BEFORE` + one anchor | `axis=temporal`, `relation=predecessor`, `referent=X` | `predecessor` | One-anchor before is predecessor, not `before_named`. |
| `before_named` | Ordering comparison between two named anchors | `TEMPORAL_BEFORE` + two anchors | `axis=temporal`, `relation=before_named`, `referent=X`, `secondary=Y` | `before_named` | Requires both `X` and `Y`; otherwise use `predecessor`. |
| `interval` | Memories between two anchors | two anchors or explicit interval bounds | `axis=temporal`, `relation=between` or `during_interval` | `interval` | Current implementation lacks full two-bound synthesis; keep gold-cue tests strict. |
| `range` | Memories inside a date/period range | `TEMPORAL_DURING` + `TEMPORAL_ANCHOR` | `axis=temporal`, `relation=during_interval`, `temporal_anchor=T` | `interval`/range filter | Calendar normalization can remain outside CTLG. |
| `concurrent` | Memories overlapping a named anchor/window | `TEMPORAL_DURING` + anchor, no date anchor | `axis=temporal`, `relation=concurrent_with`, `referent=X` | `concurrent` | "Alongside X" should train as concurrent/during, not before. |
| `transitive_cause` | Chain of causes leading to an effect | causal AltLex/explicit cue with chain semantics | `axis=causal`, `relation=chain_cause_of`, `referent=effect` | `transitive_cause` | Uses `CAUSED_BY` graph when present; timestamp chain is fallback. |
| `causal_chain` | Same as transitive cause in MSEB | causal AltLex/explicit cue with chain semantics | `axis=causal`, `relation=chain_cause_of`, `referent=effect` | `transitive_cause` | Alias shape until the benchmark separates direct vs chain causality. |
| `retirement` | State was removed/replaced/retired | `ASK_CHANGE` plus retirement cue, or SLM `state_change=retirement` | `axis=state`, `relation=retired` | `retirement` | CTLG must see change; SLM may disambiguate declaration vs retirement. |
| `noise` | No grammar answer | no relation cue or unsupported cue pattern | `None` | abstain | False positives here are worse than abstention. |

## Production Rules

The synthesizer is first-match deterministic. Rule order is part of the
contract:

1. Modal counterfactual.
2. Ask-change/declaration/retirement.
3. Current state, but only with meaningful current wording or grounding.
4. Ordinal first/last/nth.
5. Causal direct or chain.
6. Temporal before/after/during/since.
7. Bare referent + scope fallback.

This ordering prevents common mistakes:

- Bare question words like "what" and "which" must not be treated as
  current-state cues.
- Meaningful current-state wording like "currently", "current", "adopted",
  or "latest chosen" may route to `state/current`.
- `ORDINAL_FIRST` / `ORDINAL_LAST` targets must not be stolen by causal
  relative clauses such as "earliest concern that led to...".
- `ASK_CHANGE + TEMPORAL_BEFORE` must not be stolen by predecessor.
- `before X` means predecessor, while `X before Y` means binary ordering.
- `during 2023` means interval/range, while `during OAuth` means concurrent.

## Training Implications

The CTLG SDG generator should produce rows from this contract, not from
shape names alone. Each generated row needs an expected `TLGQuery` in
addition to token-level cue tags so the corpus can be audited in three
layers:

1. Token cue correctness.
2. Cue-to-`TLGQuery` synthesis correctness.
3. Dispatcher answer correctness.

Rows whose natural-language wording conflicts with the shape must be
rewritten or relabeled. The known risky cases are:

- `origin` templates using "motivated", "caused", or "why".
- `before_named` templates with only one anchor.
- `sequence` templates without a concrete "after X" anchor.
- `concurrent` templates using "alongside" but labeled as
  `TEMPORAL_BEFORE`.
