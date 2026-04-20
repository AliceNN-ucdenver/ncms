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

from benchmarks.mseb.metrics import (
    Prediction,
    aggregate,
    markdown_summary,
)
from benchmarks.mseb.schema import (
    CorpusMemory,
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
    """Boolean flags for every ablation axis.  All default True
    (full TLG + SLM).  ``--tlg-off`` clears the 4 TLG flags;
    ``--slm-off`` clears ``slm``."""

    temporal: bool = True
    ordinal: bool = True
    retirement: bool = True
    causal: bool = True
    preference: bool = True
    slm: bool = True

    # Single-head isolation (evaluates classifier for one head only).
    head: str | None = None  # admission|state_change|topic|intent|slot

    def to_dict(self) -> dict[str, object]:
        return {
            "temporal": self.temporal,
            "ordinal": self.ordinal,
            "retirement": self.retirement,
            "causal": self.causal,
            "preference": self.preference,
            "slm": self.slm,
            "head": self.head,
        }

    def to_ncms_config_overrides(self) -> dict[str, object]:
        """Translate feature flags into ``NCMSConfig(**overrides)``.

        Keys match ``ncms.config.NCMSConfig`` fields.  Each flag
        when ``False`` nudges the corresponding NCMS pipeline
        toward its baseline:

        - ``temporal=False``   → ``temporal_enabled=False`` +
          ``scoring_weight_temporal=0.0`` + ``scoring_weight_recency=0.0``
        - ``ordinal=False``    → ``intent_hierarchy_bonus=0.0`` +
          ``scoring_weight_hierarchy=0.0``
        - ``retirement=False`` → ``reconciliation_enabled=False``
        - ``causal=False``     → ``scoring_weight_graph=0.0`` +
          ``cooccurrence_max_entities=0``
        - ``slm=False``        → ``intent_slot_enabled=False``

        ``preference`` is enforced at prediction-time by stripping
        the intent head's contribution (no config knob required).
        """
        ov: dict[str, object] = {}
        if not self.temporal:
            ov.update({
                "temporal_enabled": False,
                "scoring_weight_temporal": 0.0,
                "scoring_weight_recency": 0.0,
            })
        if not self.ordinal:
            ov.update({
                "intent_hierarchy_bonus": 0.0,
                "scoring_weight_hierarchy": 0.0,
            })
        if not self.retirement:
            ov.update({"reconciliation_enabled": False})
        if not self.causal:
            ov.update({
                "scoring_weight_graph": 0.0,
                "cooccurrence_max_entities": 0,
            })
        if not self.slm:
            ov.update({"intent_slot_enabled": False})
        return ov


# ---------------------------------------------------------------------------
# CLI-flag parsing
# ---------------------------------------------------------------------------


def _parse_feature_set(args: argparse.Namespace) -> FeatureSet:
    fs = FeatureSet()
    if args.tlg_off:
        fs.temporal = fs.ordinal = fs.retirement = fs.causal = False
    if args.no_temporal:
        fs.temporal = False
    if args.no_ordinal:
        fs.ordinal = False
    if args.no_retirement:
        fs.retirement = False
    if args.no_causal:
        fs.causal = False
    if args.no_preference:
        fs.preference = False
    if args.slm_off:
        fs.slm = False
    fs.head = args.head
    return fs


# ---------------------------------------------------------------------------
# NCMS instance lifecycle — mirrors benchmarks/longmemeval/harness.py
# ---------------------------------------------------------------------------


async def _create_ncms_instance(
    *,
    feature_set: FeatureSet,
    adapter_domain: str | None,
    shared_splade: object | None = None,
    shared_intent_slot: object | None = None,
):
    """Create a fresh in-memory NCMS instance with the given feature set."""
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()
    splade = shared_splade if shared_splade is not None else SpladeEngine()

    # Resolve SLM adapter only if the feature set allows it.
    intent_slot = shared_intent_slot
    if feature_set.slm and intent_slot is None and adapter_domain is not None:
        from benchmarks.intent_slot_adapter import get_intent_slot_chain

        intent_slot = get_intent_slot_chain(
            domain=adapter_domain,
            include_e5_fallback=False,
        )

    # Base config = the SciFact-tuned defaults used elsewhere.
    base_config_kwargs: dict[str, object] = {
        "db_path": ":memory:",
        "actr_noise": 0.0,
        "splade_enabled": True,
        "scoring_weight_bm25": 0.6,
        "scoring_weight_actr": 0.0,
        "scoring_weight_splade": 0.3,
        "scoring_weight_graph": 0.3,
        "contradiction_detection_enabled": False,
        "intent_slot_enabled": feature_set.slm and intent_slot is not None,
        "intent_slot_populate_domains": True,
    }
    base_config_kwargs.update(feature_set.to_ncms_config_overrides())
    config = NCMSConfig(**base_config_kwargs)

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
        intent_slot=intent_slot,
    )
    await svc.start_index_pool()
    return store, index, graph, splade, config, svc


# ---------------------------------------------------------------------------
# Ingestion — store corpus memories in observed_at order per subject
# ---------------------------------------------------------------------------


async def _ingest_corpus(svc: object, corpus: list[CorpusMemory]) -> dict[str, str]:
    """Store every memory; return mapping corpus_mid → ncms_memory_id."""
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]
    mid_map: dict[str, str] = {}

    # Ingest in (subject, observed_at) order so temporal scoring is
    # consistent.  Harness is deterministic → the same corpus always
    # lands in the same ACT-R decay state.
    ordered = sorted(corpus, key=lambda m: (m.subject, m.observed_at, m.mid))
    for m in ordered:
        try:
            observed_at = datetime.fromisoformat(
                m.observed_at.replace("Z", "+00:00"),
            ).astimezone(UTC)
        except ValueError:
            observed_at = None

        memory = await svc_typed.store_memory(
            content=m.content,
            memory_type="fact",
            source_agent=m.metadata.get("source_agent", "mseb"),
            domains=m.metadata.get("domains") or [],
            tags=["mseb", f"subject:{m.subject}", f"mid:{m.mid}"],
            observed_at=observed_at,
            # NOTE: importance could be pulled from metadata but the
            # corpora don't carry importance scores in v1.
        )
        mid_map[m.mid] = memory.id
    return mid_map


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


