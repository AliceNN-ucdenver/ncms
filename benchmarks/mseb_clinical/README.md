# MSEB-Clinical — Clinical State-Evolution Benchmark

Mines PMC (PubMed Central) Open Access case reports filtered on
MeSH `Diagnosis, Differential` / `Diagnostic Errors` into the
MSEB corpus / queries schema.  One subject = one case report;
one memory = one narrative section along the diagnostic arc.

This domain exercises the P2 SLM's `admission_head`,
`state_change_head`, and `topic_head`.  It does not carry
preference signal — all MSEB-Clinical gold queries have
`preference="none"`.  Preference sub-types are covered by
MSEB-Convo.

> Pre-paper mapping: §3b of
> `docs/p3-state-evolution-benchmark.md`, §4.2.2 of the MSEB
> results write-up (forthcoming).

---

## 1. Why PMC Open Access + differential-diagnosis MeSH?

The clinical instantiation exists because **TLG's intent shapes
were designed against clinical multi-step diagnosis** — a patient
presents, initial hypotheses are ruled out, new tests arrive,
diagnosis is revised.  This is the state-evolution arc the memory
system was originally motivated by (see README §1 of the main
docs).

We need a corpus that:

- Contains **multi-step diagnostic narratives** — not isolated
  findings.  MeSH `Diagnosis, Differential`[MeSH] and `Diagnostic
  Errors`[MeSH] both imply the paper *reasons* through alternatives.
- Is **redistributable** under CC-BY (so corpus JSONL can be
  committed to the benchmark repo).  `open access[filter]` on PMC
  restricts to CC-BY / CC-0 / equivalent.
- Ships **structured sections** (abstract, case presentation,
  differential, investigation, final diagnosis).  JATS XML gives
  us that for free — see `SECTION_PRIORITY` in `mine.py`.

Alternatives considered:

- *MIMIC-IV / eICU* — requires CITI training + DUA; not
  redistributable; can't ship gold labels in a public repo.
- *PubMed abstracts* — lose the narrative arc; abstract-only is
  not a state evolution, it's a summary.
- *MedQA / MedMCQA* — question-answering datasets, not clinical
  timelines.
- *BioASQ* — IR but passage-level; no per-message temporal
  structure.

PMC-OA case reports tagged with these two MeSH terms are the
only corpus that (a) has the narrative, (b) is CC-BY, and
(c) is machine-parseable (JATS XML).

## 2. Search query

```text
("Diagnosis, Differential"[MeSH Terms]
   OR "Diagnostic Errors"[MeSH Terms])
 AND "Case Reports"[Publication Type]
 AND "open access"[filter]
 AND English[Language]
```

Pilot esearch returns **4,440 matching PMCIDs** — ample headroom
for a 200-paper full-scale pull after filtering.

The query is intentionally broad at search time; narrowing
happens at extraction time:

1. JATS parser rejects non-CC-BY licenses (see `_license_ok`).
2. Section extractor ignores headings < 40 chars of body content.
3. Optional post-filter (§6) removes methods/review papers that
   NCBI mis-tags as Case Reports.

## 3. Subject = paper, memory = section

Every retained PMC paper explodes into one message per narrative
section.  The `source` field tags each message by heading type.
Matched headings follow the canonical clinical case-report arc:

| `source` | Narrative role | Typical `MemoryKind` |
| --- | --- | --- |
| `abstract` | summary ("first presentation + outcome") | `ordinal_anchor` |
| `introduction` / `background` | context, prior literature | `none` |
| `case presentation` / `case report` / `history` | initial symptoms | `declaration` (initial state) |
| `physical examination` / `investigations` / `workup` | evidence gathered | `declaration` (findings) |
| `differential diagnosis` | hypotheses considered | `declaration` (candidates) |
| `initial diagnosis` | first working diagnosis | `declaration` (state), later often `retirement` |
| `management` / `treatment` | intervention | `causal_link` |
| `course` / `outcome` / `follow-up` | response to treatment | `declaration` (state update) |
| `final diagnosis` | the answer (when revised) | `retirement` (of earlier diagnosis) + `declaration` (new state) |
| `discussion` / `conclusion` | retrospective reasoning | `causal_link` (why misdiagnosed) |
| `other` | unmatched heading (kept, marked generic) | classifier TBD |

