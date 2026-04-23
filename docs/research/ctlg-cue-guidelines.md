# CTLG Cue-Labeling Guidelines

**Status:** draft — for cue-labeling pilot (Phase 1)
**Companion:** `docs/research/ctlg-design.md`
**Owner:** NCMS core

This document specifies how human (and LLM) annotators apply BIO cue labels to query-voice and memory-voice text for training the CTLG 6th head. Target: inter-annotator agreement Cohen's κ ≥ 0.8 on the pilot set before scale-out.

## Labeling contract

Each token receives exactly one label from the cue vocabulary. Labels use standard BIO notation: `B-<TYPE>` begins a span, `I-<TYPE>` continues, `O` is outside. Adjacent `B-X` / `I-X` spans of the same type form one cue span.

Input is tokenized by the model's tokenizer (BERT wordpiece). Annotators work at the **surface-word level** and the preprocessor expands to wordpieces with same-label propagation.

## Cue families

### CAUSAL — explicit causal connectives

**Tag**: `B-CAUSAL_EXPLICIT` / `I-CAUSAL_EXPLICIT`

Cues that mark explicit cause-effect relations. PDTB 3.0 Class: CONTINGENCY.Cause.

| Include | Example |
|---------|---------|
| "because" | "We picked Postgres **because** it handles JSONB well" |
| "due to" | "Switched **due to** the audit requirements" |
| "since" (causal sense only) | "**Since** the new policy, we use Vault" |
| "given that" | "**Given that** we moved to AWS, Docker made sense" |
| "as" (causal sense) | "**As** the load grew, we sharded" |
| "owing to" | "**Owing to** outages, we moved" |
| "on account of" | "**On account of** compliance, we added encryption" |

| EXCLUDE | Why |
|---------|-----|
| "since 2023" | Temporal sense, not causal — use TEMPORAL_SINCE |
| "as a service" | Idiomatic compound, not causal |
| "as we discussed" | Discourse marker, not causal |

**Disambiguation rule**: if substituting "because" preserves meaning, it's CAUSAL_EXPLICIT. Otherwise tag by dominant sense.

---

### CAUSAL_ALTLEX — alternative causal lexicalizations

**Tag**: `B-CAUSAL_ALTLEX` / `I-CAUSAL_ALTLEX`

Multi-word non-connective phrases that express causation. Anchored in Hidey & McKeown 2016.

| Include | Example |
|---------|---------|
| "led to" | "The outage **led to** our multi-region rollout" |
| "resulted in" | "Pressure **resulted in** the rewrite" |
| "drove the decision" | "What **drove the decision** to use Yugabyte?" |
| "one reason" | "**One reason** we use Postgres is JSONB" |
| "one driver" | "**A driver** behind the move was cost" |
| "the reason" | "**The reason** we left CRDB was latency" |
| "the motivation" | "**The motivation** for event sourcing is audit" |
| "caused us to" | "The spike **caused us to** adopt rate limiting" |
| "made us" | "Outages **made us** adopt circuit breakers" |
| "behind X" | "The rationale **behind** that choice" |

**Multi-word span**: tag the full phrase as one span (B + one-or-more I tags).

---

### TEMPORAL_BEFORE — before-reference markers

**Tag**: `B-TEMPORAL_BEFORE` / `I-TEMPORAL_BEFORE`

Markers locating a state/event BEFORE an anchor.

| Include | Example |
|---------|---------|
| "before" | "What did we use **before** Postgres?" |
| "prior to" | "**Prior to** YugabyteDB, we ran CRDB" |
| "ahead of" | "**Ahead of** the migration, we benchmarked" |
| "preceding" | "The stack **preceding** v2" |
| "earlier than" | "**Earlier than** Kubernetes, we used Swarm" |
| "predated" | "Rails **predated** Phoenix in our codebase" |
| "until" | "We used Redis **until** we adopted Memcached" (boundary marker, still before) |

| EXCLUDE | Why |
|---------|-----|
| "before the meeting" | Generic temporal, but for CTLG purposes ONLY tag when it anchors a state-evolution question — meetings aren't zone graph nodes. Use judgment. |

---

### TEMPORAL_AFTER — after-reference markers

**Tag**: `B-TEMPORAL_AFTER` / `I-TEMPORAL_AFTER`

