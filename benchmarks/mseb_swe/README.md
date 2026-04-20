# MSEB-SWE — Software-Dev State-Evolution Benchmark

Mines SWE-bench Verified (Princeton NLP) into the MSEB corpus /
queries schema.  One subject = one GitHub issue; one memory =
one message along the issue's resolution arc.

This domain exercises the P2 SLM's `admission_head`,
`state_change_head`, and `topic_head`.  It does not carry
preference signal — all MSEB-SWE gold queries have
`preference="none"`.  Preference sub-types are covered by
MSEB-Convo.

> Pre-paper mapping: §3a of
> `docs/p3-state-evolution-benchmark.md`, §4.2.1 of the MSEB
> results write-up (forthcoming).

---

## 1. Why SWE-bench Verified?

We need a software-dev corpus where:

- **Ground-truth resolution is machine-verifiable.**  SWE-bench
  ships `patch` + `test_patch` + `PASS_TO_PASS` + `FAIL_TO_PASS`,
  so "final state" is unambiguous.
- **Verified subset is clean.**  The 500-issue Verified split is
  hand-curated: no flaky tests, no ambiguous specs.  Standard
  across SWE-bench benchmarks (SWE-agent, SWE-rebench, Aider).
- **License is redistributable.**  All 12 upstream repos are
  MIT / BSD / Apache; SWE-bench aggregates under CC-BY-4.0.

Alternatives considered:

- *SWE-bench Lite (300 issues)* — no additional quality signal.
- *SWE-rebench* — monthly refresh makes the ground-truth label
  moving target; worse for a reproducible benchmark.
- *Princeton SWE-gym* — training data, not an eval split.

SWE-bench Verified is the community-standard eval, so comparable
to other published numbers.

## 2. Subject = issue, memory = one message along the fix arc

Every Verified row explodes into up to 4 raw messages:

| `source` | Where it comes from | Typical `kind` label |
| --- | --- | --- |
| `issue_body` | `problem_statement` (issue body at file time) | `declaration` (bug described) or `ordinal_anchor` (first report) |
| `pr_discussion` | `hints_text` (PR review / maintainer replies) | `causal_link` (root-cause analysis) |
| `resolving_patch` | `patch` (the PR that closes the issue) | `retirement` (old behavior replaced) + `causal_link` |
| `test_patch` | `test_patch` (added / modified tests) | `declaration` (invariant now enforced) |

The timestamps inherit from `created_at` (issue creation) — we
don't synthesize per-message times because SWE-bench doesn't
carry PR-comment timestamps reliably.  Ordinal shapes still work
because `patch` and `test_patch` conceptually *follow* the issue
body.

## 3. Search patterns (intent shapes) we target

All 14 MSEB intent shapes are exercised.  A representative
sample of handwritten gold queries (full set lands in
`gold.yaml`):

| Shape | Example query on an issue | Gold answer |
| --- | --- | --- |
| `current_state` | "What's the current behaviour of `separability_matrix` for nested CompoundModels in astropy?" | `patch` memory (fix landed) |
| `origin` | "Where was the separability-matrix bug first reported?" | `issue_body` |
| `ordinal_first` | "First observation on this issue?" | `issue_body` |
| `predecessor` | "What behaviour preceded the current fix?" | `issue_body` (pre-fix state) |
| `causal_chain` | "What chain of messages led to the patch?" | all 4 in order |
| `retirement` | "Which message retired the old `_cstack` implementation?" | `patch` |
| `before_named` | "What was the state before the test_patch landed?" | `patch` or `issue_body` |
| `concurrent` | "Discussion concurrent with the patch?" | `pr_discussion` |
| `transitive_cause` | "What caused the test_patch to be necessary?" | `issue_body` → `patch` chain |
| `noise` | query about an unrelated module | rejects all |

The first seven are the highest-value for real-world agent use.

## 4. Pipeline

```text
mine.py  (Phase 1)  →  raw/<instance_id>.jsonl          (cacheable)
label.py (Phase 2)  →  raw_labeled/<instance_id>.jsonl  (MemoryKind added)
build.py (Phase 3)  →  corpus.jsonl + gold.yaml → queries.jsonl
harness  (Phase 4)  →  per-shape rank-1 / top-5 metrics
```

Phase 1 (this file) is intentionally un-labeled — labeling is
prompt-sensitive and we want to iterate the classifier without
re-hitting HuggingFace.  Cached dataset sits in `~/.cache/huggingface/`.

## 5. Reproducibility

- Dataset: `princeton-nlp/SWE-bench_Verified` (HF hub)
- Split: `test` (the 500-issue verified split; HF puts it under "test")
- Load: `datasets.load_dataset(...)` — pinned by the `datasets`
  version in `pyproject.toml`
- Auth: `HF_TOKEN` loaded via `benchmarks/env.py` from project-root
  `.env`.  Required only if rate-limited; anonymous pulls work.

## 6. Running

```bash
# Pilot — first 50 issues
uv run python -m benchmarks.mseb_swe.mine --limit 50

# Full scale — all 500
uv run python -m benchmarks.mseb_swe.mine --limit 500
```

Output: `raw/<instance_id>.jsonl` + `raw/_stats.json`.  Durable
logs go to `benchmarks/mseb/run-logs/swe-pilot-<ts>.log` when run
from the scripts in `benchmarks/mseb/`.

## 7. Pilot results (2026-04-20)

- 50 issues in → 50 issues kept (100% retention)
- 188 messages emitted
- Per-source counts: `issue_body=50`, `pr_discussion=38`,
  `resolving_patch=50`, `test_patch=50`
- Coverage gap: first 50 issues in dataset order are
  alphabetical (astropy=22, django=28).  Full-scale pull covers
  all 12 repos; for representative pilots use
  `--limit 500 | shuf | head -50`.

**Applicability: strong.**  Every row had a problem_statement +
patch + test_patch; 76 % had PR-discussion hints.  All four
`MemoryKind` labels are represented with low ambiguity.

See `benchmarks/mseb/run-logs/pilot-applicability-*.md` for the
full analysis.
