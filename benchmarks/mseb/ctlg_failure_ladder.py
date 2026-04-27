"""CTLG failure ladder for MSEB.

This diagnostic separates five questions that the normal shadow metric
collapses together:

1. Adapter-only: do live cue tags synthesize to the expected relation?
2. Gold-cue: can the deterministic grammar synthesize the expected relation?
3. Oracle-subject dispatch: if the TLGQuery is supplied with the gold
   subject, can the dispatcher produce the gold memory?
4. Live shadow: does the full adapter+synthesizer+dispatcher change ranking?
5. Composition oracle: would a confident gold grammar answer move rank-1?

The output is intended for architecture decisions, not leaderboard scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from time import perf_counter
from typing import Any

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import GoldQuery, load_corpus, load_queries
from ncms.application.ctlg import extract_ctlg_cues
from ncms.domain.tlg import Confidence, LGIntent, LGTrace
from ncms.domain.tlg.composition import compose
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import SLMQuerySignals, TLGQuery, synthesize

EXPECTED: dict[str, tuple[str, str] | None] = {
    "current_state": ("state", "current"),
    # Legacy TLG meaning: origin/root of the subject trajectory.
    # Several software-dev templates are worded as causal motivation;
    # the ladder reports those adapter-vs-gold mismatches explicitly.
    "origin": ("ordinal", "first"),
    "ordinal_first": ("ordinal", "first"),
    "ordinal_last": ("ordinal", "last"),
    "sequence": ("temporal", "after_named"),
    "predecessor": ("temporal", "predecessor"),
    "interval": ("temporal", "during_interval"),
    "range": ("temporal", "during_interval"),
    "transitive_cause": ("causal", "chain_cause_of"),
    "causal_chain": ("causal", "chain_cause_of"),
    "concurrent": ("temporal", "concurrent_with"),
    "before_named": ("temporal", "before_named"),
    "retirement": ("state", "retired"),
    "noise": None,
}

logger = logging.getLogger(__name__)


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _log_stage(
    *,
    qid: str,
    stage: str,
    started: float,
    slow_ms: float,
) -> float:
    elapsed = _elapsed_ms(started)
    if elapsed >= slow_ms:
        logger.warning(
            "ctlg_ladder slow_stage qid=%s stage=%s elapsed_ms=%.1f",
            qid,
            stage,
            elapsed,
        )
    else:
        logger.debug(
            "ctlg_ladder stage qid=%s stage=%s elapsed_ms=%.1f",
            qid,
            stage,
            elapsed,
        )
    return elapsed


def _tokenize(labels: Iterable[tuple[str, str]]) -> tuple[TaggedToken, ...]:
    out: list[TaggedToken] = []
    pos = 0
    for surface, label in labels:
        out.append(
            TaggedToken(
                char_start=pos,
                char_end=pos + len(surface),
                surface=surface,
                cue_label=label,
                confidence=0.99,
            )
        )
        pos += len(surface) + 1
    return tuple(out)


def _gold_cues(q: GoldQuery) -> tuple[TaggedToken, ...]:
    subject = q.subject
    referent = q.entity or "decision"
    shape = q.shape
    if shape == "current_state":
        return _tokenize(
            [
                ("current", "B-ASK_CURRENT"),
                ("decision", "B-SCOPE"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "origin":
        return _tokenize(
            [
                ("first", "B-ORDINAL_FIRST"),
                ("decision", "B-SCOPE"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "ordinal_first":
        return _tokenize(
            [
                ("first", "B-ORDINAL_FIRST"),
                ("section", "B-SCOPE"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "ordinal_last":
        return _tokenize(
            [
                ("last", "B-ORDINAL_LAST"),
                ("section", "B-SCOPE"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "sequence":
        return _tokenize(
            [
                ("after", "B-TEMPORAL_AFTER"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "predecessor":
        return _tokenize(
            [
                ("predecessor", "B-TEMPORAL_BEFORE"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "before_named":
        return _tokenize(
            [
                ("before", "B-TEMPORAL_BEFORE"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "concurrent":
        return _tokenize(
            [
                ("during", "B-TEMPORAL_DURING"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape in {"transitive_cause", "causal_chain"}:
        return _tokenize(
            [
                ("led", "B-CAUSAL_ALTLEX"),
                ("to", "I-CAUSAL_ALTLEX"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape == "retirement":
        return _tokenize(
            [
                ("changed", "B-ASK_CHANGE"),
                ("before", "B-TEMPORAL_BEFORE"),
                (referent, "B-REFERENT"),
                (subject, "B-SUBJECT"),
            ]
        )
    if shape in {"interval", "range"}:
        return _tokenize(
            [
                ("during", "B-TEMPORAL_DURING"),
                ("period", "B-TEMPORAL_ANCHOR"),
                (subject, "B-SUBJECT"),
            ]
        )
    return ()


def _oracle_query(q: GoldQuery) -> TLGQuery | None:
    expected = EXPECTED.get(q.shape)
    if expected is None:
        return None
    axis, relation = expected
    return TLGQuery(
        axis=axis,  # type: ignore[arg-type]
        relation=relation,  # type: ignore[arg-type]
        subject=q.subject,
        referent=q.entity or "decision",
        scope="decision" if q.entity is None else None,
        confidence=1.0,
        matched_rule=f"oracle_{q.shape}",
    )


def _matches_expected(q: GoldQuery, tlg_query: TLGQuery | None) -> bool:
    expected = EXPECTED.get(q.shape)
    if expected is None:
        return tlg_query is None
    if tlg_query is None:
        return False
    return (tlg_query.axis, tlg_query.relation) == expected


async def _resolve_answer(svc: Any, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        node = await svc.store.get_memory_node(value)
    except Exception:
        node = None
    return node.memory_id if node is not None else value


def _rank(ids: list[str], gold: set[str]) -> int | None:
    for idx, mid in enumerate(ids, start=1):
        if mid in gold:
            return idx
    return None


async def run_ladder(args: argparse.Namespace) -> dict[str, Any]:
    run_started = perf_counter()
    logger.info("ctlg_ladder load build_dir=%s", args.build_dir)
    corpus = load_corpus(args.build_dir / "corpus.jsonl")
    queries = load_queries(args.build_dir / "queries.jsonl")
    if args.limit:
        queries = queries[: args.limit]
    logger.info(
        "ctlg_ladder loaded corpus=%d queries=%d adapter=%s ctlg=%s/%s",
        len(corpus),
        len(queries),
        args.adapter_domain,
        args.ctlg_adapter_domain,
        args.ctlg_adapter_version or "<default>",
    )

    setup_started = perf_counter()
    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain=args.adapter_domain,
        ctlg_adapter_domain=args.ctlg_adapter_domain,
        ctlg_adapter_version=args.ctlg_adapter_version,
        ncms_config_overrides={
            "entity_extraction_mode": "slm_only",
            "splade_enabled": False,
            "scoring_weight_splade": 0.0,
        },
    )
    await backend.setup()
    logger.info("ctlg_ladder backend_setup elapsed_ms=%.1f", _elapsed_ms(setup_started))
    try:
        ingest_started = perf_counter()
        logger.info("ctlg_ladder ingest_start memories=%d", len(corpus))
        await backend.ingest(corpus)
        logger.info("ctlg_ladder ingest_done elapsed_ms=%.1f", _elapsed_ms(ingest_started))
        svc = backend._svc
        if svc is None:
            raise RuntimeError("NCMS backend did not initialize service")
        cue_tagger = getattr(svc, "_ctlg_cue_tagger", None)
        intent_slot = getattr(svc, "_intent_slot", None)
        ncms_to_mseb = getattr(backend, "_ncms_to_mseb", {})

        rows: list[dict[str, Any]] = []
        counters: dict[str, Counter] = defaultdict(Counter)
        examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for idx, q in enumerate(queries, start=1):
            query_started = perf_counter()
            should_log_query = idx == 1 or idx == len(queries) or idx % args.log_every == 0
            if should_log_query:
                logger.info(
                    "ctlg_ladder query_start index=%d/%d qid=%s shape=%s class=%s",
                    idx,
                    len(queries),
                    q.qid,
                    q.shape,
                    q.query_class,
                )
            gold = {q.gold_mid, *q.gold_alt}
            expected = EXPECTED.get(q.shape)

            stage_started = perf_counter()
            extraction = await extract_ctlg_cues(cue_tagger, q.text, domain=args.adapter_domain)
            extract_ms = _log_stage(
                qid=q.qid,
                stage="extract_ctlg_cues",
                started=stage_started,
                slow_ms=args.slow_ms,
            )

            stage_started = perf_counter()
            ctlg_only_tlg = synthesize(tuple(extraction.tokens))
            slm_signals: SLMQuerySignals | None = None
            if intent_slot is not None:
                try:
                    loop = asyncio.get_running_loop()
                    query_text = q.text
                    slm_label = await loop.run_in_executor(
                        None,
                        lambda text=query_text: intent_slot.extract(
                            text,
                            domain=args.adapter_domain,
                        ),
                    )
                    slm_signals = SLMQuerySignals.from_label(slm_label)
                except Exception:
                    slm_signals = None
            adapter_tlg = synthesize(tuple(extraction.tokens), slm_signals=slm_signals)
            adapter_ms = _log_stage(
                qid=q.qid,
                stage="adapter_synthesize",
                started=stage_started,
                slow_ms=args.slow_ms,
            )
            adapter_match = _matches_expected(q, adapter_tlg)
            ctlg_only_match = _matches_expected(q, ctlg_only_tlg)

            stage_started = perf_counter()
            gold_tlg = synthesize(_gold_cues(q))
            gold_ms = _log_stage(
                qid=q.qid,
                stage="gold_cue_synthesize",
                started=stage_started,
                slow_ms=args.slow_ms,
            )
            gold_match = _matches_expected(q, gold_tlg)

            oracle_tlg = _oracle_query(q)
            oracle_trace = None
            oracle_answer = None
            oracle_mseb = None
            oracle_hit = False
            if oracle_tlg is not None:
                stage_started = perf_counter()
                oracle_trace = await svc.retrieve_lg(q.text, tlg_query=oracle_tlg)
                oracle_dispatch_ms = _log_stage(
                    qid=q.qid,
                    stage="oracle_dispatch",
                    started=stage_started,
                    slow_ms=args.slow_ms,
                )
                stage_started = perf_counter()
                oracle_answer = await _resolve_answer(svc, oracle_trace.grammar_answer)
                oracle_resolve_ms = _log_stage(
                    qid=q.qid,
                    stage="oracle_resolve",
                    started=stage_started,
                    slow_ms=args.slow_ms,
                )
                oracle_mseb = ncms_to_mseb.get(oracle_answer, oracle_answer)
                oracle_hit = oracle_mseb in gold
            else:
                oracle_dispatch_ms = 0.0
                oracle_resolve_ms = 0.0

            stage_started = perf_counter()
            baseline, _stages = await backend.search_with_stages(
                q.text,
                limit=10,
                capture_stages=True,
            )
            baseline_ms = _log_stage(
                qid=q.qid,
                stage="baseline_search",
                started=stage_started,
                slow_ms=args.slow_ms,
            )
            stage_started = perf_counter()
            live = await backend.ctlg_shadow_query(q.text, gold_mids=gold)
            live_ms = _log_stage(
                qid=q.qid,
                stage="live_shadow",
                started=stage_started,
                slow_ms=args.slow_ms,
            )

            oracle_compose_rank = None
            before_rank = _rank([r.mid for r in baseline], gold)
            if baseline and q.gold_mid:
                trace = LGTrace(
                    query=q.text,
                    intent=LGIntent(kind="oracle", subject=q.subject),
                    grammar_answer=q.gold_mid,
                    confidence=Confidence.HIGH,
                )
                composed = compose([r.mid for r in baseline], trace)
                oracle_compose_rank = _rank(list(composed), gold)

            row = {
                "qid": q.qid,
                "shape": q.shape,
                "query_class": q.query_class,
                "expected": expected,
                "adapter_rule": adapter_tlg.matched_rule if adapter_tlg else None,
                "adapter_axis_relation": (
                    [adapter_tlg.axis, adapter_tlg.relation] if adapter_tlg else None
                ),
                "adapter_match": adapter_match,
                "ctlg_only_rule": ctlg_only_tlg.matched_rule if ctlg_only_tlg else None,
                "ctlg_only_axis_relation": (
                    [ctlg_only_tlg.axis, ctlg_only_tlg.relation] if ctlg_only_tlg else None
                ),
                "ctlg_only_match": ctlg_only_match,
                "gold_rule": gold_tlg.matched_rule if gold_tlg else None,
                "gold_axis_relation": [gold_tlg.axis, gold_tlg.relation] if gold_tlg else None,
                "gold_match": gold_match,
                "oracle_confidence": str(oracle_trace.confidence) if oracle_trace else None,
                "oracle_proof": oracle_trace.proof if oracle_trace else "",
                "oracle_answer": oracle_mseb,
                "oracle_hit": oracle_hit,
                "rank_before": before_rank,
                "live_rank_after": live.get("rank_after"),
                "live_changed": bool(live.get("ranking_changed")),
                "live_would_compose": bool(live.get("would_compose")),
                "oracle_compose_rank": oracle_compose_rank,
                "timings_ms": {
                    "extract_ctlg_cues": round(extract_ms, 3),
                    "adapter_synthesize": round(adapter_ms, 3),
                    "gold_cue_synthesize": round(gold_ms, 3),
                    "oracle_dispatch": round(oracle_dispatch_ms, 3),
                    "oracle_resolve": round(oracle_resolve_ms, 3),
                    "baseline_search": round(baseline_ms, 3),
                    "live_shadow": round(live_ms, 3),
                    "total": round(_elapsed_ms(query_started), 3),
                },
            }
            rows.append(row)

            c = counters[q.shape]
            c["n"] += 1
            c["adapter_match"] += int(adapter_match)
            c["ctlg_only_match"] += int(ctlg_only_match)
            c["gold_match"] += int(gold_match)
            c["oracle_confident"] += int(
                str(oracle_trace.confidence) in {"high", "medium"} if oracle_trace else False
            )
            c["oracle_hit"] += int(oracle_hit)
            c["baseline_hit@10"] += int(before_rank is not None)
            c["live_changed"] += int(bool(live.get("ranking_changed")))
            c["live_would_compose"] += int(bool(live.get("would_compose")))
            c["oracle_compose_rank1"] += int(oracle_compose_rank == 1)

            for key in (
                "adapter_match",
                "gold_match",
                "oracle_hit",
                "live_would_compose",
            ):
                if not row[key] and len(examples[key]) < args.max_examples:
                    examples[key].append(row)
            if should_log_query:
                logger.info(
                    "ctlg_ladder query_done index=%d/%d qid=%s elapsed_ms=%.1f "
                    "adapter_match=%s gold_match=%s oracle_hit=%s "
                    "live_would_compose=%s live_changed=%s rank_before=%s rank_after=%s",
                    idx,
                    len(queries),
                    q.qid,
                    _elapsed_ms(query_started),
                    adapter_match,
                    gold_match,
                    oracle_hit,
                    bool(live.get("would_compose")),
                    bool(live.get("ranking_changed")),
                    before_rank,
                    live.get("rank_after"),
                )

        summary = {
            shape: dict(counts)
            for shape, counts in sorted(counters.items())
        }
        logger.info(
            "ctlg_ladder done queries=%d elapsed_ms=%.1f out=%s",
            len(queries),
            _elapsed_ms(run_started),
            args.out,
        )
        return {"summary": summary, "examples": examples, "rows": rows}
    finally:
        shutdown_started = perf_counter()
        await backend.shutdown()
        logger.info("ctlg_ladder backend_shutdown elapsed_ms=%.1f", _elapsed_ms(shutdown_started))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CTLG failure ladder on an MSEB build")
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--adapter-domain", required=True)
    parser.add_argument("--ctlg-adapter-domain", required=True)
    parser.add_argument("--ctlg-adapter-version", default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-examples", type=int, default=8)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Console log level for progress and per-stage timings.",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=1,
        help="Emit query_start/query_done progress every N queries.",
    )
    parser.add_argument(
        "--slow-ms",
        type=float,
        default=1000.0,
        help="Warn when an individual ladder stage exceeds this many milliseconds.",
    )
    parser.add_argument(
        "--verbose-deps",
        action="store_true",
        help="Keep high-volume NCMS ingest/index/search logs at the selected log level.",
    )
    parser.add_argument(
        "--force-exit",
        action="store_true",
        help=(
            "Force process exit after writing JSON. Useful when torch/tokenizer "
            "runtime threads keep a benchmark process alive after cleanup."
        ),
    )
    args = parser.parse_args()
    args.log_every = max(1, args.log_every)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose_deps:
        for noisy in (
            "ncms.application.ingestion",
            "ncms.application.index_worker",
            "ncms.application.retrieval.pipeline",
            "ncms.application.diagnostics.search_diag",
        ):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    result = asyncio.run(run_ladder(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    logger.info("ctlg_ladder write_done out=%s bytes=%d", args.out, args.out.stat().st_size)
    print(json.dumps({"out": str(args.out), "summary": result["summary"]}, indent=2))
    sys.stdout.flush()
    sys.stderr.flush()
    if args.force_exit:
        os._exit(0)


if __name__ == "__main__":
    main()
