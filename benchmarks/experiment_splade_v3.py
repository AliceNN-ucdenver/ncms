"""Side-by-side comparison: Splade_PP_en_v1 (fastembed/ONNX) vs splade-v3 (sentence-transformers).

Compares output quality, speed, and chunk counts on scifact documents.

Usage:
    uv run --with sentence-transformers python -m benchmarks.experiment_splade_v3
    uv run --with sentence-transformers python -m benchmarks.experiment_splade_v3 --docs 50
"""

from __future__ import annotations

import argparse
import time


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="SPLADE v1 vs v3 comparison")
    parser.add_argument("--docs", type=int, default=13, help="Number of scifact documents")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from benchmarks.datasets import load_beir_dataset

    print(f"Loading scifact (first {args.docs} docs)...")
    corpus, queries, qrels = load_beir_dataset("scifact")
    doc_ids = list(corpus.keys())[:args.docs]
    docs = []
    for did in doc_ids:
        d = corpus[did]
        title = d.get("title", "")
        text = d.get("text", "")
        content = f"{title}\n{text}".strip() if title else text
        docs.append(content[:10000])

    # Find queries that have relevant docs in our slice
    test_queries = []
    for qid, rels in qrels.items():
        if any(did in doc_ids for did in rels):
            test_queries.append((qid, queries[qid]))
    if not test_queries:
        test_queries = [(qid, queries[qid]) for qid in list(queries.keys())[:5]]
    print(f"  {len(docs)} documents, {len(test_queries)} test queries")

    # ─── Experiment 1: Current Splade_PP_en_v1 via fastembed ──────────
    print(f"\n{'='*70}")
    print("MODEL A: prithivida/Splade_PP_en_v1 (fastembed/ONNX, CPU)")
    print(f"{'='*70}")

    from ncms.infrastructure.text.chunking import chunk_text

    # Count chunks
    v1_chunk_count = sum(len(chunk_text(d, max_chars=400, overlap=50)) for d in docs)
    print("  Chunk config: 400 chars max, 50 overlap")
    print(f"  Total chunks: {v1_chunk_count} across {len(docs)} docs "
          f"({v1_chunk_count/len(docs):.1f} chunks/doc avg)")

    from ncms.domain.models import Memory
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine

    splade_v1 = SpladeEngine()

    # Warm up
    t0 = time.perf_counter()
    _warm = Memory(content="Warm up text for model loading", type="fact", id="warmup")
    splade_v1.index_memory(_warm)
    splade_v1.remove("warmup")
    print(f"  Warm-up: {(time.perf_counter()-t0)*1000:.0f}ms")

    # Index
    t0 = time.perf_counter()
    v1_vectors = {}
    for i, content in enumerate(docs):
        mem = Memory(content=content, type="fact", id=f"v1_{i}")
        splade_v1.index_memory(mem)
        v1_vectors[f"v1_{i}"] = splade_v1.get_vector(f"v1_{i}")
    v1_index_ms = (time.perf_counter() - t0) * 1000
    print(f"  Indexing: {v1_index_ms:.0f}ms ({v1_index_ms/len(docs):.0f}ms/doc)")

    # Search
    t0 = time.perf_counter()
    v1_results = {}
    for qid, qtext in test_queries[:5]:
        results = splade_v1.search(qtext, limit=10)
        v1_results[qid] = results
    v1_search_ms = (time.perf_counter() - t0) * 1000
    print(f"  Search ({min(5,len(test_queries))} queries): {v1_search_ms:.0f}ms")

    # Vector stats
    dims = [len(v.indices) for v in v1_vectors.values() if v]
    print(f"  Avg sparse dims: {sum(dims)/len(dims):.0f}" if dims else "  No vectors")

    # ─── Experiment 2: splade-v3 via sentence-transformers ────────────
    print(f"\n{'='*70}")
    print("MODEL B: naver/splade-v3 (sentence-transformers, auto device)")
    print(f"{'='*70}")

    try:
        from sentence_transformers import SparseEncoder
    except ImportError:
        print("  ERROR: sentence-transformers not installed.")
        print(
            "  Run: uv run --with sentence-transformers"
            " python -m benchmarks.experiment_splade_v3"
        )
        return

    # Chunk at 2000 chars (512 token window ≈ 2000 chars)
    v3_chunk_size = 2000
    v3_overlap = 100
    v3_chunk_count = sum(len(chunk_text(d, max_chars=v3_chunk_size, overlap=v3_overlap))
                         for d in docs)
    print(f"  Chunk config: {v3_chunk_size} chars max, {v3_overlap} overlap")
    print(f"  Total chunks: {v3_chunk_count} across {len(docs)} docs "
          f"({v3_chunk_count/len(docs):.1f} chunks/doc avg)")

    # Load model
    t0 = time.perf_counter()
    v3_model = SparseEncoder("naver/splade-v3")
    device = str(v3_model.device) if hasattr(v3_model, 'device') else 'unknown'
    print(f"  Model loaded on: {device}")
    print(f"  Warm-up: {(time.perf_counter()-t0)*1000:.0f}ms")

    # Index (encode documents)
    t0 = time.perf_counter()
    v3_doc_embeddings = v3_model.encode_document(docs)
    v3_index_ms = (time.perf_counter() - t0) * 1000
    print(f"  Indexing: {v3_index_ms:.0f}ms ({v3_index_ms/len(docs):.0f}ms/doc)")

    # Search
    query_texts = [qt for _, qt in test_queries[:5]]
    t0 = time.perf_counter()
    v3_query_embeddings = v3_model.encode_query(query_texts)
    v3_search_ms = (time.perf_counter() - t0) * 1000
    print(f"  Search ({len(query_texts)} queries): {v3_search_ms:.0f}ms")

    # Compute dot products for v3
    import torch

    def _sparse_dot(a, b):
        """Dot product handling PyTorch tensors, scipy sparse, or numpy arrays."""
        if isinstance(a, torch.Tensor):
            if a.is_sparse:
                return float(torch.sparse.mm(a.unsqueeze(0), b.unsqueeze(1)).item())
            return float(torch.dot(a.flatten(), b.flatten()).item())
        if hasattr(a, 'toarray'):  # scipy sparse
            return float((a @ b.T).toarray()[0, 0])
        import numpy as np
        return float(np.dot(np.asarray(a).flatten(), np.asarray(b).flatten()))

    v3_all_scores: dict[str, list] = {}
    for qi, (qid, _qtext) in enumerate(test_queries[:5]):
        q_emb = v3_query_embeddings[qi]
        scores = []
        for di in range(len(docs)):
            d_emb = v3_doc_embeddings[di]
            dot = _sparse_dot(q_emb, d_emb)
            scores.append((f"v3_{di}", dot))
        scores.sort(key=lambda x: x[1], reverse=True)
        v3_all_scores[qid] = scores

    # Vector stats
    sample = v3_doc_embeddings[0]
    if isinstance(sample, torch.Tensor):
        if sample.is_sparse:
            v3_dims = [emb._nnz() for emb in v3_doc_embeddings]
        else:
            v3_dims = [int((emb != 0).sum()) for emb in v3_doc_embeddings]
        print(f"  Avg sparse dims: {sum(v3_dims)/len(v3_dims):.0f}")
    elif hasattr(sample, 'nnz'):
        v3_dims = [emb.nnz for emb in v3_doc_embeddings]
        print(f"  Avg sparse dims: {sum(v3_dims)/len(v3_dims):.0f}")
    else:
        v3_dims = [0]

    # ─── Comparison ───────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("COMPARISON SUMMARY")
    print(f"{'='*70}")
    print(f"{'Metric':<30s}  {'Splade_PP_v1':>15s}  {'splade-v3':>15s}  {'Speedup':>10s}")
    print(f"{'─'*70}")
    print(f"{'Chunks/doc':<30s}  {v1_chunk_count/len(docs):>15.1f}  "
          f"{v3_chunk_count/len(docs):>15.1f}")
    print(f"{'Total chunks':<30s}  {v1_chunk_count:>15d}  {v3_chunk_count:>15d}")
    print(f"{'Index time (ms)':<30s}  {v1_index_ms:>15.0f}  {v3_index_ms:>15.0f}  "
          f"{v1_index_ms/v3_index_ms:>9.1f}x" if v3_index_ms > 0 else "")
    print(f"{'Index ms/doc':<30s}  {v1_index_ms/len(docs):>15.0f}  "
          f"{v3_index_ms/len(docs):>15.0f}")
    print(f"{'Search time (ms)':<30s}  {v1_search_ms:>15.0f}  {v3_search_ms:>15.0f}")
    if dims and v3_dims:
        print(f"{'Avg sparse dims':<30s}  {sum(dims)/len(dims):>15.0f}  "
              f"{sum(v3_dims)/len(v3_dims):>15.0f}")
    print(f"{'Device':<30s}  {'CPU (ONNX)':>15s}  {device:>15s}")

    # Show top results for first query
    if test_queries:
        qid, qtext = test_queries[0]
        print(f"\nTop-3 results for query: \"{qtext[:60]}...\"")
        v1_top = v1_results.get(qid, [])[:3]
        print(f"  v1: {[(r[0], f'{r[1]:.2f}') for r in v1_top]}")
        v3_top = v3_all_scores.get(qid, [])[:3]
        print(f"  v3: {[(r[0], f'{r[1]:.2f}') for r in v3_top]}")


if __name__ == "__main__":
    main()
