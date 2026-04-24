"""v9 Phase 1 — pre-regeneration gold corpus audit.

Pure static analysis over ``adapters/corpora/*.jsonl``.  No API
calls, no training.  Decides per-domain whether existing gold is
salvageable (small label edits + SDG refresh) or needs a full
regeneration with OpenAI in Phase 2.

Stats per domain × split:

  * row count
  * intent class histogram (6 classes)
  * admission class histogram (3 classes)
  * state_change class histogram (3 classes)
  * topic class histogram (domain-specific taxonomy)
  * slot coverage (rows with at least one slot vs empty)
  * role_spans coverage (rows with explicit role_spans vs empty)
  * gazetteer hit rate (rows where detect_spans finds >=1 surface)
  * mean / p95 / max text length

Writes the full report to ``docs/forensics/gold-audit-pre-v9.md``
and prints a terse summary + per-domain verdict to stdout.

Usage::

    uv run python scripts/ctlg/audit_gold_pre_v9.py

Exit code 0 always — this is an audit, not a gate.
"""

from __future__ import annotations

import random
import statistics
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.application.adapters.corpus.loader import load_jsonl  # noqa: E402
from ncms.application.adapters.schemas import (  # noqa: E402
    ADMISSION_DECISIONS,
    INTENT_CATEGORIES,
    STATE_CHANGES,
)
from ncms.application.adapters.sdg.catalog import detect_spans  # noqa: E402

DOMAINS = ("conversational", "clinical", "software_dev", "swe_diff")
SPLITS = ("gold", "sdg", "adversarial_train")


