"""MSEB harness — run a domain's corpus + queries through NCMS.

Spins up a fresh in-memory NCMS per run, ingests the corpus
(ordered by ``observed_at`` so temporal scoring sees the right
history), runs each gold query through ``MemoryService.search``,
grades with ``benchmarks/mseb/metrics.py``, writes
``results.json`` + markdown summary.

Ablation flags (see ``docs/p3-state-evolution-benchmark.md``
§4.1.1) translate to ``NCMSConfig`` weight / feature overrides.

Usage::

    # Default run (all TLG mechanisms on)
    uv run python -m benchmarks.mseb.harness \\
        --domain mseb_swe \\
        --build-dir benchmarks/mseb_swe/build \\
        --adapter-domain software_dev

    # TLG fully off — baseline
    uv run python -m benchmarks.mseb.harness --domain mseb_swe \\
        --build-dir benchmarks/mseb_swe/build --tlg-off

    # Single-flag ablation
    uv run python -m benchmarks.mseb.harness --domain mseb_swe \\
        --build-dir benchmarks/mseb_swe/build --no-retirement
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Load HF_TOKEN etc. before any ncms/sentence-transformers import
# (SPLADE v3 is gated on HuggingFace and falls back to an
# anonymous fetch otherwise, which 401s).
try:
    from benchmarks.env import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:  # pragma: no cover
    pass

from benchmarks.mseb.backends import BACKENDS, make_backend
from benchmarks.mseb.metrics import (
    Prediction,
    aggregate,
    markdown_summary,
)
from benchmarks.mseb.schema import (
    GoldQuery,
    load_corpus,
    load_queries,
)

logger = logging.getLogger("mseb.harness")

DEFAULT_RESULTS_ROOT = Path("benchmarks/results/mseb")


# ---------------------------------------------------------------------------
# Ablation feature set — maps CLI flags to NCMSConfig overrides
# ---------------------------------------------------------------------------


@dataclass
class FeatureSet:
    """Boolean flags for every ablation axis.  Both default True
    (full temporal stack + SLM).  ``--temporal-off`` flips
    ``temporal`` to False; ``--slm-off`` flips ``slm``.

    Sub-phase ablations (reconciliation alone, episodes alone,
    intent-classifier alone, etc.) are no longer user-facing — the
    ``NCMSConfig`` flag scheme collapsed them into ``temporal_enabled``.
    Researchers who want a weight-sweep at individual phases can pass
    ``--ncms-config`` scoring-weight overrides directly.
    """

    temporal: bool = True
    slm: bool = True

    # Single-head isolation (evaluates classifier for one head only).
    head: str | None = None  # admission|state_change|topic|intent|slot

    def to_dict(self) -> dict[str, object]:
        return {
            "temporal": self.temporal,
            "slm": self.slm,
            "head": self.head,
        }

    def to_ncms_config_overrides(self) -> dict[str, object]:
        """Translate feature flags into ``NCMSConfig(**overrides)``.

        - ``temporal=False`` → ``temporal_enabled=False`` +
          ``scoring_weight_temporal=0.0`` + ``scoring_weight_hierarchy=0.0``
          + ``scoring_weight_recency=0.0`` (zero the temporal-layer
          scoring weights so the mechanism is fully off, not merely
          a 0 × nonzero multiplication)

        ``slm=False`` is NOT a config override -- the SLM kill-switch
        is now ``intent_slot=None`` at MemoryService construction
        time.  The backend handles that translation; this method
        only emits genuine config-field overrides.
        """
        ov: dict[str, object] = {}
        if not self.temporal:
            ov.update(
                {
                    "temporal_enabled": False,
                    "scoring_weight_temporal": 0.0,
                    "scoring_weight_hierarchy": 0.0,
                    "scoring_weight_recency": 0.0,
                }
            )
        return ov


# ---------------------------------------------------------------------------
# CLI-flag parsing
# ---------------------------------------------------------------------------


def _parse_feature_set(args: argparse.Namespace) -> FeatureSet:
    fs = FeatureSet()
    if getattr(args, "temporal_off", False):
        fs.temporal = False
    if getattr(args, "slm_off", False):
        fs.slm = False
    fs.head = getattr(args, "head", None)
    return fs


# ---------------------------------------------------------------------------
# Query execution — backend-agnostic
# ---------------------------------------------------------------------------


async def _run_queries(
    backend,
    queries: list[GoldQuery],
    *,
    top_k: int = 10,
) -> list[Prediction]:
    """Run each gold query through the backend's search and capture
    per-head SLM classification for forensic analysis.

    ``Prediction.head_outputs`` is populated from the backend's
    ``classify_query()`` method (a no-op for non-NCMS backends) so
    the dumped predictions.jsonl carries, per query, BOTH the
    search ranking AND the SLM's 6-head classification.  That's
    what downstream forensic tooling needs to trace WHY a query
    routed the way it did.
    """
    preds: list[Prediction] = []
    classify = getattr(backend, "classify_query", None)
    ctlg_shadow = getattr(backend, "ctlg_shadow_query", None)
    # Per-stage gold-recall capture is opt-in: only NCMS backend
    # exposes ``search_with_stages``.  mem0 baseline doesn't have
    # per-stage candidates, so we fall back to plain ``search``.
    search_with_stages = getattr(backend, "search_with_stages", None)
    stage_recall_top_k = 50

    def _gold_set(q: GoldQuery) -> set[str]:
        return {q.gold_mid} | set(q.gold_alt or [])

    for q in queries:
        gold = _gold_set(q)
        t0 = time.perf_counter()
        stages: dict[str, list[str]] = {}
        try:
            if search_with_stages is not None:
                rankings, stages = await search_with_stages(
                    query=q.text,
                    limit=top_k,
                    capture_stages=True,
                )
            else:
                rankings = await backend.search(
                    query=q.text,
                    limit=top_k,
                )
        except Exception as exc:  # pragma: no cover — surface in log
            logger.warning("qid=%s search failed: %s", q.qid, exc)
            rankings = []
        latency_ms = (time.perf_counter() - t0) * 1000.0
        head_outputs: dict[str, object] = {}
        if classify is not None:
            try:
                head_outputs = classify(q.text) or {}
            except Exception as exc:  # pragma: no cover — forensic path
                logger.debug(
                    "qid=%s classify_query failed: %s",
                    q.qid,
                    exc,
                )
        if ctlg_shadow is not None:
            try:
                ctlg_diag = await ctlg_shadow(
                    q.text,
                    gold_mids=gold,
                    gold_subject=q.subject,
                )
                if ctlg_diag:
                    head_outputs["ctlg_shadow"] = ctlg_diag
            except Exception as exc:  # pragma: no cover — forensic path
                logger.debug(
                    "qid=%s ctlg_shadow_query failed: %s",
                    q.qid,
                    exc,
                )
        intent_conf = head_outputs.get("intent_conf")

        # Per-stage gold-recall flags.  For each stage, check
        # whether ANY gold mid (gold_mid + gold_alt) appears in
        # the first ``stage_recall_top_k`` candidates.  Stays
        # ``None`` when the backend didn't expose per-stage
        # candidates.
        def _gold_in_stage(
            stage_key: str,
            *,
            _stages: dict[str, list[str]] = stages,
            _gold: set[str] = gold,
            _k: int = stage_recall_top_k,
        ) -> bool | None:
            ids = _stages.get(stage_key)
            if ids is None:
                return None
            return bool(set(ids[:_k]) & _gold)

        preds.append(
            Prediction(
                qid=q.qid,
                ranked_mids=[r.mid for r in rankings],
                latency_ms=latency_ms,
                head_outputs=head_outputs,
                intent_confidence=(
                    float(intent_conf) if isinstance(intent_conf, (int, float)) else None
                ),
                gold_in_bm25=_gold_in_stage("bm25"),
                gold_in_splade=_gold_in_stage("splade"),
                gold_in_rrf_fused=_gold_in_stage("rrf_fused"),
                gold_in_expanded=_gold_in_stage("expanded"),
                gold_in_scored=_gold_in_stage("scored"),
                stage_recall_top_k=stage_recall_top_k,
            )
        )
    return preds


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    domain: str
    build_dir: Path
    backend: str
    adapter_domain: str | None
    feature_set: FeatureSet
    out_dir: Path
    run_id: str
    top_k: int = 10
    ctlg_adapter_domain: str | None = None
    ctlg_adapter_version: str | None = None
    head_gold: dict[str, dict[str, str]] | None = field(default=None)
    backend_kwargs: dict[str, object] = field(default_factory=dict)


async def run(cfg: RunConfig) -> dict[str, object]:
    """End-to-end MSEB run → results dict (backend-agnostic)."""
    corpus = load_corpus(cfg.build_dir / "corpus.jsonl")
    queries = load_queries(cfg.build_dir / "queries.jsonl")

    logger.info(
        "=" * 72,
    )
    logger.info(
        "MSEB RUN START  domain=%s backend=%s adapter=%s",
        cfg.domain,
        cfg.backend,
        cfg.adapter_domain,
    )
    logger.info(
        "MSEB RUN ctlg_adapter=%s version=%s",
        cfg.ctlg_adapter_domain,
        cfg.ctlg_adapter_version,
    )
    logger.info(
        "MSEB RUN corpus=%d memories  queries=%d  top_k=%d",
        len(corpus),
        len(queries),
        cfg.top_k,
    )
    logger.info(
        "MSEB RUN feature_set=%s",
        cfg.feature_set.to_dict(),
    )
    logger.info(
        "MSEB RUN backend_kwargs=%s run_id=%s",
        cfg.backend_kwargs,
        cfg.run_id,
    )
    logger.info(
        "=" * 72,
    )

    # Construct the selected backend.  NCMS honours the feature set;
    # other backends ignore flags they don't understand but still
    # record them in results.json so runs stay comparable.
    backend = make_backend(
        cfg.backend,
        feature_set=cfg.feature_set,
        adapter_domain=cfg.adapter_domain,
        ctlg_adapter_domain=cfg.ctlg_adapter_domain,
        ctlg_adapter_version=cfg.ctlg_adapter_version,
        **cfg.backend_kwargs,
    )
    await backend.setup()
    try:
        t_ingest = time.perf_counter()
        await backend.ingest(corpus)
        ingest_secs = time.perf_counter() - t_ingest
        logger.info("ingested %d memories in %.1fs", len(corpus), ingest_secs)

        t_query = time.perf_counter()
        preds = await _run_queries(backend, queries, top_k=cfg.top_k)
        query_secs = time.perf_counter() - t_query
        logger.info("ran %d queries in %.1fs", len(preds), query_secs)
    finally:
        await backend.shutdown()

    result = aggregate(preds, queries, head_gold=cfg.head_gold)
    result["run_id"] = cfg.run_id
    result["domain"] = cfg.domain
    result["backend"] = cfg.backend
    result["feature_set"] = cfg.feature_set.to_dict()
    result["ingest_seconds"] = ingest_secs
    result["query_seconds"] = query_secs

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / f"{cfg.run_id}.results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str),
    )
    (cfg.out_dir / f"{cfg.run_id}.summary.md").write_text(
        markdown_summary(result, run_id=cfg.run_id),
    )
    # Dump per-query predictions alongside results.  Enables post-hoc
    # re-scoring against new class / shape / preference taxonomies
    # without re-running the whole pipeline (which costs MPS / Spark
    # time).  One JSON line per prediction.
    preds_path = cfg.out_dir / f"{cfg.run_id}.predictions.jsonl"
    with preds_path.open("w", encoding="utf-8") as fh:
        for p in preds:
            fh.write(
                json.dumps(
                    {
                        "qid": p.qid,
                        "ranked_mids": p.ranked_mids,
                        "latency_ms": p.latency_ms,
                        "intent_confidence": p.intent_confidence,
                        "head_outputs": p.head_outputs,
                        # Per-stage gold-recall (None when backend doesn't
                        # expose per-stage candidates -- e.g. mem0 baseline).
                        "gold_in_bm25": p.gold_in_bm25,
                        "gold_in_splade": p.gold_in_splade,
                        "gold_in_rrf_fused": p.gold_in_rrf_fused,
                        "gold_in_expanded": p.gold_in_expanded,
                        "gold_in_scored": p.gold_in_scored,
                        "stage_recall_top_k": p.stage_recall_top_k,
                    },
                    ensure_ascii=False,
                )
            )
            fh.write("\n")

    # Aggregate per-stage gold recall into the results.json + summary.
    # Lets ``Δ recall`` between stages tell us where the gold drops
    # out of the funnel — the canonical "where did the gold disappear?"
    # diagnostic.  Skipped silently for backends that don't expose
    # per-stage candidates (bool flags stay None).
    def _stage_recall(attr: str) -> dict[str, object] | None:
        vals = [getattr(p, attr) for p in preds]
        present = [v for v in vals if v is not None]
        if not present:
            return None
        return {
            "n": len(present),
            "recall": round(sum(present) / len(present), 4),
        }

    stage_recall_block = {
        f"gold_in_bm25@{preds[0].stage_recall_top_k if preds else 50}": _stage_recall(
            "gold_in_bm25"
        ),
        f"gold_in_splade@{preds[0].stage_recall_top_k if preds else 50}": _stage_recall(
            "gold_in_splade"
        ),
        f"gold_in_rrf_fused@{preds[0].stage_recall_top_k if preds else 50}": _stage_recall(
            "gold_in_rrf_fused"
        ),
        f"gold_in_expanded@{preds[0].stage_recall_top_k if preds else 50}": _stage_recall(
            "gold_in_expanded"
        ),
        f"gold_in_scored@{preds[0].stage_recall_top_k if preds else 50}": _stage_recall(
            "gold_in_scored"
        ),
    }
    if any(v is not None for v in stage_recall_block.values()):
        # Re-write results.json with the stage-recall block injected
        # under ``stage_recall``.  Keeps the existing summary.md
        # unchanged for backwards compat; consumers that want
        # per-stage recall read results.json directly.
        result["stage_recall"] = stage_recall_block
        (cfg.out_dir / f"{cfg.run_id}.results.json").write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str),
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _make_run_id(domain: str, backend: str, feature_set: FeatureSet) -> str:
    """Build a run_id that self-describes the active feature set.

    Format: ``<domain>_<backend>[_temporal-on|temporal-off][_slm-off][_head-X]_<ts>``
    The temporal/slm tags are always present for ncms runs so the
    file name cannot lie about what was measured.
    """
    parts = [domain, backend]
    if backend == "ncms":
        parts.append("temporal-on" if feature_set.temporal else "temporal-off")
        if not feature_set.slm:
            parts.append("slm-off")
        if feature_set.head:
            parts.append(f"head-{feature_set.head}")
    parts.append(datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ"))
    return "_".join(parts)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="MSEB harness: run one domain")
    ap.add_argument(
        "--domain",
        required=True,
        help="Domain identifier, e.g. mseb_swe / mseb_clinical / mseb_convo",
    )
    ap.add_argument(
        "--build-dir",
        type=Path,
        required=True,
        help="Directory containing corpus.jsonl + queries.jsonl",
    )
    ap.add_argument(
        "--adapter-domain",
        default=None,
        help="LoRA adapter domain (software_dev / clinical / conversational).  "
        "Required when SLM is on (NCMS backend only).",
    )
    ap.add_argument(
        "--ctlg-adapter-domain",
        default=None,
        help="Dedicated CTLG cue-tagger adapter domain. Enables CTLG shadow diagnostics "
        "without changing scored rankings.",
    )
    ap.add_argument(
        "--ctlg-adapter-version",
        default=None,
        help="Optional CTLG cue-tagger version, e.g. ctlg-v1. Defaults to newest deployed.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write results (default: benchmarks/results/mseb/<domain>/)",
    )
    ap.add_argument("--top-k", type=int, default=10)

    # Backend selection — ``ncms`` is the reference; ``mem0`` is the
    # competitor.  Additional backends register themselves in
    # ``benchmarks/mseb/backends/__init__.py``.
    ap.add_argument(
        "--backend",
        default="ncms",
        choices=sorted(BACKENDS),
        help="Which memory backend to evaluate (default: ncms)",
    )
    # mem0-specific knobs — ignored by other backends.
    ap.add_argument(
        "--mem0-infer",
        action="store_true",
        help="mem0 only: enable LLM fact extraction on add() "
        "(default: off, stores content verbatim)",
    )
    ap.add_argument(
        "--mem0-rerank",
        action="store_true",
        help="mem0 only: enable mem0's LLM reranker on search (default: off)",
    )
    # NCMS scoring-weight overrides (for ablation sweeps without
    # editing the backend each time).
    ap.add_argument(
        "--temporal-weight",
        type=float,
        default=None,
        help="Override scoring_weight_temporal (default from NCMSConfig is 0.2).",
    )
    ap.add_argument(
        "--hierarchy-weight",
        type=float,
        default=None,
        help="Override scoring_weight_hierarchy (default via backend is 0.5).",
    )
    ap.add_argument(
        "--graph-weight",
        type=float,
        default=None,
        help="Override scoring_weight_graph (default 0.3).",
    )
    ap.add_argument(
        "--bm25-weight",
        type=float,
        default=None,
        help="Override scoring_weight_bm25 (default 0.6).",
    )
    ap.add_argument(
        "--splade-weight",
        type=float,
        default=None,
        help="Override scoring_weight_splade (default 0.3).",
    )
    ap.add_argument(
        "--splade-off",
        action="store_true",
        help="[Performance ablation] Disable SPLADE engine construction/index/search "
        "and set scoring_weight_splade=0.0.",
    )

    # Ablation flags — two master toggles mirroring NCMSConfig.
    ap.add_argument(
        "--temporal-off",
        action="store_true",
        help="Disable the temporal reasoning stack (temporal_enabled=False): "
        "TLG grammar, reconciliation, episodes, intent classifier, "
        "intent routing, temporal + hierarchy scoring weights.",
    )
    ap.add_argument(
        "--slm-off",
        action="store_true",
        help="Disable the 5-head LoRA classifier (intent_slot=None at "
        "MemoryService construction).  Ingest falls back to the "
        "regex/heuristic chain.",
    )
    ap.add_argument(
        "--head",
        default=None,
        choices=["admission", "state_change", "topic", "intent", "slot"],
        help="Evaluate ONE head in isolation.",
    )

    # Phase G ablation flags — fine-grained SLM signal toggles for
    # isolating which SLM-derived input is responsible for the
    # MSEB retrieval regression seen in Phase F.  Each one disables
    # ONE downstream consequence of the SLM's classification while
    # leaving the SLM itself enabled and producing labels.
    ap.add_argument(
        "--no-populate-domains",
        action="store_true",
        help="[Phase G ablation] Set slm_populate_domains=False — "
        "disables auto-appending the topic head's prediction "
        "to Memory.domains.  Hypothesis: domain auto-expansion "
        "widens the domain-filter surface area and adds noise "
        "to retrieval.",
    )
    ap.add_argument(
        "--no-reconciliation-penalty",
        action="store_true",
        help="[Phase G ablation] Zero out reconciliation supersession "
        "+ conflict penalties.  Hypothesis: SLM-driven state "
        "labels create more supersession edges; the per-result "
        "penalty pushes correct memories below their replacements.",
    )
    ap.add_argument(
        "--slm-confidence-threshold",
        type=float,
        default=None,
        help="[Phase G ablation] Override slm_confidence_threshold "
        "(default 0.3).  Use 0.7 to revert to the v6/v7-era "
        "floor — tests whether the lowered threshold admits "
        "low-confidence labels that perturb retrieval.",
    )
    ap.add_argument(
        "--entity-extraction-mode",
        choices=["slm_only", "gliner_only"],
        default=None,
        help="[Entity extraction ablation] Select the graph/query entity "
        "lane. slm_only never invokes GLiNER from NCMS application paths; "
        "gliner_only preserves the historical GLiNER entity lane.",
    )
    ap.add_argument(
        "--intent-alignment-weight",
        type=float,
        default=None,
        help="[Phase H.1 ablation] Override "
        "scoring_weight_intent_alignment (default 0.5).  Use "
        "0.0 to disable the per-memory intent × QueryIntent "
        "alignment bonus — tests whether the SLM's preference-"
        "intent label adds retrieval lift on PATTERN_LOOKUP / "
        "STRATEGIC_REFLECTION queries.",
    )
    ap.add_argument(
        "--role-grounding-weight",
        type=float,
        default=None,
        help="[Phase H.3 ablation] Override "
        "scoring_weight_role_grounding (default 0.5).  Use "
        "0.0 to disable the role-grounding bonus.  Tests "
        "whether boosting memories where the query entity has "
        "role=primary in the SLM's per-span output (vs "
        "casual/not_relevant) lifts retrieval across all "
        "query intent classes.",
    )
    ap.add_argument(
        "--state-change-alignment-weight",
        type=float,
        default=None,
        help="[Phase H.2 ablation] Override "
        "scoring_weight_state_change_alignment (default 0.5).  "
        "Use 0.0 to disable the per-memory state_change × "
        "QueryIntent alignment bonus.  Tests whether boosting "
        "memories tagged state_change=declaration or "
        "state_change=retirement on CHANGE_DETECTION queries "
        "lifts retrieval (small MSEB surface: 5 queries).",
    )

    args = ap.parse_args()
    feature_set = _parse_feature_set(args)

    out_dir = args.out_dir or DEFAULT_RESULTS_ROOT / args.domain
    run_id = _make_run_id(args.domain, args.backend, feature_set)

    backend_kwargs: dict[str, object] = {}
    if args.backend == "mem0":
        backend_kwargs["infer"] = args.mem0_infer
        backend_kwargs["rerank"] = args.mem0_rerank
    if args.backend == "ncms":
        # Loose typing because Phase G ablation flags inject bool
        # (slm_populate_domains) alongside the float scoring weights.
        weight_overrides: dict[str, object] = {}
        if args.temporal_weight is not None:
            weight_overrides["scoring_weight_temporal"] = args.temporal_weight
        if args.hierarchy_weight is not None:
            weight_overrides["scoring_weight_hierarchy"] = args.hierarchy_weight
        if args.graph_weight is not None:
            weight_overrides["scoring_weight_graph"] = args.graph_weight
        if args.bm25_weight is not None:
            weight_overrides["scoring_weight_bm25"] = args.bm25_weight
        if args.splade_weight is not None:
            weight_overrides["scoring_weight_splade"] = args.splade_weight
        if args.splade_off:
            weight_overrides["splade_enabled"] = False
            weight_overrides["scoring_weight_splade"] = 0.0
        # Phase G: SLM signal-isolation flags routed through the
        # same overrides dict so the ablation runs are a one-line
        # CLI change.
        if args.no_populate_domains:
            weight_overrides["slm_populate_domains"] = False
        if args.no_reconciliation_penalty:
            weight_overrides["reconciliation_supersession_penalty"] = 0.0
            weight_overrides["reconciliation_conflict_penalty"] = 0.0
        if args.slm_confidence_threshold is not None:
            weight_overrides["slm_confidence_threshold"] = args.slm_confidence_threshold
        if args.entity_extraction_mode is not None:
            weight_overrides["entity_extraction_mode"] = args.entity_extraction_mode
        if args.intent_alignment_weight is not None:
            weight_overrides["scoring_weight_intent_alignment"] = args.intent_alignment_weight
        if args.role_grounding_weight is not None:
            weight_overrides["scoring_weight_role_grounding"] = args.role_grounding_weight
        if args.state_change_alignment_weight is not None:
            weight_overrides["scoring_weight_state_change_alignment"] = (
                args.state_change_alignment_weight
            )
        if weight_overrides:
            backend_kwargs["ncms_config_overrides"] = weight_overrides

    cfg = RunConfig(
        domain=args.domain,
        build_dir=args.build_dir,
        backend=args.backend,
        adapter_domain=args.adapter_domain,
        feature_set=feature_set,
        out_dir=out_dir,
        run_id=run_id,
        top_k=args.top_k,
        ctlg_adapter_domain=args.ctlg_adapter_domain,
        ctlg_adapter_version=args.ctlg_adapter_version,
        backend_kwargs=backend_kwargs,
    )
    result = asyncio.run(run(cfg))
    print(
        json.dumps(
            {
                "run_id": run_id,
                "total_queries": result["total_queries"],
                "overall": result["overall"],
                "out_dir": str(out_dir),
            },
            indent=2,
        ),
        flush=True,
    )

    # Hard-exit to bypass asyncio cleanup hang at full corpus scale.
    # NCMS's GLiNER/SPLADE threadpools + sentence-transformers tokenizer
    # workers don't always drain cleanly; at pilot (188-memory) scale
    # they exit, at full scale (1,835+) the process hangs indefinitely
    # after results.json / summary.md have been written.  We've
    # persisted everything we care about, so fast-exit is safe.
    import os as _os

    sys.stdout.flush()
    sys.stderr.flush()
    _os._exit(0)


if __name__ == "__main__":
    main()
