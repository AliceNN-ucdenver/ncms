"""MSEB metrics — pure scoring over predictions × gold.

Inputs are (ranked ``list[mid]``, ``GoldQuery``) pairs; outputs
are the per-shape / per-preference / per-head tables that drive
``results.json`` in `harness.py` and the markdown summary emitted
for pre-paper insertion.

Pure, no NCMS / memory-service dependency — benchmarks pass in
the final ranked-mid list; this module scores it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from benchmarks.mseb.schema import (
    INTENT_SHAPES,
    PREFERENCE_KINDS,
    QUERY_CLASSES,
    GoldQuery,
    IntentShape,
    PreferenceKind,
    QueryClass,
)

# ---------------------------------------------------------------------------
# Per-query prediction record
# ---------------------------------------------------------------------------


@dataclass
class Prediction:
    """One prediction the harness produces per gold query.

    ``ranked_mids`` is the ordered list the retrieval pipeline
    returned (top-k already applied upstream; we only look at the
    first 5 for our metrics).  ``latency_ms`` is wall-clock, used
    to compute per-run p50/p95.  ``head_outputs`` carries per-head
    classifier outputs for the gold *memory* of the query (when
    the harness is running with the SLM on and the author has
    annotated gold head labels); used for the per-head F1 table.
    """

    qid: str
    ranked_mids: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    # Optional SLM diagnostic fields.
    head_outputs: dict[str, str] = field(default_factory=dict)
    """Per-head classifier output on the gold memory; only set when
    ``--slm-on`` and the gold query carries expected head labels."""

    intent_confidence: float | None = None
    """The SLM's self-reported confidence on the top label for this
    query; used to compute the `confidently_wrong` rate."""


# ---------------------------------------------------------------------------
# Core scoring primitives
# ---------------------------------------------------------------------------


def _accept_set(q: GoldQuery) -> set[str]:
    return {q.gold_mid, *q.gold_alt} - {""}


def rank1_hit(pred: Prediction, query: GoldQuery) -> int:
    """1 if the top-ranked mid is in the accept set, else 0."""
    if not pred.ranked_mids:
        return 0
    return int(pred.ranked_mids[0] in _accept_set(query))


def top5_hit(pred: Prediction, query: GoldQuery) -> int:
    """1 if any of the top-5 mids is in the accept set, else 0."""
    return int(bool(set(pred.ranked_mids[:5]) & _accept_set(query)))


def rr(pred: Prediction, query: GoldQuery) -> float:
    """Reciprocal rank of the first correct result (0.0 if none in top-10)."""
    accept = _accept_set(query)
    for i, mid in enumerate(pred.ranked_mids[:10]):
        if mid in accept:
            return 1.0 / (i + 1)
    return 0.0


# ---------------------------------------------------------------------------
# Wilson score interval — proper CI for small-n per-shape cells.
# ---------------------------------------------------------------------------


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion.

    More honest than normal-approx when n is small (which our
    per-shape cells are — ~20-25 queries per cell).
    """
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# ---------------------------------------------------------------------------
# Multi-class F1 (used for per-head classifier reports)
# ---------------------------------------------------------------------------


def macro_f1(pairs: list[tuple[str, str]]) -> dict[str, float]:
    """Macro-F1 across observed classes.

    ``pairs`` is a list of ``(gold_label, pred_label)`` strings.
    Returns ``{"macro_f1": float, "per_class": {label: f1}}``.
    """
    labels = sorted({g for g, _ in pairs} | {p for _, p in pairs})
    per_class: dict[str, float] = {}
    for label in labels:
        tp = sum(1 for g, p in pairs if g == label and p == label)
        fp = sum(1 for g, p in pairs if g != label and p == label)
        fn = sum(1 for g, p in pairs if g == label and p != label)
        if tp + fp == 0 or tp + fn == 0 or tp == 0:
            per_class[label] = 0.0
            continue
        prec = tp / (tp + fp)
        rec = tp / (tp + fn)
        per_class[label] = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    macro = sum(per_class.values()) / len(per_class) if per_class else 0.0
    return {"macro_f1": macro, "per_class": per_class}


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


