# Claims-Format Spec

**Status:** verification protocol for `phase-{a,b,c}-claims.md`
**Audience:** codex (or any reviewer auditing a Subject-Graph PR)
**Owner:** NCMS core

## What this is

Each phase of the subject-centered graph rollout is shipped against a
**claim doc** (`phase-a-claims.md`, etc.) written before the
production code lands. The claim doc enumerates every assertion the
PR is responsible for, with a concrete way to falsify it.

The reviewer's job is to falsify the claims, not to read the code
sympathetically. Pass means *every* claim verifiably holds against
the diff; fail means at least one claim is unsupported.

## Claim categories

| Tag | Meaning |
|---|---|
| `[SCHEMA]` | A class/file/field/migration exists with a specific shape. Verifiable with grep + import. |
| `[API]` | A public function signature or kwarg. Verifiable with grep + a one-line Python check. |
| `[BEHAVIOR]` | Given input X, the system produces output Y. Verifiable with a named test. |
| `[COVERAGE]` | Every site that should be updated, was. Verifiable by enumerating sites. |
| `[PARITY]` | Path A and path B produce equivalent output for the same input. Verifiable with a parity test. |
| `[NEGATIVE]` | This code/behavior did NOT change. Verifiable with grep + diff. |
| `[PERF]` | A latency or throughput threshold. Verifiable with a benchmark run. |

## Reproducible verify commands

**All Python imports must use `uv run python`, never bare `python`.**
The repo uses `uv` for env management; bare `python` produces
`ModuleNotFoundError: No module named 'ncms'` for environment
reasons that have nothing to do with code state. A reviewer running
into that gets a useless audit signal.

Correct:
```
uv run python -c "from ncms.domain.models import EdgeType; print(len(list(EdgeType)))"
```

Incorrect:
```
python -c "from ncms.domain.models import EdgeType; print(len(list(EdgeType)))"
```

Same rule for any verify line that imports the package.

## Pre-conditions per phase

Each phase doc's pre-conditions assert state on a specific commit:

| Phase | Pre-conditions verified against |
|---|---|
| A | `main` *today* (no merge prereqs) |
| B | the commit immediately after Phase A merges |
| C | the commit immediately after Phase B merges |

A reviewer auditing Phase B's pre-conditions on a commit where
Phase A hasn't merged will see ❌ failures by definition. That's
expected — the doc is verifiable in sequence, not in parallel.

The pre-flight audit (before any code lands) verifies **Phase A
pre-conditions only**. Phase B and C pre-conditions are verified
after their predecessor phase ships.

## Claim shape

Every claim follows this template:

```
### <ID> — <one-line summary>
**[TAG]** — optional second tag
**Pre:** what must be true on `main` before the PR (often "the new thing does not exist yet")
**Post:** what must be true after the PR ships
**Verify:**
  - `<command or test path>` — expected result
  - `<command or test path>` — expected result
**Failure mode:** what's the symptom if this claim is false in the wild
**Citation:** file:line where the change lives (NEW for new files)
```

## Pre-conditions vs delivered claims

Each phase doc has two zones:

1. **Pre-conditions (PC-X.N):** must hold on `main` before this phase
   starts. If a pre-condition fails, the audit was wrong; either the
   doc needs revision or main has drifted. Pre-conditions are not
   delivered by this PR.

2. **Delivered claims (X.N):** the assertions the PR is responsible
   for. Each one must be verifiable from the diff alone.

Reviewer checks pre-conditions first (one-shot, ~2 minutes). If
any fail, stop and flag the design doc. Otherwise proceed to
delivered claims.

## Verification commands

Every `Verify:` line is a literal command codex (or a script) can
run. Examples:

- `grep -n "class Subject" src/ncms/domain/models.py` — exact-string
- `uv run python -c "from ncms.domain.models import Subject; assert ..."` — import
- `uv run pytest tests/integration/test_subject_payload_parity.py::test_inline_async_parity` — test
- `sqlite3 :memory: "EXPLAIN QUERY PLAN SELECT ... ;" | grep idx_mnodes_subject` — query plan
- `uv run radon cc <path> -nc --min D` — complexity gate

Commands are reproducible: any reviewer with the repo checked out
can run them. No "trust me, I checked" lines.

## Negative claims (regression scope)

Every phase doc includes a `[NEGATIVE]` section enumerating code/
behavior that *did not* change. These exist to bound the regression
risk. If a negative claim is violated (a file was modified that
shouldn't have been), reviewer flags scope creep.

Example:

```
### NEG-A.1 — L2 ENTITY_STATE schema unchanged
**[NEGATIVE]**
**Verify:**
  - `git diff main -- src/ncms/domain/models.py | grep -E "^[+-]\s+(state_key|state_value|entity_id|valid_from|valid_to|observed_at|ingested_at)"`
**Expected:** No output — none of these fields' lines changed.
**Failure mode:** Phase A inadvertently changed L2 schema; reconciliation regression risk.
```

## Behavioral parity tests

Refactors that touch multi-path code (e.g. inline + async ingest)
require a `[PARITY]` claim with a named test that exercises both
paths against the same input and asserts equivalent output. Without
this test, the PR cannot merge.

The Phase G audit found that `index_worker.py` had silently diverged
from `ingestion/pipeline.py` for L2 detection, GLiNER usage, and
several other features — *because no test exercised both paths*.
The parity test is permanent insurance against the next divergence.

## What the reviewer reports back

For each claim, codex's audit response is one of:

- ✅ **Verified** — command ran, output matches expected
- ❌ **Failed** — command ran, output does NOT match expected (link to actual output)
- ⚠️ **Unverifiable** — claim is too vague or verification command doesn't actually test what the claim says (push back to author for clarification)
- 🚧 **Partial** — claim verifies for some sites but not others (e.g. inline ingest has it, async ingest doesn't)

The PR ships only when every claim is ✅ or has a documented exception
that the user has accepted in writing.

## Drift safeguards

These docs do not eliminate drift. They make drift *visible*. The
patterns they catch:

- "I touched memory_service.py but didn't update index_worker.py" → `[COVERAGE]` claim fails.
- "I claim L2 schema unchanged but added a field" → `[NEGATIVE]` claim fails.
- "I claim parity but only have a one-direction test" → reviewer flags as ⚠️ Unverifiable.
- "I claim the migration applies but there's no test for the upgrade path" → reviewer asks for one.

The patterns they do NOT catch:

- Claims I forgot to write. The doc is exhaustive, but only if the
  audit before authoring was exhaustive.
- Subtle semantic drift inside a function whose signature I claim is
  unchanged. The `[NEGATIVE]` shape catches "this file changed";
  it does not catch "this function still has the same signature
  but the inner logic now does something different."

For semantic-drift cases, behavioral tests (`[BEHAVIOR]` and
`[PARITY]`) are the second line of defense. If you're skeptical, ask
for more behavioral claims rather than trusting the schema/coverage
ones.
