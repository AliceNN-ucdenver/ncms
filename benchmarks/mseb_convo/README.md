# MSEB-Convo — Conversational State-Evolution + Preference Benchmark

Wraps the LongMemEval corpus into the MSEB schema and adds
hand-authored preference gold queries.  One subject = one
LongMemEval question's full haystack; one memory = one turn.
This is the domain that exercises the P2 `intent_head`'s four
preference sub-types — the two earlier domains (SWE, Clinical)
don't carry preference signal.

> Pre-paper mapping: §3c of
> `docs/p3-state-evolution-benchmark.md`, §4.2.3 of the MSEB
> results write-up (forthcoming).

---

## 1. Why LongMemEval?

We need a conversational corpus where:

- **Users declare preferences** naturally across sessions
  (favorite tools, dietary restrictions, daily routines,
  recurring struggles).
- **Sessions are dated** so the harness can evaluate temporal
  shapes (`sequence`, `predecessor`, `interval`) against
  real time anchors.
- **Preferences sometimes change** (yesterday's "I love X"
  becomes today's "I've switched to Y") so we get
  `retirement` + `knowledge-update` coverage for free.
- **Already benchmarked in the repo** — sits next to our prior
  LongMemEval A/B run (`benchmarks/longmemeval/`), same adapter
  (`conversational/v4`), same corpus cache.

LongMemEval ships 500 users × ~20 sessions × ~10-30 turns per
session, already cached at
`benchmarks/results/.cache/longmemeval/`.  MIT-ish license (verify
before redistribution).

## 2. The label gap — and how we close it

LongMemEval's own question categories do **not** sub-type
preferences.  Its 30 `single-session-preference` questions have
free-text answers that blend positive and negative preferences
into a single paragraph (e.g. *"the user would prefer X, they
may not prefer Y"*).  That's useful for a free-text
preference-following eval, but it doesn't supply the
`{positive, avoidance, habitual, difficult}` labels the P2
`intent_head` emits.

**Our approach.**  Treat LongMemEval's corpus as the substrate;
hand-author the preference labels ourselves:

1. `mine.py` (this directory) — exports the corpus into MSEB
   `CorpusMemory` format, one file per subject.
2. `label.py` (next sprint) — applies heuristic + LLM
   classification for `MemoryKind` (state-evolution).  Preference
   kind defaults to `none` at this stage.
3. `gold.yaml` (authored by us) — **100 preference queries** (25
   per sub-type × 4 sub-types) plus 100 state-evolution queries
   re-annotated from LMEval's own question bank.

The authored queries cite exact `message_id`s, making grading
deterministic and making the benchmark reproducible across memory
systems.

## 3. Subject = question, memory = one turn

Every LMEval question-row produces one subject:
`user-<question_id[:8]>`.  Its sessions flatten into messages:

| `source` | LMEval origin | Typical `MemoryKind` / `PreferenceKind` |
| --- | --- | --- |
| `user_turn` | `haystack_sessions[k][j]` with `role="user"` | preference declarations + state reveals |
| `assistant_turn` | `haystack_sessions[k][j]` with `role="assistant"` | mostly `none`; occasional causal_link when the assistant confirms prior state |

Timestamps use the session date (LMEval granularity stops at the
day; `observed_at` is the day at 00:00 UTC).  Temporal shapes
that need intra-day ordering are answered by turn-index tiebreak.

## 4. Preference taxonomy (matches `intent_head`)

| `preference` | Declaration pattern | Example turn |
| --- | --- | --- |
| `positive` | affirmation, use, prefer | "I use Premiere Pro for editing" |
| `avoidance` | negation, can't, allergic, won't | "I can't eat shellfish" |
| `habitual` | frequency, usually, every, routine | "Every morning I do a 5-min meditation" |
| `difficult` | struggle, hard, issue, pain point | "I struggle with focusing in open-plan offices" |

Queries targeting each sub-type exercise the classifier's head
in isolation when the harness runs with `--head intent`.

## 5. Search patterns we target

All 14 MSEB intent shapes, PLUS a per-preference breakdown.  The
richest cells:

| Shape × Preference | Example query | Gold answer |
| --- | --- | --- |
| `current_state` × `positive` | "What video editor does the user use?" | turn where user said "I use Premiere Pro" |
| `current_state` × `avoidance` | "What dietary restriction should the assistant remember?" | turn where user said "I can't eat shellfish" |
| `current_state` × `habitual` | "What's the user's morning routine?" | turn with "every morning I …" |
| `predecessor` × `positive` | "What video editor did the user use before Premiere Pro?" | earlier session declaring DaVinci Resolve |
| `retirement` × `positive` | "Which preference did the user change away from?" | the "I've switched from X to Y" turn |
| `causal_chain` × `difficult` | "Why does the user struggle with focus?" | turn explaining open-plan office |

## 6. Pipeline

```text
mine.py   (Phase 1)  →  raw/<subject>.jsonl   + raw/_questions.jsonl
label.py  (Phase 2)  →  raw_labeled/<subject>.jsonl (MemoryKind + PreferenceKind)
gold.yaml (author)   →  queries.jsonl via build.py
harness   (Phase 4)  →  per-shape × per-preference × per-head metrics
```

Caching: LongMemEval source JSON is already on disk;
`mine.py` is pure-Python, no network.

## 7. Running

```bash
# Ensure LMEval cache is populated (one-time)
uv run python -m benchmarks longmemeval --test   # populates the cache

# Pilot — 50 questions → 50 subjects
uv run python -m benchmarks.mseb_convo.mine --limit 50

# Full scale — 500 questions
uv run python -m benchmarks.mseb_convo.mine --limit 500
```

Output: `raw/<subject>.jsonl` + `raw/_questions.jsonl` +
`raw/_stats.json`.  Durable logs go to
`benchmarks/mseb/run-logs/convo-pilot-<ts>.log` when launched via
the repo's standard run scripts.

## 8. Pilot targets (not yet run)

| Slice | Subjects | Messages (est.) | Gold queries |
| --- | --- | --- | --- |
| Pilot | 50 | ~1,000 | 50 (10 per preference sub-type + 10 state-evolution) |
| Full | 500 | ~10,000 | 200 (25 per preference × 4 + 100 state shapes) |

LMEval's own mix (500 questions) covers the shape axis
naturally — `multi-session` ≈ `predecessor` / `causal_chain`;
`temporal-reasoning` ≈ `ordinal_*` / `sequence` / `interval`;
`knowledge-update` ≈ `retirement`.

## 9. Pilot finding — LMEval's preference distribution is uneven

Running `label.py` on a shuffled 50-user pilot (seed 42) gave:

| PreferenceKind | Turns labeled | Note |
| --- | --- | --- |
| `positive` | 26 | common — "I use Premiere Pro" / "I love …" / "my favourite …" |
| `habitual` | 14-16 | common — "every morning" / "I usually" |
| `avoidance` | **0** | **rare** — LMEval users mostly ask "should I avoid X?" rather than declare "I avoid X" |
| `difficult` | **0** | **rare** — same reason; users describe their interests, not their struggles |

The avoidance/difficult gap is a **property of the LongMemEval
corpus**, not a labeler bug.  Inspection of all 50 pilot users
found only 5 raw `avoid` mentions and 8 `difficult` mentions —
and most of those are questions, not declarations.

**Mitigation (gold-authoring sprint):**

1. Target the full 500-question LMEval with `--shuffle-seed 42
   --limit 500` so `single-session-preference` questions
   (n=30 in LMEval) all enter the corpus.
2. Where avoidance/difficult declarations exist in the raw
   turns, author gold queries that cite them as `gold_mid`.
3. If coverage is still thin (<20 queries per sub-type),
   synthesize ~10 avoidance + ~10 difficult declarations per
   sub-type using the P2 SDG tooling
   (`experiments/intent_slot_distillation/sdg/`) and inject them
   as new turns at known session boundaries.  This is the same
   synthetic-augmentation strategy MSEB-Clinical uses.

Either way: hand-authored gold queries fix the per-preference
n=20-25 target.  The mechanical labels will remain sparse, which
is fine — the benchmark grades retrieval, not labeler recall.

## 10. Status

| Step | Status |
| --- | --- |
| `mine.py` — LMEval → `CorpusMemory`-shaped JSONL (`--question-types`, `--shuffle-seed`) | ✅ done (50-user pilot smoke-tested) |
| `label.py` — MemoryKind + PreferenceKind classifier | ✅ done (rule-based, pos/habit covered, avoid/diff corpus-thin) |
| `gold.yaml` — 200 hand-authored gold queries (50 per sub-type + 100 state-evolution) | pending — next sprint |
| Full-scale corpus (500 users) + analysis | pending (triggered by full-run CLI) |