def aggregate(
    predictions: list[Prediction],
    queries: list[GoldQuery],
    *,
    intent_confidence_threshold: float = 0.7,
    head_gold: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compute the full MSEB result dict.

    ``head_gold`` (optional) is ``{qid: {head_name: gold_label}}``
    for the per-head F1 table.  When absent, the ``per_head``
    section is skipped.
    """
    # Index predictions by qid for O(1) lookup.
    pred_by_qid = {p.qid: p for p in predictions}

    # ---- per-shape ----
    shape_hits_r1: dict[IntentShape, int] = defaultdict(int)
    shape_hits_r5: dict[IntentShape, int] = defaultdict(int)
    shape_sum_rr: dict[IntentShape, float] = defaultdict(float)
    shape_n: dict[IntentShape, int] = defaultdict(int)

    # ---- per-preference ----
    pref_hits_r1: dict[PreferenceKind, int] = defaultdict(int)
    pref_hits_r5: dict[PreferenceKind, int] = defaultdict(int)
    pref_n: dict[PreferenceKind, int] = defaultdict(int)

    # ---- per-class (general / temporal / preference / noise) ----
    class_hits_r1: dict[QueryClass, int] = defaultdict(int)
    class_hits_r5: dict[QueryClass, int] = defaultdict(int)
    class_sum_rr: dict[QueryClass, float] = defaultdict(float)
    class_n: dict[QueryClass, int] = defaultdict(int)

    # ---- latency / confidently-wrong ----
    latencies: list[float] = []
    confidently_wrong = 0
    confidently_wrong_denom = 0

    for q in queries:
        pred = pred_by_qid.get(q.qid)
        if pred is None:
            # Missing prediction — count as miss so a crashed query
            # doesn't silently improve the headline.
            shape_n[q.shape] += 1
            pref_n[q.preference] += 1
            continue

        r1 = rank1_hit(pred, q)
        r5 = top5_hit(pred, q)
        rr_val = rr(pred, q)

        shape_hits_r1[q.shape] += r1
        shape_hits_r5[q.shape] += r5
        shape_sum_rr[q.shape] += rr_val
        shape_n[q.shape] += 1

        pref_hits_r1[q.preference] += r1
        pref_hits_r5[q.preference] += r5
        pref_n[q.preference] += 1

        qc = getattr(q, "query_class", "general") or "general"
        class_hits_r1[qc] += r1
        class_hits_r5[qc] += r5
        class_sum_rr[qc] += rr_val
        class_n[qc] += 1

        if pred.latency_ms > 0:
            latencies.append(pred.latency_ms)

        if (
            pred.intent_confidence is not None
            and pred.intent_confidence >= intent_confidence_threshold
        ):
            confidently_wrong_denom += 1
            if r1 == 0:
                confidently_wrong += 1

    per_shape: dict[str, dict[str, float | int | tuple[float, float]]] = {}
    for shape in INTENT_SHAPES:
        n = shape_n[shape]
        per_shape[shape] = {
            "n": n,
            "r@1": (shape_hits_r1[shape] / n) if n else 0.0,
            "r@5": (shape_hits_r5[shape] / n) if n else 0.0,
            "mrr": (shape_sum_rr[shape] / n) if n else 0.0,
            "r@1_ci95": wilson_ci(shape_hits_r1[shape], n),
        }

    per_preference: dict[str, dict[str, float | int | tuple[float, float]]] = {}
    for pref in PREFERENCE_KINDS:
        n = pref_n[pref]
        per_preference[pref] = {
            "n": n,
            "r@1": (pref_hits_r1[pref] / n) if n else 0.0,
            "r@5": (pref_hits_r5[pref] / n) if n else 0.0,
            "r@1_ci95": wilson_ci(pref_hits_r1[pref], n),
        }

    per_class: dict[str, dict[str, float | int | tuple[float, float]]] = {}
    for cls in QUERY_CLASSES:
        n = class_n[cls]
        per_class[cls] = {
            "n": n,
            "r@1": (class_hits_r1[cls] / n) if n else 0.0,
            "r@5": (class_hits_r5[cls] / n) if n else 0.0,
            "mrr": (class_sum_rr[cls] / n) if n else 0.0,
            "r@1_ci95": wilson_ci(class_hits_r1[cls], n),
        }

    # ---- overall aggregates ----
    total_n = sum(shape_n.values())
    total_r1 = sum(shape_hits_r1.values())
    total_r5 = sum(shape_hits_r5.values())
    total_rr = sum(shape_sum_rr.values())

    # ---- per-head (optional) ----
    per_head: dict[str, dict[str, Any]] = {}
    if head_gold:
        heads = {"admission", "state_change", "topic", "intent", "slot"}
        head_pairs: dict[str, list[tuple[str, str]]] = {h: [] for h in heads}
        for qid, gold_heads in head_gold.items():
            pred = pred_by_qid.get(qid)
            if pred is None:
                continue
            for head_name, gold_label in gold_heads.items():
                if head_name not in heads:
                    continue
                pred_label = pred.head_outputs.get(head_name, "none")
                head_pairs[head_name].append((gold_label, pred_label))
        for head_name, pairs in head_pairs.items():
            if not pairs:
                continue
            per_head[head_name] = {"n": len(pairs), **macro_f1(pairs)}

    def _pct(x: list[float], q: float) -> float:
        if not x:
            return 0.0
        s = sorted(x)
        k = max(0, min(len(s) - 1, int(q * (len(s) - 1))))
        return s[k]

    return {
        "total_queries": total_n,
        "overall": {
            "r@1": (total_r1 / total_n) if total_n else 0.0,
            "r@5": (total_r5 / total_n) if total_n else 0.0,
            "mrr": (total_rr / total_n) if total_n else 0.0,
        },
        "per_shape": per_shape,
        "per_preference": per_preference,
        "per_class": per_class,
        "per_head": per_head,
        "latency_p50_ms": _pct(latencies, 0.50),
        "latency_p95_ms": _pct(latencies, 0.95),
        "confidently_wrong": (
            confidently_wrong / confidently_wrong_denom
            if confidently_wrong_denom else 0.0
        ),
        "confidently_wrong_denominator": confidently_wrong_denom,
    }


# ---------------------------------------------------------------------------
# Markdown summary — ready to paste into the pre-paper
# ---------------------------------------------------------------------------


def markdown_summary(result: dict[str, Any], *, run_id: str = "") -> str:
    """Render the full result as a markdown table suitable for docs."""
    lines: list[str] = []
    if run_id:
        lines.append(f"# MSEB run: `{run_id}`\n")
    o = result["overall"]
    lines.append(
        f"**Overall.** queries={result['total_queries']}  "
        f"rank-1={o['r@1']:.3f}  top-5={o['r@5']:.3f}  "
        f"MRR={o['mrr']:.3f}  "
        f"latency p50={result['latency_p50_ms']:.0f}ms / "
        f"p95={result['latency_p95_ms']:.0f}ms  "
        f"confidently-wrong={result['confidently_wrong']:.3f}\n",
    )

    lines.append("## Per-shape")
    lines.append("| shape | n | r@1 | r@5 | MRR | r@1 95% CI |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for shape, cell in result["per_shape"].items():
        if not cell["n"]:
            continue
        lo, hi = cell["r@1_ci95"]
        lines.append(
            f"| `{shape}` | {cell['n']} | "
            f"{cell['r@1']:.3f} | {cell['r@5']:.3f} | {cell['mrr']:.3f} | "
            f"[{lo:.2f}, {hi:.2f}] |",
        )

    if any(cell["n"] for cell in result.get("per_class", {}).values()):
        lines.append("\n## Per-class")
        lines.append("| query_class | n | r@1 | r@5 | MRR | r@1 95% CI |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for cls, cell in result["per_class"].items():
            if not cell["n"]:
                continue
            lo, hi = cell["r@1_ci95"]
            lines.append(
                f"| `{cls}` | {cell['n']} | "
                f"{cell['r@1']:.3f} | {cell['r@5']:.3f} | "
                f"{cell['mrr']:.3f} | [{lo:.2f}, {hi:.2f}] |",
            )

    if any(cell["n"] for cell in result["per_preference"].values()):
        lines.append("\n## Per-preference")
        lines.append("| preference | n | r@1 | r@5 | r@1 95% CI |")
        lines.append("|---|---:|---:|---:|---|")
        for pref, cell in result["per_preference"].items():
            if not cell["n"]:
                continue
            lo, hi = cell["r@1_ci95"]
            lines.append(
                f"| `{pref}` | {cell['n']} | "
                f"{cell['r@1']:.3f} | {cell['r@5']:.3f} | "
                f"[{lo:.2f}, {hi:.2f}] |",
            )

    if result["per_head"]:
        lines.append("\n## Per-head (classifier F1)")
        lines.append("| head | n | macro F1 |")
        lines.append("|---|---:|---:|")
        for head, cell in result["per_head"].items():
            lines.append(f"| `{head}` | {cell['n']} | {cell['macro_f1']:.3f} |")

    return "\n".join(lines) + "\n"


__all__ = [
    "Prediction",
    "aggregate",
    "macro_f1",
    "markdown_summary",
    "rank1_hit",
    "rr",
    "top5_hit",
    "wilson_ci",
]
