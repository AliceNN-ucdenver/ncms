"""MSEB — Memory State-Evolution Benchmark (framework).

Reusable multi-domain benchmark for typed state-change retrieval.
Shared schema + metrics + harness; pluggable per-domain instantiations.

See ``benchmarks/mseb/README.md`` for the full methodology,
documentation contract, and add-a-domain playbook.
"""

from benchmarks.mseb.schema import (
    INTENT_SHAPES,
    MESSAGE_KINDS,
    CorpusMemory,
    GoldQuery,
    MemoryKind,
    dump_corpus,
    dump_queries,
    load_corpus,
    load_queries,
)

__all__ = [
    "CorpusMemory",
    "GoldQuery",
    "INTENT_SHAPES",
    "MESSAGE_KINDS",
    "MemoryKind",
    "dump_corpus",
    "dump_queries",
    "load_corpus",
    "load_queries",
]
