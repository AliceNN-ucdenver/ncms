"""Tantivy BM25 search engine for memory retrieval.

Uses the tantivy-py bindings for a Rust-based inverted index.
Provides sub-millisecond search with BM25 scoring.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import tantivy

from ncms.domain.models import Memory


class TantivyEngine:
    """BM25 full-text search engine backed by Tantivy."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._index: tantivy.Index | None = None
        self._schema: tantivy.Schema | None = None
        self._index_dir: str | None = None  # Actual directory where index files live
        self._writer_lock = threading.Lock()  # Tantivy allows only one writer at a time

    def initialize(self, path: str | None = None) -> None:
        effective_path = path or self._path

        builder = tantivy.SchemaBuilder()
        builder.add_text_field("memory_id", stored=True, tokenizer_name="raw")
        builder.add_text_field("content", stored=False, tokenizer_name="en_stem")
        builder.add_text_field("domains", stored=False, tokenizer_name="default")
        builder.add_text_field("tags", stored=False, tokenizer_name="default")
        self._schema = builder.build()

        if effective_path:
            index_dir = Path(effective_path)
            index_dir.mkdir(parents=True, exist_ok=True)
            self._index_dir = str(index_dir)
            self._index = tantivy.Index(self._schema, path=str(index_dir))
        else:
            # In-memory index using a temp directory
            tmp_dir = tempfile.mkdtemp(prefix="ncms_index_")
            self._index_dir = tmp_dir
            self._index = tantivy.Index(self._schema, path=tmp_dir)

    @property
    def index(self) -> tantivy.Index:
        if self._index is None:
            self.initialize()
        assert self._index is not None
        return self._index

    def index_memory(self, memory: Memory) -> None:
        with self._writer_lock:
            writer = self.index.writer()
            writer.add_document(
                tantivy.Document(
                    memory_id=memory.id,
                    content=memory.content,
                    domains=" ".join(memory.domains),
                    tags=" ".join(memory.tags),
                )
            )
            writer.commit()

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        """Search the index and return (memory_id, bm25_score) pairs."""
        self.index.reload()
        searcher = self.index.searcher()

        # Lenient parse silently drops invalid syntax while preserving
        # all valid terms — no manual sanitization needed.
        parsed, _errors = self.index.parse_query_lenient(query, ["content", "domains", "tags"])
        results = searcher.search(parsed, limit).hits

        scored: list[tuple[str, float]] = []
        for score, doc_address in results:
            doc = searcher.doc(doc_address)
            memory_ids = doc.get_first("memory_id")
            if memory_ids:
                scored.append((str(memory_ids), float(score)))

        return scored

    def remove(self, memory_id: str) -> None:
        with self._writer_lock:
            writer = self.index.writer()
            writer.delete_documents("memory_id", memory_id)
            writer.commit()