**Timestamps.**  PMC carries publication date but not
per-section event times.  The mined `timestamp` is always the
`pub-date`.  Gold queries that depend on intra-section ordering
(e.g. `ordinal_first`, `sequence`) use `source` as a proxy for
ordinal position — the SECTION_PRIORITY tuple is ordered by the
canonical clinical arc.

## 4. Pipeline

```text
mine.py   (Phase 1)  →  raw/PMC<id>.xml  (cached JATS)
                        raw/PMC<id>.jsonl (messages)
label.py  (Phase 2)  →  raw_labeled/PMC<id>.jsonl
build.py  (Phase 3)  →  corpus.jsonl + gold.yaml → queries.jsonl
harness   (Phase 4)  →  per-shape rank-1 / top-5 metrics
```

Phase 1 caches XML on disk so label-prompt iteration costs 0
NCBI requests.

## 5. Reproducibility

- API: NCBI eutils (`esearch` → `efetch`, `db=pmc`)
- Rate limit: 3 req/s without key, 10 req/s with `NCBI_API_KEY`
  in `.env`.  Honours `NCBI_EMAIL` per Entrez policy.
- Retries: 3× exponential backoff on 429 / 5xx.
- License verification: every paper's `<license>` element
  inspected; only `cc-by-*` retained.  Non-CC-BY counted in
  `_stats.json["skipped_reasons"]["non_cc_by_license"]`.

## 6. Known limitations

1. **Publication-type tagging is noisy.**  NCBI tags some
   methods/review papers (e.g. "agentic system for rare disease
   diagnosis") as `Case Reports`.  Pilot shows ~30 % of the kept
   papers are methods/review-like.  Post-filter heuristic (coming
   in `label.py`): require at least one of
   {`case presentation`, `case report`, `history`, `physical
   examination`, `investigations`, `patient`} in the source set.
2. **Differential-diagnosis reasoning often lives under
   `Discussion`, not under an explicit `Differential Diagnosis`
   heading.**  Pilot found only 3/41 papers with that exact
   heading.  Don't over-weight `source=differential diagnosis`.
3. **`other` is the largest bucket** (51 % of messages in pilot).
   Case reports use bespoke headings ("Case Description",
   "Lessons learned", "Patient and observation").  Do not drop
   `other` — it carries the narrative.
4. **No patient-level timestamps.**  Section ordering is the only
   intra-paper temporal signal.  Works for ordinal shapes,
   doesn't work for `interval` / `range` with real clock times
   (omit those shapes for MSEB-Clinical in the first pass).

## 7. Running

```bash
# Pilot — 50 papers (~15 s with default 3 rps; ~5 s with NCBI_API_KEY)
uv run python -m benchmarks.mseb_clinical.mine --limit 50

# Full scale — 200 papers (~60 s with API key)
uv run python -m benchmarks.mseb_clinical.mine --limit 200
```

Output: `raw/PMC<id>.jsonl` + `raw/xml/PMC<id>.xml` (cached) +
`raw/_esearch.json` + `raw/_stats.json`.  Durable logs go to
`benchmarks/mseb/run-logs/clinical-pilot-<ts>.log`.

## 8. Pilot results (2026-04-20)

- 50 PMCIDs requested → 41 papers kept (82 % retention)
- 530 messages emitted
- 7 skipped (non-CC-BY licenses)
- 2 skipped (efetch 400 errors — likely retraction / embargoed)
- Per-source: `abstract=41`, `case presentation=22`, `case report=20`,
  `discussion=38`, `conclusion=57`, `introduction=36`,
  `differential diagnosis=3`, `final diagnosis=1`, `other=271`

**Applicability: good, with caveats.**

- **Exemplar case** (PMC12976445, "Human Immunodeficiency Virus
  Misdiagnosis"):  negative HIV rapid tests → wife positive
  (causal context) → HIV RNA test ordered → positive →
  prior-negative diagnosis retired.  Textbook MSEB state
  evolution; every intent shape except `interval` / `range`
  has a clear answer in the text.
- **Problem case** (PMC12999473, "agentic system for rare
  disease diagnosis"):  methods paper mis-tagged as Case
  Reports.  Addressed by post-filter in §6.1.
- ~68 % of kept papers match the case-report narrative heuristic
  directly; an estimated 80-85 % match after the post-filter
  is tuned (several "methods-like" papers actually are case
  reports with headings like "Case Description" / "Case Summary").

See `benchmarks/mseb/run-logs/pilot-applicability-*.md` for the
full analysis.