| Include | Example |
|---------|---------|
| "after" | "What did we adopt **after** Postgres?" |
| "following" | "**Following** the outage, we added retries" |
| "once" | "**Once** K8s landed, Swarm was retired" |
| "subsequently" | "**Subsequently** we adopted Istio" |
| "succeeded" | "Prisma **succeeded** TypeORM in our stack" |

---

### TEMPORAL_DURING — during-interval markers

**Tag**: `B-TEMPORAL_DURING` / `I-TEMPORAL_DURING`

| Include | Example |
|---------|---------|
| "during" | "What did we use **during** 2023?" |
| "while" | "**While** we were on CRDB, we had issues" |
| "amid" | "**Amid** the migration, tests broke" |
| "throughout" | "**Throughout** Q2, we ran A/B" |
| "in the period" | "**In the period** of monolith maintenance" |

---

### TEMPORAL_SINCE — since-anchor markers

**Tag**: `B-TEMPORAL_SINCE` / `I-TEMPORAL_SINCE`

| Include | Example |
|---------|---------|
| "since" (temporal) | "We've used Postgres **since** 2023" |
| "as of" | "**As of** last sprint, Yugabyte is primary" |
| "from X on(ward)" | "**From Q3 on**, we require code review" |
| "ever since" | "**Ever since** the migration" |

**Disambiguation**: "since" defaults to TEMPORAL_SINCE unless the sentence clearly expresses causation ("since you asked" = discourse, not causal; "since the audit, we …" = could be either — use judgment by dominant sense).

---

### TEMPORAL_ANCHOR — concrete date/time expressions

**Tag**: `B-TEMPORAL_ANCHOR` / `I-TEMPORAL_ANCHOR`

Dates, quarters, relative time references. Populates `TLGQuery.temporal_anchor`.

| Include | Example |
|---------|---------|
| "2023", "Q2 2024" | "**Q2** last year" |
| "last sprint", "next quarter" | "**Last sprint**, we …" |
| "yesterday", "last week", "last month" | "**Yesterday** the test failed" |
| "the monolith era", "the CRDB phase" | named interval — can be multi-word |

**Multi-word spans**: tag whole dates and named periods as single spans.

---

### ORDINAL_FIRST — first/initial markers

**Tag**: `B-ORDINAL_FIRST` / `I-ORDINAL_FIRST`

| Include | Example |
|---------|---------|
| "first", "1st" | "What was the **first** database?" |
| "initial" | "**Initial** design" |
| "earliest" | "**Earliest** commit" |
| "original" (ordinal sense) | "**Original** architecture" |
| "starter" | "**Starter** framework" |

**Disambiguation**: "first database in the morning" = habitual/frequency, NOT ordinal. Check context.

---

### ORDINAL_LAST — last/final/most-recent markers

**Tag**: `B-ORDINAL_LAST` / `I-ORDINAL_LAST`

| Include | Example |
|---------|---------|
| "last", "latest", "final" | "The **last** decision" |
| "most recent" | "**Most recent** change" |
| "current" (when used as ordinal) | "The **current** framework" — prefer ASK_CURRENT when the cue is asking for present state |
| "latter" | |
| "newest" | |

---

### ORDINAL_NTH — nth-position markers

**Tag**: `B-ORDINAL_NTH` / `I-ORDINAL_NTH`

| Include | Example |
|---------|---------|
| "second", "third", "fourth" | "The **second** migration" |
| "2nd", "3rd", "4th" | |

---

### MODAL_HYPOTHETICAL — counterfactual / hypothetical markers

**Tag**: `B-MODAL_HYPOTHETICAL` / `I-MODAL_HYPOTHETICAL`

The signal that triggers counterfactual dispatcher behavior.

| Include | Example |
|---------|---------|
| "would have" | "**Would we have** kept CRDB?" |
| "could have" | "**Could we have** avoided the rewrite?" |
| "if not for" | "**If not for** the audit, …" |
| "had we" | "**Had we** stayed with Swarm, …" |
| "if X hadn't" | "**If CRDB hadn't** failed, …" |
| "suppose" | "**Suppose** we'd picked Mongo" |
| "imagine" | "**Imagine** staying on monolith" |

---

### ASK_CURRENT — asks for current / present state

**Tag**: `B-ASK_CURRENT` / `I-ASK_CURRENT`

Temporal cues indicating the question focus is the CURRENT state, not historical.

| Include | Example |
|---------|---------|
| "now" | "What's our database **now**?" |
| "currently" | "**Currently** running on" |
| "today" | "What are we using **today**?" |
| "at present" | "**At present**, the stack is" |
| "right now" | "**Right now**, we're on K8s" |
| "in production" (when present-tense context) | |