def _audit_rows(rows: list, domain: str) -> dict:
    """Compute aggregate stats for one (domain, split) slice."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    intents = Counter(r.intent or "none" for r in rows)
    admissions = Counter(str(r.admission) for r in rows)
    state_changes = Counter(str(r.state_change) for r in rows)
    topics = Counter(str(r.topic) for r in rows)

    rows_with_slots = sum(1 for r in rows if r.slots)
    rows_with_role_spans = sum(1 for r in rows if r.role_spans)
    rows_with_gaz_hit = sum(
        1 for r in rows if detect_spans(r.text, domain=domain)
    )
    total_gaz_hits = sum(
        len(detect_spans(r.text, domain=domain)) for r in rows
    )

    lengths = [len(r.text) for r in rows]
    return {
        "n": n,
        "intents": dict(intents),
        "admissions": dict(admissions),
        "state_changes": dict(state_changes),
        "topics": dict(topics),
        "rows_with_slots": rows_with_slots,
        "rows_with_role_spans": rows_with_role_spans,
        "rows_with_gaz_hit": rows_with_gaz_hit,
        "total_gaz_hits": total_gaz_hits,
        "len_mean": round(statistics.mean(lengths), 1),
        "len_p95": round(
            lengths[int(len(lengths) * 0.95)] if lengths else 0, 1,
        ),
        "len_max": max(lengths) if lengths else 0,
    }


def _fmt_counter(c: dict[str, int], vocab: tuple[str, ...] = ()) -> str:
    """Render a class histogram, preserving vocab order when given."""
    if not c:
        return "(empty)"
    keys = (
        list(vocab) + sorted(k for k in c if k not in vocab)
        if vocab else sorted(c, key=lambda k: -c[k])
    )
    parts = [f"{k}={c.get(k, 0)}" for k in keys if c.get(k, 0)]
    return " ".join(parts) if parts else "(empty)"


def _load_or_empty(path: Path) -> list:
    try:
        return load_jsonl(path)
    except FileNotFoundError:
        return []


def _sample(rows: list, k: int, seed: int) -> list:
    rng = random.Random(seed)
    if len(rows) <= k:
        return list(rows)
    return rng.sample(rows, k)


def _domain_verdict(gold_stats: dict, sdg_stats: dict) -> str:
    """Heuristic verdict: regenerate / salvage / review.

    Regenerate when:
      * gold has fewer than 150 rows, OR
      * role_spans coverage <60% of gold, OR
      * intent distribution skews >70% to a single class
    """
    n_gold = gold_stats.get("n", 0)
    if n_gold == 0:
        return "regenerate (no gold on disk)"
    if n_gold < 150:
        return f"regenerate (gold too small: {n_gold} < 150)"
    role_cov = gold_stats.get("rows_with_role_spans", 0) / max(n_gold, 1)
    if role_cov < 0.60:
        return f"regenerate (role_spans coverage {role_cov:.0%} < 60%)"
    intent_counts = gold_stats.get("intents", {})
    if intent_counts:
        top = max(intent_counts.values())
        if top / max(n_gold, 1) > 0.70:
            dominant = max(intent_counts, key=intent_counts.get)
            return (
                f"regenerate (intent={dominant!r} dominates "
                f"{top}/{n_gold} = {top/n_gold:.0%})"
            )
    # SDG coverage is helpful but not blocking.
    n_sdg = sdg_stats.get("n", 0)
    if n_sdg < n_gold * 2:
        return f"salvage + refresh SDG (SDG {n_sdg} < 2×gold {2*n_gold})"
    return "salvage (looks usable as-is after spot-check)"


def main() -> None:
    corpora_dir = _REPO / "adapters/corpora"
    out_dir = _REPO / "docs/forensics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "gold-audit-pre-v9.md"

    report: list[str] = []
    w = report.append
    w("# v9 Phase 1 — gold audit (pre-regeneration)")
    w("")
    w(
        "Static audit of existing `adapters/corpora/*.jsonl` files "
        "before Phase 2 OpenAI-driven regeneration.  Counts label "
        "distributions, role_span coverage, and gazetteer hit rate "
        "per domain.  Stratified samples appear at the bottom for "
        "human spot-check."
    )
    w("")

    verdicts: dict[str, str] = {}

    for dom in DOMAINS:
        w(f"## Domain: `{dom}`")
        w("")
        dom_stats: dict[str, dict] = {}
        for split in SPLITS:
            path = corpora_dir / f"{split}_{dom}.jsonl"
            rows = _load_or_empty(path)
            stats = _audit_rows(rows, dom) if rows else {"n": 0}
            dom_stats[split] = stats

            if stats["n"] == 0:
                w(f"### `{split}_{dom}.jsonl` — not present / empty")
                w("")
                continue

            w(f"### `{split}_{dom}.jsonl` — {stats['n']} rows")
            w("")
            w(
                f"- intent:   {_fmt_counter(stats['intents'], INTENT_CATEGORIES)}"
            )
            w(
                f"- admission: {_fmt_counter(stats['admissions'], ADMISSION_DECISIONS)}"
            )
            w(
                f"- state_change: {_fmt_counter(stats['state_changes'], STATE_CHANGES)}"
            )
            w(f"- topics: {_fmt_counter(stats['topics'])}")
            w(
                f"- rows_with_slots: {stats['rows_with_slots']} "
                f"({stats['rows_with_slots']/stats['n']:.0%})"
            )
            w(
                f"- rows_with_role_spans: {stats['rows_with_role_spans']} "
                f"({stats['rows_with_role_spans']/stats['n']:.0%})"
            )
            w(
                f"- rows_with_gazetteer_hit: {stats['rows_with_gaz_hit']} "
                f"({stats['rows_with_gaz_hit']/stats['n']:.0%}) — "
                f"total {stats['total_gaz_hits']} surfaces detected"
            )
            w(
                f"- text length: mean={stats['len_mean']}  "
                f"p95={stats['len_p95']}  max={stats['len_max']}"
            )
            w("")

        verdict = _domain_verdict(dom_stats["gold"], dom_stats["sdg"])
        verdicts[dom] = verdict
        w(f"**Verdict:** {verdict}")
        w("")

        # Per-domain stratified sample of gold for human eyeballing.
        gold_rows = _load_or_empty(corpora_dir / f"gold_{dom}.jsonl")
        if gold_rows:
            sample = _sample(gold_rows, 20, seed=42)
            w("#### 20-row stratified sample from gold")
            w("")
            for i, ex in enumerate(sample):
                w(f"**{i+1}.** `{ex.text}`")
                row_summary = (
                    f"    intent={ex.intent!r}  "
                    f"admission={str(ex.admission)!r}  "
                    f"state_change={str(ex.state_change)!r}  "
                    f"topic={ex.topic!r}"
                )
                w(row_summary)
                if ex.slots:
                    w(f"    slots={ex.slots}")
                if ex.role_spans:
                    rs = [
                        f"{r.surface}→{r.role}({r.slot})"
                        for r in ex.role_spans[:5]
                    ]
                    more = "" if len(ex.role_spans) <= 5 else f" +{len(ex.role_spans)-5}"
                    w(f"    role_spans: {'  '.join(rs)}{more}")
                w("")

    # Top-level summary
    w("## Summary")
    w("")
    w("| Domain | Gold | SDG | Verdict |")
    w("|---|---:|---:|---|")
    corpora_dir = _REPO / "adapters/corpora"
    for dom in DOMAINS:
        n_gold = len(_load_or_empty(corpora_dir / f"gold_{dom}.jsonl"))
        n_sdg = len(_load_or_empty(corpora_dir / f"sdg_{dom}.jsonl"))
        w(f"| `{dom}` | {n_gold} | {n_sdg} | {verdicts[dom]} |")

    out_path.write_text("\n".join(report))

    # Terse stdout summary.
    print("=" * 72)
    print("v9 Phase 1 — gold audit")
    print("=" * 72)
    for dom in DOMAINS:
        n_gold = len(_load_or_empty(corpora_dir / f"gold_{dom}.jsonl"))
        n_sdg = len(_load_or_empty(corpora_dir / f"sdg_{dom}.jsonl"))
        print(f"  {dom:16}  gold={n_gold:4}  sdg={n_sdg:5}  verdict={verdicts[dom]}")
    print()
    print(f"Full report: {out_path}")


if __name__ == "__main__":
    main()
