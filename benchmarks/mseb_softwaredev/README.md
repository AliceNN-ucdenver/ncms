# MSEB-SoftwareDev — ADR Prose State-Evolution Benchmark

Mines hand-authored **Architecture Decision Records (ADRs)** from
the public
[`joelparkerhenderson/architecture-decision-record`](https://github.com/joelparkerhenderson/architecture-decision-record)
reference collection into the MSEB corpus / queries schema.  One
subject = one ADR; one memory = one structured section (Context,
Decision, Consequences, Status, Supersedes, …).

This domain exercises the P2 SLM's **`topic_head`** (tags each
section with an architecture label from the
`software_dev/v4` taxonomy) and **`state_change_head`** (detects
`Supersedes` / `Deprecates` / `Status: Accepted` declarations) on
**prose** content — distinct from MSEB-SWE which operates on
**raw git diff** content.

> Pre-paper mapping: §3c of
> [`docs/completed/p3-state-evolution-benchmark.md`](../../docs/completed/p3-state-evolution-benchmark.md),
> §3.1 of [`docs/mseb-results.md`](../../docs/mseb-results.md).

---

## 1. Why ADR prose?

ADRs are purpose-built state-evolution documents: each one records
a decision, links to what it supersedes, and declares consequences
other ADRs later reference.  The reference collection is:

- **Permissively licensed** (Creative Commons / attribution) so
  corpus JSONL can be committed.
- **Cross-domain in subject** — cloud, security, data, testing —
  so no single vocabulary dominates.
- **Structured** — standard ADR template (Context / Decision /
  Consequences / Supersedes / Status) gives us clean section
  boundaries.
- **Not in any public LLM training benchmark** — so adapter
  evaluations on this corpus are close to pretraining-clean.

## 2. Domain signature

| | Value |
| --- | --- |
| Adapter | `software_dev/v4` (prose-state) |
| Topic labels | `architecture` / `api` / `database` / `infrastructure` / `security` / `testing` / `other` |
| State-change signals | `Status: Accepted`, `Supersedes ADR-...`, `Deprecated by ...` |
| Query classes present | `general` / `temporal` / `noise` (no preference) |
| Mini corpus size | 132 memories, 165 queries (181 audited-pass / 201 authored) |

Preference sub-types are covered by **MSEB-Convo** and are not
relevant to ADR content by construction — ADRs record *team*
decisions, not *personal* preferences.

## 3. Pipeline

```bash
# 1. Mine — clone the ADR reference and rasterise into raw JSONL
uv run python -m benchmarks.mseb_softwaredev.mine \
    --source adr_jph \
    --src-dir /tmp/jph-adr \
    --out-dir benchmarks/mseb_softwaredev/raw

# 2. Label — deterministic rule-based section typing
uv run python -m benchmarks.mseb_softwaredev.label

# 3. Build + mini + gold audit (shared with other MSEB domains)
cp benchmarks/mseb_softwaredev/gold_locked.yaml \
   benchmarks/mseb_softwaredev/gold.yaml
uv run python -m benchmarks.mseb.build \
    --labeled-dir benchmarks/mseb_softwaredev/raw_labeled \
    --gold-yaml benchmarks/mseb_softwaredev/gold.yaml \
    --out-dir benchmarks/mseb_softwaredev/build
uv run python -m benchmarks.mseb.mini \
    --src benchmarks/mseb_softwaredev/build \
    --out benchmarks/mseb_softwaredev/build_mini \
    --subjects 25
```

## 4. Results snapshot (from main12 mini)

| Backend | General r@1 | Temporal r@1 | Noise reject |
| --- | ---: | ---: | ---: |
| NCMS tlg-on | **0.961** | 0.725 | 100 % |
| NCMS tlg-off | 0.974 | 0.710 | 100 % |
| mem0 (dense) | 0.513 | 0.522 | 100 % |

NCMS beats mem0 by **+0.45 on general, +0.20 on temporal**.  See
[`docs/mseb-results.md`](../../docs/mseb-results.md) §3.1 for the
full discussion and per-head SLM contribution analysis.
