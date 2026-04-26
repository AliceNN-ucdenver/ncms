"""MSEB build — labeled JSONL + gold YAML → canonical JSONL pair.

Reads a domain's ``raw_labeled/<subject>.jsonl`` set (produced by
``benchmarks/mseb_<domain>/label.py``) and its hand-authored
``gold.yaml``, emits the two canonical JSONL files the harness
consumes:

- ``corpus.jsonl``  — one ``CorpusMemory`` per line
- ``queries.jsonl`` — one ``GoldQuery`` per line

The ``raw_labeled`` format is per-line JSON with at minimum these
keys (Phase 2 labeler output)::

    {"mid": "swe-astropy-12907-m01",
     "subject": "swe-astropy-12907",
     "content": "...",
     "observed_at": "2023-07-15T14:32:00Z",
     "entities": [...],
     "metadata": {"kind": "declaration", "source": "issue_body", ...}}

The ``gold.yaml`` schema is the authored query list::

    - qid: swe-origin-001
      shape: origin
      text: "Where was the separability_matrix bug first reported?"
      subject: swe-astropy-12907
      gold_mid: swe-astropy-12907-m01
      gold_alt: []               # optional
      preference: none           # optional, defaults to "none"
      entity: null               # optional
      note: ""                   # optional
      expected_proof_pattern: null

Usage::

    # Build one domain
    uv run python -m benchmarks.mseb.build \
        --labeled-dir benchmarks/mseb_swe/raw_labeled \
        --gold-yaml   benchmarks/mseb_swe/gold.yaml \
        --out-dir     benchmarks/mseb_swe/build
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from benchmarks.mseb.schema import (
    INTENT_SHAPES,
    MESSAGE_KINDS,
    PREFERENCE_KINDS,
    QUERY_CLASSES,
    CorpusMemory,
    GoldQuery,
    dump_corpus,
    dump_queries,
)

logger = logging.getLogger("mseb.build")


# ---------------------------------------------------------------------------
# Labeled → CorpusMemory
# ---------------------------------------------------------------------------


def load_labeled_dir(path: Path) -> list[CorpusMemory]:
    """Read every ``*.jsonl`` under ``path``, emit sorted CorpusMemory list.

    Files are read in sorted order; memories inside a file in
    their native order.  This keeps the corpus.jsonl
    byte-reproducible given the same inputs.
    """
    out: list[CorpusMemory] = []
    seen_mids: set[str] = set()
    for jsonl in sorted(path.glob("*.jsonl")):
        if jsonl.name.startswith("_"):  # skip _stats.json, _questions.jsonl
            continue
        for line in jsonl.read_text(encoding="utf-8").split(chr(10)):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            # Honour optional provenance fields in metadata.
            mid = row.get("mid") or row.get("message_id")
            if not mid:
                logger.warning("row in %s missing mid/message_id — skipping", jsonl)
                continue
            if mid in seen_mids:
                logger.warning("duplicate mid %s (second copy in %s)", mid, jsonl)
                continue
            seen_mids.add(mid)

            kind = row.get("metadata", {}).get("kind", "none")
            if kind not in MESSAGE_KINDS:
                logger.warning(
                    "mid=%s unknown kind=%r → coerced to 'none'",
                    mid,
                    kind,
                )
                row.setdefault("metadata", {})["kind"] = "none"

            out.append(
                CorpusMemory(
                    mid=mid,
                    subject=row["subject"],
                    content=row["content"],
                    observed_at=row["observed_at"],
                    entities=row.get("entities", []),
                    metadata=row.get("metadata", {}),
                )
            )
    logger.info("loaded %d memories from %s", len(out), path)
    return out


# ---------------------------------------------------------------------------
# gold.yaml → GoldQuery
# ---------------------------------------------------------------------------


def load_gold_yaml(path: Path) -> list[GoldQuery]:
    """Parse a hand-authored gold.yaml into typed GoldQuery rows.

    Uses ``yaml`` if installed; falls back to JSON-on-a-list for
    CI environments without PyYAML.
    """
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        rows = yaml.safe_load(text) or []
    except ImportError:
        # Minimal fallback: a JSON array in the same file.
        rows = json.loads(text) if text.strip().startswith("[") else []

    out: list[GoldQuery] = []
    seen_qids: set[str] = set()
    for i, row in enumerate(rows):
        qid = row.get("qid")
        if not qid:
            logger.warning("row %d: missing qid — skipping", i)
            continue
        if qid in seen_qids:
            logger.warning("duplicate qid %s — skipping second instance", qid)
            continue
        seen_qids.add(qid)

        shape = row.get("shape", "current_state")
        if shape not in INTENT_SHAPES:
            logger.warning(
                "qid=%s: unknown shape=%r → coerced to 'current_state'",
                qid,
                shape,
            )
            shape = "current_state"

        pref = row.get("preference", "none")
        if pref not in PREFERENCE_KINDS:
            logger.warning(
                "qid=%s: unknown preference=%r → coerced to 'none'",
                qid,
                pref,
            )
            pref = "none"

        qclass = row.get("query_class", "general")
        if qclass not in QUERY_CLASSES:
            logger.warning(
                "qid=%s: unknown query_class=%r → coerced to 'general'",
                qid,
                qclass,
            )
            qclass = "general"

        out.append(
            GoldQuery(
                qid=qid,
                shape=shape,
                text=row.get("text", ""),
                subject=row.get("subject", ""),
                entity=row.get("entity"),
                gold_mid=row.get("gold_mid", ""),
                gold_alt=row.get("gold_alt", []) or [],
                expected_proof_pattern=row.get("expected_proof_pattern"),
                note=row.get("note", ""),
                preference=pref,
                query_class=qclass,
            )
        )
    logger.info("loaded %d gold queries from %s", len(out), path)
    return out


# ---------------------------------------------------------------------------
# Validation — ensure every gold query points at a real mid
# ---------------------------------------------------------------------------


def validate(
    corpus: list[CorpusMemory],
    queries: list[GoldQuery],
) -> dict[str, int]:
    """Consistency checks.

    Fails loudly for missing gold mids (would silently tank rank-1).
    Returns a dict of counts for the build log.
    """
    mids = {m.mid for m in corpus}
    subjects = {m.subject for m in corpus}

    unknown_gold: list[str] = []
    unknown_subject: list[str] = []
    for q in queries:
        for acc in {q.gold_mid, *q.gold_alt} - {""}:
            if acc not in mids:
                unknown_gold.append(f"{q.qid}:{acc}")
        if q.subject and q.subject not in subjects:
            unknown_subject.append(f"{q.qid}:{q.subject}")

    if unknown_gold:
        raise ValueError(
            f"{len(unknown_gold)} gold mids not present in corpus: {unknown_gold[:5]} …",
        )
    if unknown_subject:
        logger.warning(
            "%d queries reference unknown subjects (e.g. %s) — "
            "harness will still run but these queries will return empty",
            len(unknown_subject),
            unknown_subject[:3],
        )
    return {
        "corpus_memories": len(corpus),
        "corpus_subjects": len(subjects),
        "gold_queries": len(queries),
        "queries_with_alternates": sum(1 for q in queries if q.gold_alt),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build(
    *,
    labeled_dir: Path,
    gold_yaml: Path,
    out_dir: Path,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus = load_labeled_dir(labeled_dir)
    queries = load_gold_yaml(gold_yaml)
    stats = validate(corpus, queries)

    dump_corpus(corpus, out_dir / "corpus.jsonl")
    dump_queries(queries, out_dir / "queries.jsonl")
    (out_dir / "_build_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    logger.info(
        "built %d memories + %d queries → %s",
        stats["corpus_memories"],
        stats["gold_queries"],
        out_dir,
    )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="MSEB build: labeled + gold → canonical JSONL")
    ap.add_argument("--labeled-dir", type=Path, required=True)
    ap.add_argument("--gold-yaml", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    stats = build(
        labeled_dir=args.labeled_dir,
        gold_yaml=args.gold_yaml,
        out_dir=args.out_dir,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
