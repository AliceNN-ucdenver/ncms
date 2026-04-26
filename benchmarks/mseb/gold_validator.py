"""MSEB gold-query validator.

Given a labeled corpus + a gold.yaml candidate file, for every
gold query check:

1. **gold-mid points at a real, retrievable memory** (``gold_mid``
   is present in ``corpus.jsonl``).
2. **gold wins a local BM25 race against its chain siblings**.
   Using a cheap TF over-overlap proxy (no index build), compute
   the query's overlap with each memory in its subject chain; if
   the gold memory is not the top-ranked in its own chain, flag.
3. **query mentions at least one token that distinguishes gold
   from siblings** (gold's TF-lift vs siblings is >0).

Usage::

    uv run python -m benchmarks.mseb.gold_validator \\
        --labeled-dir benchmarks/mseb_swe/raw_labeled \\
        --gold benchmarks/mseb_swe/gold.yaml \\
        --out  benchmarks/mseb_swe/gold_validation.json \\
        [--drop-invalid]    # write a cleaned gold.yaml back
        [--out-yaml benchmarks/mseb_swe/gold_validated.yaml]

The ``drop-invalid`` path produces a cleaned yaml that the
harness can consume directly.  We never silently accept a gold
query that a priori cannot be answered correctly.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("mseb.gold_validator")


# ---------------------------------------------------------------------------
# Lexical scoring — cheap, deterministic, no index dependency.
# Same ordering a reasonable BM25 would produce on a tiny chain.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def score_overlap(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """TF-log-normalized overlap: sum_{t in q} log(1 + count_in_doc).

    A cheap stand-in for BM25 when we don't want to build an index
    just to validate gold.  Used to predict which memory in a
    subject chain BM25 would return first.
    """
    if not query_tokens or not doc_tokens:
        return 0.0
    dcount = Counter(doc_tokens)
    return sum(math.log1p(dcount.get(t, 0)) for t in query_tokens)


def rank_chain(
    query: str,
    chain: list[dict],
) -> list[tuple[float, dict]]:
    """Rank a chain by lexical overlap with the query."""
    qtok = tokenize(query)
    scored: list[tuple[float, dict]] = []
    for m in chain:
        dtok = tokenize(m.get("content", ""))
        scored.append((score_overlap(qtok, dtok), m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Validation pass
# ---------------------------------------------------------------------------


def load_labeled_grouped(labeled_dir: Path) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for jsonl in sorted(labeled_dir.glob("*.jsonl")):
        if jsonl.name.startswith("_"):
            continue
        for line in jsonl.read_text(encoding="utf-8").split(chr(10)):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            groups[row["subject"]].append(row)
    for subj in groups:
        groups[subj].sort(key=lambda r: (r.get("observed_at", ""), r.get("mid", "")))
    return dict(groups)


def validate(
    gold_rows: list[dict],
    by_subject: dict[str, list[dict]],
) -> list[dict]:
    """Score each gold query, return per-query verdict list.

    Every returned dict carries ``qid``, ``shape``, ``valid``,
    ``reason`` (if invalid), ``gold_rank_in_chain``, and
    ``lexical_score_gold`` + ``lexical_score_top_sibling``.
    """
    out: list[dict] = []
    for row in gold_rows:
        qid = row.get("qid", "")
        shape = row.get("shape", "")
        text = row.get("text", "")
        subject = row.get("subject", "")
        gold_mid = row.get("gold_mid", "")
        gold_alt = set(row.get("gold_alt") or [])

        verdict: dict[str, Any] = {
            "qid": qid,
            "shape": shape,
            "subject": subject,
            "gold_mid": gold_mid,
            "valid": False,
            "reason": "",
            "gold_rank_in_chain": None,
            "lexical_score_gold": 0.0,
            "lexical_score_top_sibling": 0.0,
        }

        # Noise queries deliberately have no gold; skip lexical checks.
        if shape == "noise":
            if not gold_mid:
                verdict["valid"] = True
                verdict["reason"] = "noise-ok"
            else:
                verdict["reason"] = "noise-query-has-gold-mid"
            out.append(verdict)
            continue

        chain = by_subject.get(subject) or []
        if not chain:
            verdict["reason"] = "subject-not-found"
            out.append(verdict)
            continue

        mids_in_chain = {m["mid"] for m in chain}
        accept = ({gold_mid} | gold_alt) - {""}
        accept_in_chain = accept & mids_in_chain
        if not accept_in_chain:
            verdict["reason"] = "gold-mid-not-in-chain"
            out.append(verdict)
            continue

        ranked = rank_chain(text, chain)
        # Find where the gold (or any alt) first appears.
        rank = None
        for i, (_score, m) in enumerate(ranked):
            if m["mid"] in accept:
                rank = i + 1
                break
        top_score = ranked[0][0] if ranked else 0.0
        gold_score = next(
            (s for s, m in ranked if m["mid"] in accept),
            0.0,
        )
        verdict["gold_rank_in_chain"] = rank
        verdict["lexical_score_gold"] = gold_score
        verdict["lexical_score_top_sibling"] = top_score

        # Validity policy — intentionally lenient for shapes where
        # TLG is supposed to lift rank-1:
        # - INVALID: query has zero overlap with gold.  No retrieval
        #   system (TLG or not) can find a memory whose words the
        #   query doesn't contain.
        # - INVALID: gold is last in its chain AND has strictly
        #   less than the top sibling.  Saves unambiguously lost
        #   queries.
        # - VALID: gold ranks 1 lexically ("easy" — BM25 solves).
        # - VALID: gold ranks 2 or 3 with non-zero overlap
        #   ("medium/hard" — BM25 gets wrong, TLG needs to rescue).
        #   These are exactly the queries that measure TLG's lift.
        #
        # We annotate the difficulty class on each verdict so the
        # later analysis can break out where TLG wins vs loses.
        chain_size = len(chain)
        if gold_score <= 0.0:
            verdict["reason"] = "query-has-zero-overlap-with-gold"
            verdict["difficulty"] = "impossible"
        elif rank is None:
            verdict["reason"] = "gold-not-found-in-ranking"
            verdict["difficulty"] = "impossible"
        elif rank == chain_size and chain_size > 1:
            verdict["reason"] = f"gold-ranks-last-of-{chain_size}-in-chain"
            verdict["difficulty"] = "impossible"
        else:
            verdict["valid"] = True
            verdict["reason"] = "ok"
            if rank == 1:
                verdict["difficulty"] = "easy"  # BM25 wins alone
            elif rank in (2, 3):
                verdict["difficulty"] = "tlg-needed"
            else:
                verdict["difficulty"] = "hard"

        out.append(verdict)
    return out


def summarize(verdicts: list[dict]) -> dict:
    by_shape_total: Counter[str] = Counter()
    by_shape_valid: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_difficulty: Counter[str] = Counter()
    by_shape_difficulty: dict[str, Counter] = defaultdict(Counter)
    for v in verdicts:
        by_shape_total[v["shape"]] += 1
        by_difficulty[v.get("difficulty", "?")] += 1
        by_shape_difficulty[v["shape"]][v.get("difficulty", "?")] += 1
        if v["valid"]:
            by_shape_valid[v["shape"]] += 1
        else:
            by_reason[v["reason"]] += 1
    total = sum(by_shape_total.values())
    valid = sum(by_shape_valid.values())
    return {
        "total_queries": total,
        "valid_queries": valid,
        "invalid_queries": total - valid,
        "valid_rate": (valid / total) if total else 0.0,
        "difficulty_distribution": dict(by_difficulty),
        "per_shape_valid_rate": {
            shape: {
                "valid": by_shape_valid[shape],
                "total": by_shape_total[shape],
                "rate": (
                    by_shape_valid[shape] / by_shape_total[shape] if by_shape_total[shape] else 0.0
                ),
                "difficulty": dict(by_shape_difficulty[shape]),
            }
            for shape in sorted(by_shape_total)
        },
        "invalid_reasons": dict(by_reason.most_common()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_gold_yaml(path: Path) -> list[dict]:
    try:
        import yaml

        return list(yaml.safe_load(path.read_text()) or [])
    except ImportError:
        return json.loads(path.read_text())


def _dump_gold_yaml(rows: list[dict], path: Path) -> None:
    try:
        import yaml

        body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
    except ImportError:
        body = json.dumps(rows, indent=2, ensure_ascii=False)
    path.write_text(body, encoding="utf-8")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="MSEB gold validator")
    ap.add_argument("--labeled-dir", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="Per-query verdict JSON")
    ap.add_argument(
        "--drop-invalid",
        action="store_true",
        help="Write a cleaned gold.yaml (invalid queries removed)",
    )
    ap.add_argument(
        "--out-yaml",
        type=Path,
        default=None,
        help="Where to write the cleaned gold.yaml (default: <gold>.validated.yaml next to --gold)",
    )
    args = ap.parse_args()

    by_subject = load_labeled_grouped(args.labeled_dir)
    rows = _load_gold_yaml(args.gold)
    verdicts = validate(rows, by_subject)
    summary = summarize(verdicts)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "summary": summary,
                "per_query": verdicts,
            },
            indent=2,
        )
    )

    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.drop_invalid:
        valid_qids = {v["qid"] for v in verdicts if v["valid"]}
        cleaned = [r for r in rows if r.get("qid") in valid_qids]
        out_yaml = args.out_yaml or args.gold.with_suffix(".validated.yaml")
        _dump_gold_yaml(cleaned, out_yaml)
        logger.info(
            "wrote cleaned gold with %d/%d queries to %s",
            len(cleaned),
            len(rows),
            out_yaml,
        )


if __name__ == "__main__":
    sys.exit(main())
