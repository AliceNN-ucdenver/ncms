"""BEIR benchmark dataset loading.

Downloads and caches BEIR datasets from the official distribution.
Parses corpus.jsonl, queries.jsonl, and qrels/test.tsv.

Supported datasets (laptop-friendly sizes):
- scifact:  5,183 docs,  300 queries  (science fact verification)
- nfcorpus: 3,633 docs,  323 queries  (biomedical)
- arguana:  8,674 docs, 1406 queries  (argument retrieval)
"""

from __future__ import annotations

import csv
import json
import logging
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

BEIR_BASE_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"

# Domain-specific GLiNER labels for each dataset
DATASET_TOPICS: dict[str, dict[str, list[str] | str]] = {
    "scifact": {
        "domain": "science",
        # D_synonym_swap: 9.1 ent/doc, 181 unique — best doc coverage in taxonomy test
        "labels": [
            "medical_condition", "medication", "protein", "gene",
            "chemical_compound", "organism", "cell_type", "tissue",
            "symptom", "therapy",
        ],
    },
    "nfcorpus": {
        "domain": "biomedical",
        # D_nutrition_bio: 9.3 ent/doc, 174 unique — nutrition-specific labels
        "labels": [
            "disease", "nutrient", "vitamin", "mineral", "drug",
            "food", "protein", "compound", "symptom", "treatment",
        ],
    },
    "arguana": {
        "domain": "argument",
        # A_traditional: 4.4 ent/doc, 77% query overlap — best for graph connectivity
        "labels": [
            "person", "organization", "location", "nationality",
            "event", "law",
        ],
    },
}

# SWE-bench Django entity labels (code-entity tuned for GLiNER)
SWEBENCH_TOPICS: dict[str, dict[str, list[str] | str]] = {
    "swebench_django": {
        "domain": "django",
        "labels": [
            "class", "method", "function", "module", "field",
            "model", "view", "middleware", "url_pattern", "form",
            "template", "queryset", "manager", "migration", "signal",
            "test_case", "exception", "setting", "command", "mixin",
        ],
    },
}

SUPPORTED_DATASETS = list(DATASET_TOPICS.keys())

# Default cache directory for downloaded datasets
_CACHE_DIR = Path.home() / ".cache" / "ncms" / "beir"


def _download_dataset(name: str, cache_dir: Path) -> Path:
    """Download and extract a BEIR dataset zip file.

    Returns the path to the extracted dataset directory.
    """
    dataset_dir = cache_dir / name
    marker = dataset_dir / ".complete"

    if marker.exists():
        logger.info("Using cached dataset: %s", dataset_dir)
        return dataset_dir

    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / f"{name}.zip"
    url = f"{BEIR_BASE_URL}/{name}.zip"

    logger.info("Downloading %s from %s ...", name, url)
    urllib.request.urlretrieve(url, zip_path)
    logger.info("  Downloaded %.1f MB", zip_path.stat().st_size / 1_048_576)

    logger.info("Extracting to %s ...", cache_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(cache_dir)

    # Clean up zip
    zip_path.unlink()

    # Write completion marker
    marker.touch()

    return dataset_dir


def _load_corpus(dataset_dir: Path) -> dict[str, dict[str, str]]:
    """Load corpus from corpus.jsonl."""
    corpus_path = dataset_dir / "corpus.jsonl"
    corpus: dict[str, dict[str, str]] = {}

    with open(corpus_path) as f:
        for line in f:
            row = json.loads(line)
            corpus[str(row["_id"])] = {
                "title": row.get("title", "") or "",
                "text": row.get("text", "") or "",
            }

    return corpus


def _load_queries(dataset_dir: Path) -> dict[str, str]:
    """Load queries from queries.jsonl."""
    queries_path = dataset_dir / "queries.jsonl"
    queries: dict[str, str] = {}

    with open(queries_path) as f:
        for line in f:
            row = json.loads(line)
            queries[str(row["_id"])] = row["text"]

    return queries


def _load_qrels(dataset_dir: Path, split: str = "test") -> dict[str, dict[str, int]]:
    """Load qrels from qrels/{split}.tsv."""
    qrels_path = dataset_dir / "qrels" / f"{split}.tsv"
    qrels: dict[str, dict[str, int]] = {}

    with open(qrels_path) as f:
        reader = csv.reader(f, delimiter="\t")
        next(reader, None)  # Skip header
        for row in reader:
            if len(row) < 3:
                continue
            qid, did, score = row[0], row[1], int(row[2])
            if qid not in qrels:
                qrels[qid] = {}
            qrels[qid][did] = score

    return qrels


def load_beir_dataset(
    name: str,
    cache_dir: str | Path | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, dict[str, int]]]:
    """Load a BEIR dataset (downloading if not cached).

    Args:
        name: Dataset name (scifact, nfcorpus, arguana).
        cache_dir: Directory to cache downloaded datasets.
            Defaults to ~/.cache/ncms/beir.

    Returns:
        Tuple of:
        - corpus: {doc_id: {"title": str, "text": str}}
        - queries: {query_id: query_text}
        - qrels: {query_id: {doc_id: relevance_grade}}

    Raises:
        ValueError: If dataset name is not supported.
    """
    if name not in SUPPORTED_DATASETS:
        msg = f"Unsupported dataset: {name}. Supported: {SUPPORTED_DATASETS}"
        raise ValueError(msg)

    cache = Path(cache_dir) if cache_dir else _CACHE_DIR

    logger.info("Loading BEIR dataset: %s", name)
    dataset_dir = _download_dataset(name, cache)

    corpus = _load_corpus(dataset_dir)
    logger.info("  Corpus: %d documents", len(corpus))

    queries = _load_queries(dataset_dir)
    logger.info("  Queries: %d", len(queries))

    qrels = _load_qrels(dataset_dir)
    logger.info("  Qrels: %d queries with judgments", len(qrels))

    return corpus, queries, qrels