---

### ASK_CHANGE — asks about changes / transitions

**Tag**: `B-ASK_CHANGE` / `I-ASK_CHANGE`

| Include | Example |
|---------|---------|
| "what changed" | "**What changed** about the stack?" |
| "what happened" | "**What happened** with Postgres?" |
| "the transition" | "Describe **the transition** to Kubernetes" |
| "the migration" (when used as referent) | "Walk me through **the migration**" |

---

### REFERENT — named catalog entity referenced in the query

**Tag**: `B-REFERENT` / `I-REFERENT`

Any catalog hit (gazetteer-detectable surface form) that serves as a reference anchor in the query.

| Include | Example |
|---------|---------|
| "Postgres" | "What did we use before **Postgres**?" |
| "auth-service" (if registered as a subject) | |
| "React Native" (multi-word catalog entry) | tag as one span |
| "GPT-4" (if in catalog) | |

**Bootstrap rule**: during LLM-labeling phase, use the gazetteer output as the REFERENT label. Any span with `slot != "none"` in the role-head output becomes `B-REFERENT`.

---

### SUBJECT — subject whose state evolves

**Tag**: `B-SUBJECT` / `I-SUBJECT`

The entity whose state is being queried about. Distinct from REFERENT: a REFERENT is ANY catalog mention; a SUBJECT is specifically the thing whose state the query targets.

| Include | Example |
|---------|---------|
| "auth-service" | "What does the **auth-service** use for its database?" |
| "the user API" | "What did the **user API** run on?" |
| "our pipeline" | "What's our **pipeline** based on?" |

**Annotator note**: when a query has BOTH a subject and a separate referent, tag both. E.g. "What did the **auth-service** use before **Postgres**?" → SUBJECT=auth-service, REFERENT=Postgres.

---

### SCOPE — catalog slot word

**Tag**: `B-SCOPE` / `I-SCOPE`

The slot the query is asking about ("database", "framework", "tool", etc.).

| Include | Example |
|---------|---------|
| "database" | "What **database** do we use?" |
| "framework" | "Which **framework** is in prod?" |
| "CI tool" | "Our **CI tool**" |
| "orchestrator" | "Before **Kubernetes** took over as **orchestrator**" |

## Tagging decision tree

When annotators are uncertain, apply in this order:

