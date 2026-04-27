"""Evaluate CTLG adapter query composition without indexing/search.

This is the fast rung before full shadow mode: it asks whether a cue tagger's
output composes to the MSEB query shape expected by the benchmark.  It does not
measure recall; it isolates adapter + deterministic grammar coverage.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmarks.mseb.ctlg_failure_ladder import EXPECTED
from benchmarks.mseb.schema import GoldQuery, load_queries
from ncms.application.adapters.ctlg import LoraCTLGCueTagger
from ncms.application.intent_slot_chain import build_default_intent_slot_chain
from ncms.domain.tlg.semantic_parser import SLMQuerySignals, TLGQuery, synthesize


def _matches_expected(query: GoldQuery, tlg_query: TLGQuery | None) -> bool:
    expected = EXPECTED.get(query.shape)
    if expected is None:
        return tlg_query is None
    if tlg_query is None:
        return False
    return (tlg_query.axis, tlg_query.relation) == expected


def evaluate_adapter(
    *,
    build_dir: Path,
    adapter_dir: Path,
    domain: str,
    limit: int = 0,
    max_examples: int = 8,
    slm_domain: str | None = None,
    slm_version: str | None = None,
    slm_root: Path | None = None,
    slm_confidence_threshold: float = 0.3,
) -> dict[str, Any]:
    """Evaluate one CTLG adapter against ``build_dir/queries.jsonl``."""
    queries = load_queries(build_dir / "queries.jsonl")
    if limit:
        queries = queries[:limit]
    tagger = LoraCTLGCueTagger(adapter_dir)
    slm = (
        build_default_intent_slot_chain(
            domain=slm_domain,
            version=slm_version,
            root=slm_root,
            confidence_threshold=slm_confidence_threshold,
            include_e5_fallback=False,
        )
        if slm_domain is not None
        else None
    )
    totals: Counter[str] = Counter()
    by_shape: dict[str, Counter[str]] = defaultdict(Counter)
    examples: list[dict[str, Any]] = []

    for query in queries:
        expected = EXPECTED.get(query.shape)
        tags = tuple(tagger.extract_cues(query.text, domain=domain))
        slm_signals = (
            SLMQuerySignals.from_label(slm.extract(query.text, domain=domain))
            if slm is not None
            else None
        )
        tlg_query = synthesize(tags, slm_signals=slm_signals)
        matched = _matches_expected(query, tlg_query)
        totals["n"] += 1
        totals["synthesized"] += int(tlg_query is not None)
        totals["matched"] += int(matched)

        shape_counts = by_shape[query.shape]
        shape_counts["n"] += 1
        shape_counts["synthesized"] += int(tlg_query is not None)
        shape_counts["matched"] += int(matched)

        if not matched and len(examples) < max_examples:
            examples.append(
                {
                    "qid": query.qid,
                    "shape": query.shape,
                    "text": query.text,
                    "expected": list(expected) if expected is not None else None,
                    "actual": (
                        {
                            "axis": tlg_query.axis,
                            "relation": tlg_query.relation,
                            "matched_rule": tlg_query.matched_rule,
                            "referent": tlg_query.referent,
                            "subject": tlg_query.subject,
                            "scope": tlg_query.scope,
                        }
                        if tlg_query is not None
                        else None
                    ),
                    "cue_tags": [
                        {
                            "surface": tag.surface,
                            "cue_label": tag.cue_label,
                            "confidence": tag.confidence,
                        }
                        for tag in tags
                        if tag.cue_label != "O"
                    ],
                    "slm": _slm_to_json(slm_signals),
                }
            )

    return {
        "build_dir": str(build_dir),
        "adapter_dir": str(adapter_dir),
        "domain": domain,
        "slm_domain": slm_domain,
        "slm_version": slm_version,
        "summary": dict(totals),
        "match_rate": totals["matched"] / totals["n"] if totals["n"] else 1.0,
        "synthesis_rate": totals["synthesized"] / totals["n"] if totals["n"] else 1.0,
        "by_shape": {shape: dict(counts) for shape, counts in sorted(by_shape.items())},
        "examples": examples,
    }


def _slm_to_json(signals: SLMQuerySignals | None) -> dict[str, Any] | None:
    if signals is None:
        return None
    return {
        "intent": signals.intent,
        "topic": signals.topic,
        "state_change": signals.state_change,
        "slots": dict(signals.slots),
        "role_spans": list(signals.role_spans),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate CTLG adapter query composition")
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument(
        "--slm-domain",
        default=None,
        help="Optional 5-head SLM domain to supply grounding signals to the grammar.",
    )
    parser.add_argument("--slm-version", default=None)
    parser.add_argument("--slm-root", type=Path, default=None)
    parser.add_argument("--slm-confidence-threshold", type=float, default=0.3)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-examples", type=int, default=8)
    args = parser.parse_args()

    result = evaluate_adapter(
        build_dir=args.build_dir,
        adapter_dir=args.adapter_dir,
        domain=args.domain,
        limit=args.limit,
        max_examples=args.max_examples,
        slm_domain=args.slm_domain,
        slm_version=args.slm_version,
        slm_root=args.slm_root,
        slm_confidence_threshold=args.slm_confidence_threshold,
    )
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload)
    print(payload)


if __name__ == "__main__":
    main()
