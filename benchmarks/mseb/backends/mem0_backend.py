"""mem0 backend — competitor memory system.

Wraps ``mem0.Memory`` behind the :class:`MemoryBackend` protocol.

Configuration (all overridable via constructor kwargs):

- **LLM**: OpenAI-compatible API at ``http://spark-ee7d.local:8000/v1``
  serving ``nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16``.  Matches
  the vLLM endpoint we already run for NCMS contradiction
  detection / consolidation.  Set via
  ``NCMS_LLM_API_BASE`` + ``NCMS_LLM_MODEL`` env vars or pass
  explicit values on construction.
- **Embedder**: ``sentence-transformers/all-MiniLM-L6-v2`` (local,
  fast, mem0's default).  All embedding is in-process — no cloud
  API, no extra service.
- **Vector store**: Chroma in-memory.  Fresh per run; no
  cross-run leakage.

Design decisions vs mem0 defaults:

- ``infer=False`` on every ``add()`` — we store content verbatim
  rather than letting mem0's LLM extract "facts".  This keeps the
  comparison apples-to-apples with NCMS (which also stores raw
  content).  A separate "mem0-infer" configuration can be run
  later to quantify mem0's LLM-extraction contribution (see
  `infer=True` constructor arg).
- ``rerank=False`` on every ``search()`` — mem0's LLM reranker
  introduces variance we want off by default.  Again, a
  separate "mem0-rerank" config can enable it.
- All memories land under a single ``user_id = "mseb-corpus"``
  because MSEB grades global retrieval; mem0 requires a user_id
  on search, so we use one global bucket.  Subject is recovered
  from ``metadata.subject`` on result rows (not used for
  scoring; preserved for per-subject diagnostics).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from benchmarks.mseb.backends.base import BackendRanking

if TYPE_CHECKING:
    from benchmarks.mseb.schema import CorpusMemory

logger = logging.getLogger("mseb.backends.mem0")

GLOBAL_USER_ID = "mseb-corpus"


@dataclass
class Mem0Backend:
    """mem0.Memory configured to use our Spark + local embeddings."""

    # Factory-time knobs — harness passes adapter_domain for
    # interface parity; mem0 ignores it.
    adapter_domain: str | None = None

    # LLM config — defaults mirror NCMS's vLLM endpoint.
    llm_model: str = field(
        default_factory=lambda: os.environ.get(
            "NCMS_LLM_MODEL",
            "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        ).removeprefix("openai/"),
    )
    llm_api_base: str = field(
        default_factory=lambda: os.environ.get(
            "NCMS_LLM_API_BASE",
            "http://spark-ee7d.local:8000/v1",
        ),
    )
    llm_api_key: str = field(
        default_factory=lambda: os.environ.get(
            "OPENAI_API_KEY", "EMPTY",
        ),
    )

    # Embedder + vector store — kept local for reproducibility.
    embedder_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    collection_name: str = "mseb"

    # Pipeline configuration — see module docstring.
    infer: bool = False
    rerank: bool = False

    # Unused but accepted for constructor parity with NcmsBackend.
    feature_set: object | None = None
    shared_splade: object | None = None
    shared_intent_slot: object | None = None

    name: str = "mem0"
    _memory: object | None = field(default=None, init=False, repr=False)
    _tmp_path: str | None = field(default=None, init=False, repr=False)

    # -------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------

    async def setup(self) -> None:
        from mem0 import Memory

        # mem0's Chroma backend takes a filesystem path (literal
        # ``":memory:"`` would create a directory with that name).
        # Use a tempdir that ``shutdown()`` removes — same fresh-
        # per-run semantics without the stray folder.
        self._tmp_path = tempfile.mkdtemp(prefix="mseb-mem0-chroma-")

        config = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "openai_base_url": self.llm_api_base,
                    "api_key": self.llm_api_key,
                    "temperature": 0.0,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {"model": self.embedder_model},
            },
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": self.collection_name,
                    "path": self._tmp_path,
                },
            },
        }
        # Memory() itself is sync; run in a thread so we don't block
        # the harness's event loop during cold model load (MiniLM is
        # ~80MB but still spends 0.5-2s on first init).
        self._memory = await asyncio.to_thread(Memory.from_config, config)
        # Log the actual runtime config (unambiguous, greppable).
        logger.info(
            "mem0 runtime config: llm=%s api_base=%s embedder=%s "
            "collection=%s infer=%s rerank=%s user_id=%s tmp_path=%s",
            self.llm_model, self.llm_api_base, self.embedder_model,
            self.collection_name, self.infer, self.rerank,
            GLOBAL_USER_ID, self._tmp_path,
        )

    # -------------------------------------------------------------------
    # Ingest
    # -------------------------------------------------------------------

    async def ingest(
        self, memories: list[CorpusMemory],
    ) -> dict[str, str]:
        if self._memory is None:
            raise RuntimeError("setup() must be called before ingest()")
        mid_map: dict[str, str] = {}

        # Ordering doesn't matter for mem0 (no temporal scoring), but
        # we keep the same ordering NCMS uses for determinism.
        ordered = sorted(
            memories, key=lambda m: (m.subject, m.observed_at, m.mid),
        )

        for m in ordered:
            metadata = {
                "mid": m.mid,
                "subject": m.subject,
                "observed_at": m.observed_at,
                **{
                    k: v for k, v in (m.metadata or {}).items()
                    if isinstance(v, (str, int, float, bool))
                },
            }
            try:
                resp = await asyncio.to_thread(
                    self._memory.add,        # type: ignore[union-attr]
                    m.content,
                    user_id=GLOBAL_USER_ID,
                    metadata=metadata,
                    infer=self.infer,
                )
            except Exception as exc:  # pragma: no cover — log + skip
                logger.warning("mem0 add failed for %s: %s", m.mid, exc)
                continue

            # `add` returns {"results": [{"id": "...", ...}, ...]};
            # with infer=False that's a single row per call.
            for row in resp.get("results", []):
                mid_map[m.mid] = row.get("id", "")
                break
        return mid_map

    # -------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------

    async def search(
        self, query: str, *, limit: int = 10,
    ) -> list[BackendRanking]:
        if self._memory is None:
            raise RuntimeError("setup() must be called before search()")

        try:
            resp = await asyncio.to_thread(
                self._memory.search,    # type: ignore[union-attr]
                query,
                user_id=GLOBAL_USER_ID,
                limit=limit,
                rerank=self.rerank,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("mem0 search failed: %s", exc)
            return []

        rankings: list[BackendRanking] = []
        for rank, row in enumerate(resp.get("results", [])):
            meta = row.get("metadata") or {}
            mid = meta.get("mid")
            if mid is None:
                continue
            # mem0 emits distance-like score (lower = better) on
            # Chroma.  Convert to similarity so BackendRanking's
            # "larger is better" convention holds.  We keep the
            # *ranking order* — the harness only looks at the order,
            # not absolute values.
            raw_score = row.get("score", rank)
            try:
                score = float(raw_score)
                similarity = 1.0 - score if score <= 1.0 else 1.0 / (1.0 + score)
            except (TypeError, ValueError):
                similarity = 1.0 / (rank + 1)
            rankings.append(BackendRanking(
                mid=str(mid),
                score=similarity,
                raw={"raw_score": raw_score, "rank": rank},
            ))
        return rankings

    # -------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------

    async def shutdown(self) -> None:
        # Drop mem0 ref so a second run in the same process gets a
        # clean slate; then remove the tempdir Chroma persisted to.
        self._memory = None
        if self._tmp_path:
            shutil.rmtree(self._tmp_path, ignore_errors=True)
            self._tmp_path = None


__all__ = ["Mem0Backend", "GLOBAL_USER_ID"]