1. **Is the span a catalog entity?** → REFERENT (or SUBJECT if it's the subject of the state change).
2. **Is it a date or named period?** → TEMPORAL_ANCHOR.
3. **Is it a discourse connective from the PDTB causal class?** → CAUSAL_EXPLICIT or CAUSAL_ALTLEX.
4. **Is it a temporal preposition / adverb?** → TEMPORAL_BEFORE / AFTER / DURING / SINCE (pick by sense).
5. **Is it an ordinal position word?** → ORDINAL_FIRST / LAST / NTH.
6. **Is it a modal construction?** → MODAL_HYPOTHETICAL.
7. **Is it a question-marker about present state or change?** → ASK_CURRENT / ASK_CHANGE.
8. **Is it a slot type word?** → SCOPE.
9. **None of the above** → O.

When multiple rules apply, prefer the **more specific** tag. E.g. "since 2023" — the whole span is TEMPORAL_SINCE + TEMPORAL_ANCHOR, tag as two adjacent spans (`since` = TEMPORAL_SINCE, `2023` = TEMPORAL_ANCHOR).

## Worked examples

### Example 1: current-state query

```
Query: "What's our current database now?"
Tokens:   What 's our current database now ?
Labels:   O    O  O  B-ORD_LAST B-SCOPE B-ASK_CURRENT O
```

Composes to: `TLGQuery(axis="state", relation="current", scope="database")`.

### Example 2: before-named query

```
Query: "What did the auth-service use before Postgres?"
Tokens:  What did the auth-service use before Postgres ?
Labels:  O    O   O   B-SUBJECT  O   B-TEMP_BEF B-REFERENT O
```

Composes to: `TLGQuery(axis="temporal", relation="before_named", subject="auth-service", referent="postgres")`.

### Example 3: causal query

```
Query: "Why did we migrate away from CockroachDB?"
Tokens:  Why did we migrate away from CockroachDB ?
Labels:  B-CAUSAL_EXP O O O O O B-REFERENT O
```

"Why" alone is CAUSAL_EXPLICIT (asks for cause). Composes to `TLGQuery(axis="causal", relation="cause_of", referent="cockroachdb", depth=1)`.

### Example 4: causal chain

```
Query: "Explain the chain of decisions that led to YugabyteDB."
Tokens:  Explain the chain of decisions that led_to YugabyteDB .
Labels:  O       O   O     O  O         O    B-CAUSAL_ALT I-CAUSAL_ALT B-REFERENT O
```

Multi-word cue ("led to") spans two tokens (B + I). Composes to `TLGQuery(axis="causal", relation="chain_cause_of", referent="yugabytedb", depth=2)`.

### Example 5: counterfactual

```
Query: "What would we be using if we hadn't switched to YugabyteDB?"
Tokens:  What would we be using if we hadn 't switched to YugabyteDB ?
Labels:  O    B-MODAL_HYP I-MODAL_HYP O O O O O O O O B-REFERENT O
```

"would … hadn't" is the counterfactual frame — tag "would" as B-MODAL_HYP and allow loose continuation. Synthesizer produces `TLGQuery(axis="modal", relation="would_be_current_if", referent="yugabytedb", scenario="skip_most_recent_supersession")`.

### Example 6: origin query

```
Query: "How did we originally end up with Postgres?"
Tokens:  How did we originally end up with Postgres ?
Labels:  B-ASK_CHANGE O O B-ORDINAL_FIRST O O O B-REFERENT O
```

"How … end up" as a compound ASK_CHANGE + "originally" as ORDINAL_FIRST. Composes to `TLGQuery(axis="ordinal", relation="first", referent="postgres")` — i.e. "what was our first choice that led to Postgres".

### Example 7: O-heavy

```
Query: "Thanks for the info."
Tokens:  Thanks for the info .
Labels:  O      O   O   O    O
```

No cues → `synthesize()` returns `None` → LLM fallback fires (and would likely classify as shape=none).

## Edge cases + annotator workflow

### Ambiguous "since"

"Since the audit, we added encryption" — TEMPORAL_SINCE? CAUSAL_EXPLICIT?

**Rule**: pick the sense that matches the SUBSTITUTION TEST:
- Substitute "ever since" — if sentence still makes sense, TEMPORAL_SINCE
- Substitute "because" — if sentence still makes sense, CAUSAL_EXPLICIT
- If both — the original is genuinely ambiguous; default to **CAUSAL_EXPLICIT** (the causal intent usually dominates in SDLC prose).

### Numerals in sentences

"The 3rd migration was smoother" — "3rd" = ORDINAL_NTH. Numerals without ordinal suffix ("3 services") are NOT ordinal.

### Multiple referents

"Did we use Redis before Memcached?" — both are catalog entries.
- "Redis" = REFERENT (the focal thing)
- "Memcached" = REFERENT (the after-anchor)

Synthesizer resolves the focus via position: first REFERENT after SUBJECT (if any) is the focal referent; REFERENT immediately following TEMPORAL_BEFORE/AFTER is the anchor.

### Missing SUBJECT

Many queries have implicit subjects ("What do we use now?"). Don't invent a subject — leave it unlabeled. The synthesizer defaults `subject=None` and the dispatcher falls back to the current active episode's subject.

## Annotator QA protocol (pilot phase)

1. **Two annotators label the pilot set independently.**
2. **Compute Cohen's κ per cue family.** Target ≥ 0.8 across all.
3. **Calibration meeting**: walk through any disagreements; refine guidelines in-place.
4. **Re-label 50 fresh queries.** If κ still < 0.8 on a specific family, re-design that family's guidelines.
5. **After pilot**: one annotator reviews 20% of LLM-generated batches at scale, flagging systematic drift.

## Prompt template for LLM cue-tagging

Used by the training-data generator in Phase 2 of the CTLG roadmap:

```
You are a cue-labeling annotator for a software-engineering query classifier.

Given a query, tag each WORD with a BIO cue label from this set:

[full label list from above, inline]

Output ONLY a JSON object:
{
  "text": "<original query>",
  "words": [<word strings>],
  "labels": [<label strings, same length as words>]
}

Use the guidelines in docs/research/ctlg-cue-guidelines.md — in
particular the SUBSTITUTION TEST for ambiguous cases and the
DISAMBIGUATION RULES at the end of each section.

Query: "<query here>"
```

## Change log

| Date | Change |
|------|--------|
| 2026-04-23 | Initial draft — 14 cue families from PDTB 3.0 + AltLex + TempEval + TLG needs |