async def _run_queries(
    svc: object,
    queries: list[GoldQuery],
    *,
    top_k: int = 10,
) -> list[Prediction]:
    """Run each gold query through ``svc.search``; build Predictions."""
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]
    preds: list[Prediction] = []

    for q in queries:
        t0 = time.perf_counter()
        try:
            results = await svc_typed.search(query=q.text, limit=top_k)
        except Exception as exc:  # pragma: no cover — surface in log
            logger.warning("qid=%s search failed: %s", q.qid, exc)
            results = []
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Recover the corpus mid via the "mid:<x>" tag we stamped at ingest.
        ranked_mids: list[str] = []
        for r in results:
            memory = getattr(r, "memory", r)
            tags = getattr(memory, "tags", []) or []
            mid = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("mid:")),
                None,
            )
            if mid is not None:
                ranked_mids.append(mid)

        preds.append(Prediction(
            qid=q.qid,
            ranked_mids=ranked_mids,
            latency_ms=latency_ms,
        ))
    return preds


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    domain: str
    build_dir: Path
    adapter_domain: str | None
    feature_set: FeatureSet
    out_dir: Path
    run_id: str
    top_k: int = 10
    head_gold: dict[str, dict[str, str]] | None = field(default=None)


async def run(cfg: RunConfig) -> dict[str, object]:
    """End-to-end MSEB run → results dict."""
    corpus = load_corpus(cfg.build_dir / "corpus.jsonl")
    queries = load_queries(cfg.build_dir / "queries.jsonl")

    logger.info(
        "domain=%s corpus=%d queries=%d adapter=%s feature_set=%s",
        cfg.domain, len(corpus), len(queries), cfg.adapter_domain,
        cfg.feature_set.to_dict(),
    )

    _store, _index, _graph, _splade, _config, svc = await _create_ncms_instance(
        feature_set=cfg.feature_set,
        adapter_domain=cfg.adapter_domain,
    )

    t_ingest = time.perf_counter()
    await _ingest_corpus(svc, corpus)
    ingest_secs = time.perf_counter() - t_ingest
    logger.info("ingested %d memories in %.1fs", len(corpus), ingest_secs)

    t_query = time.perf_counter()
    preds = await _run_queries(svc, queries, top_k=cfg.top_k)
    query_secs = time.perf_counter() - t_query
    logger.info("ran %d queries in %.1fs", len(preds), query_secs)

    result = aggregate(
        preds, queries,
        head_gold=cfg.head_gold,
    )
    result["run_id"] = cfg.run_id
    result["domain"] = cfg.domain
    result["feature_set"] = cfg.feature_set.to_dict()
    result["ingest_seconds"] = ingest_secs
    result["query_seconds"] = query_secs

    # Persist.
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / f"{cfg.run_id}.results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str),
    )
    (cfg.out_dir / f"{cfg.run_id}.summary.md").write_text(
        markdown_summary(result, run_id=cfg.run_id),
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _make_run_id(domain: str, feature_set: FeatureSet) -> str:
    parts = [domain]
    if not feature_set.slm:
        parts.append("slm-off")
    flags_off = [
        name for name, on in feature_set.to_dict().items()
        if name not in {"head", "slm"} and on is False
    ]
    if flags_off:
        parts.append("off-" + "-".join(flags_off))
    else:
        parts.append("tlg-on")
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
    ap.add_argument("--domain", required=True,
                    help="Domain identifier, e.g. mseb_swe / mseb_clinical / mseb_convo")
    ap.add_argument("--build-dir", type=Path, required=True,
                    help="Directory containing corpus.jsonl + queries.jsonl")
    ap.add_argument("--adapter-domain", default=None,
                    help="LoRA adapter domain (software_dev / clinical / conversational).  "
                         "Required when SLM is on.")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Where to write results (default: benchmarks/results/mseb/<domain>/)")
    ap.add_argument("--top-k", type=int, default=10)

    # Ablation flags.
    ap.add_argument("--tlg-off", action="store_true",
                    help="Shortcut for --no-temporal --no-ordinal --no-retirement --no-causal")
    ap.add_argument("--no-temporal", action="store_true")
    ap.add_argument("--no-ordinal", action="store_true")
    ap.add_argument("--no-retirement", action="store_true")
    ap.add_argument("--no-causal", action="store_true")
    ap.add_argument("--no-preference", action="store_true")
    ap.add_argument("--slm-off", action="store_true")
    ap.add_argument("--head", default=None,
                    choices=["admission", "state_change", "topic", "intent", "slot"],
                    help="Evaluate ONE head in isolation")

    args = ap.parse_args()
    feature_set = _parse_feature_set(args)

    out_dir = args.out_dir or DEFAULT_RESULTS_ROOT / args.domain
    run_id = _make_run_id(args.domain, feature_set)
    cfg = RunConfig(
        domain=args.domain,
        build_dir=args.build_dir,
        adapter_domain=args.adapter_domain,
        feature_set=feature_set,
        out_dir=out_dir,
        run_id=run_id,
        top_k=args.top_k,
    )
    result = asyncio.run(run(cfg))
    print(json.dumps({
        "run_id": run_id,
        "total_queries": result["total_queries"],
        "overall": result["overall"],
        "out_dir": str(out_dir),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
