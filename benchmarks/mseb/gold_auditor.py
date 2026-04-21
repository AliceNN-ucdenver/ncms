"""Strict gold auditor — checks each query against per-class rules.

Rule table (see docs/mseb-gold-rules.md for the prose version):

+-------------+--------------------------------------------------------------+
| class       | rule                                                         |
+=============+==============================================================+
| general     | Query contains >=1 token that is in the gold memory's content|
|             | and NOT in any of its chain-siblings' content.  (Anchor is   |
|             | chain-unique.)                                               |
+-------------+--------------------------------------------------------------+
| temporal    | (a) NCMS ``parse_temporal_reference`` fires on the query,    |
|             |     OR the shape is in the explicit temporal family, AND     |
|             | (b) the general rule (chain-unique anchor) holds too.         |
+-------------+--------------------------------------------------------------+
| preference  | Query text contains a preference marker (love / avoid /      |
|             | every / struggle / prefer / used to / switched / favourite), |
|             | AND the gold memory's metadata.preference != "none".          |
+-------------+--------------------------------------------------------------+
| noise       | Query's vocabulary has <=2 token overlap with ANY memory in  |
|             | the corpus (adversarial — should not match anything).        |
+-------------+--------------------------------------------------------------+

Every query receives a ``passes_rule`` verdict + a ``failure_reason`` when
it fails.  The audit does NOT modify the gold file — it produces a JSON
report of which queries are trustworthy per class, so authoring only
touches what the audit says is broken.

Usage::

    uv run python -m benchmarks.mseb.gold_auditor \\
        --labeled-dir benchmarks/mseb_swe/raw_labeled \\
        --gold benchmarks/mseb_swe/gold.yaml \\
        --out benchmarks/mseb_swe/gold_audit.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("mseb.gold_auditor")


# ---------------------------------------------------------------------------
# Tokeniser shared with validator
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "to", "for", "with", "at", "by",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "can", "may",
    "what", "when", "where", "who", "which", "how", "why", "that", "this",
    "these", "those", "and", "or", "but", "not", "no", "if", "then", "so",
    "also", "than", "as", "from", "into", "about", "any", "some", "all",
    "each", "every", "most", "more", "less", "much", "many", "few", "other",
    "new", "old", "same", "different", "first", "last", "final", "initial",
    "current", "latest", "earliest", "previous", "next", "before", "after",
    "during", "while",
    # Common verbs in questions
    "report", "reported", "describe", "described", "discuss", "discussed",
    "show", "tell", "find", "trace", "order", "reach", "make", "made",
    "take", "took", "give", "gave",
    # Pronouns
    "it", "its", "they", "them", "their", "he", "she", "his", "her", "we",
    "our", "you", "your", "us", "i", "me", "my",
    # Possessives
    "users", "user", "patient", "patients",
})


def tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOPWORDS and len(t) > 2
    }


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


_TEMPORAL_VOCAB = re.compile(
    r"(?i)\b(first|last|earliest|latest|recent|before|after|prior|"
    r"next|previous|final|initial|current|original|in\s+\d{4}|since\s+\w+|"
    r"ago|yesterday|today|tomorrow|order|sequence|trace|when|"
    r"last\s+(?:week|month|year)|in\s+(?:january|february|march|april|may|"
    r"june|july|august|september|october|november|december))\b",
)

_TEMPORAL_SHAPES = frozenset({
    "ordinal_first", "ordinal_last", "sequence", "predecessor",
    "before_named",
})

_PREFERENCE_VOCAB = re.compile(
    r"(?i)\b(prefer|preference|love|hate|like|dislike|enjoy|avoid|"
    r"struggle|difficult|hard|easy|favou?rite|go[- ]?to|every\s+\w+|"
    r"usually|always|never|used\s+to|switched|moved|gave\s+up|"
    r"can(?:'|no)?t\s+(?:eat|have|handle|do|use)|allergic|"
    r"my\s+(?:routine|habit|practice)|tend\s+to)\b",
)


def load_chains(labeled_dir: Path) -> dict[str, list[dict]]:
    chains: dict[str, list[dict]] = defaultdict(list)
    for jsonl in sorted(labeled_dir.glob("*.jsonl")):
        if jsonl.name.startswith("_"):
            continue
        for line in jsonl.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            chains[row["subject"]].append(row)
    for s in chains:
        chains[s].sort(key=lambda r: (
            r.get("observed_at", ""), r.get("mid", ""),
        ))
    return dict(chains)


def compute_distinctive_terms(
    chains: dict[str, list[dict]], max_df_ratio: float = 0.05,
) -> set[str]:
    """Return the set of corpus-distinctive tokens (named entities /
    technical identifiers), meant to catch on-topic noise queries.

    A token qualifies when ALL hold:

    - it is not a stopword,
    - it appears in at least 2 memories (not a typo),
    - its document frequency is below ``max_df_ratio`` of all memories
      (rare = content-specific),
    - AND either contains a capital letter in its ORIGINAL form
      (proper noun — ``Django``, ``Sjögren``, ``FastAPI``) OR is at
      least 8 characters long (technical identifier — ``separability``,
      ``amlodipine``, ``Kubernetes``).

    Common English words like ``long``, ``best``, ``deep``, ``wrote``
    — even if they happen to be low-DF in a given corpus — are
    excluded by the capital/length filter.
    """
    doc_count = 0
    df: Counter[str] = Counter()
    # Track original casing so we can detect proper nouns.
    original_case: dict[str, set[str]] = defaultdict(set)

    for _subject, mems in chains.items():
        for m in mems:
            doc_count += 1
            raw = m.get("content", "") or ""
            # Walk the ORIGINAL casing; lowercase for DF keying.
            for raw_tok in _TOKEN_RE.findall(raw):
                low = raw_tok.lower()
                if low in _STOPWORDS or len(low) <= 2:
                    continue
                original_case[low].add(raw_tok)
            for t in tokens(raw):
                df[t] += 1

    if doc_count == 0:
        return set()
    cutoff = max(2, int(doc_count * max_df_ratio))

    out: set[str] = set()
    for t, c in df.items():
        if not (2 <= c <= cutoff):
            continue
        forms = original_case.get(t, set())
        has_capital = any(
            any(ch.isupper() for ch in f) for f in forms
        )
        long_enough = len(t) >= 8
        if has_capital or long_enough:
            out.add(t)
    return out


def _temporal_parse_hits(text: str) -> bool:
    """Whether NCMS's temporal parser fires on the query."""
    try:
        from ncms.domain.temporal.parser import parse_temporal_reference
        ref = parse_temporal_reference(text, now=datetime.now(UTC))
        if ref is None:
            return False
        return bool(ref.ordinal or ref.recency_bias or ref.range_start)
    except Exception:  # pragma: no cover
        return False


def check_general(
    q: dict, chain: list[dict], corpus_tokens: set[str],
) -> tuple[bool, str]:
    """Strict general-class rule: query has >=1 token in the gold
    memory's content and NOT in any chain sibling.

    Used for domains where each subject has a small, clearly-
    differentiable chain (SWE: 3-4 memories; Clinical: 5-20 sections;
    SoftwareDev: 5-10 ADR sections).  See :func:`check_general_tf_lift`
    for the relaxed variant used on high-turn conversational chains.
    """
    gold_mid = q.get("gold_mid", "")
    gold = next((m for m in chain if m["mid"] == gold_mid), None)
    if gold is None:
        return False, "gold-mid-not-in-chain"
    q_tok = tokens(q.get("text", ""))
    if not q_tok:
        return False, "query-has-no-tokens"
    gold_tok = tokens(gold.get("content", ""))
    sibling_tok: set[str] = set()
    for m in chain:
        if m["mid"] == gold_mid:
            continue
        sibling_tok |= tokens(m.get("content", ""))
    # A token is a "chain-unique anchor" if it's in gold, not in siblings,
    # AND present in the query.
    anchors = q_tok & gold_tok - sibling_tok
    if not anchors:
        return False, "no-chain-unique-anchor"
    return True, ""


def _token_multiset(text: str) -> Counter[str]:
    """Full term-frequency counter (tokens module-level tokenizer
    filters stopwords)."""
    return Counter(
        t.lower() for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOPWORDS and len(t) > 2
    )


def check_general_tf_lift(
    q: dict, chain: list[dict], corpus_tokens: set[str],
    *, min_lift: float = 0.0,
) -> tuple[bool, str]:
    """Relaxed general-class rule using TF-lift rather than absolute
    chain-uniqueness.

    For high-turn conversational chains (Convo: 20-200+ turns per
    user), tokens in gold frequently appear in siblings too —
    different turns naturally share vocabulary ("I", "today",
    "work", "weekend").  Under the strict rule nothing passes.

    The relaxed rule: the query's tokens must have HIGHER density
    in the gold memory than in its siblings' average.  Specifically
    ``sum(TF_gold(t) - mean(TF_siblings(t))) > min_lift`` for
    t in query tokens.  Positive TF-lift means retrieval can pick
    gold out of the chain on pure lexical grounds.
    """
    gold_mid = q.get("gold_mid", "")
    gold = next((m for m in chain if m["mid"] == gold_mid), None)
    if gold is None:
        return False, "gold-mid-not-in-chain"
    q_tok = tokens(q.get("text", ""))
    if not q_tok:
        return False, "query-has-no-tokens"

    gold_tf = _token_multiset(gold.get("content", ""))
    gold_len = max(sum(gold_tf.values()), 1)
    sibling_tfs: list[Counter[str]] = []
    sibling_lens: list[int] = []
    for m in chain:
        if m["mid"] == gold_mid:
            continue
        tf = _token_multiset(m.get("content", ""))
        sibling_tfs.append(tf)
        sibling_lens.append(max(sum(tf.values()), 1))

    lift = 0.0
    has_any_overlap = False
    for t in q_tok:
        gold_rate = gold_tf.get(t, 0) / gold_len
        if not sibling_tfs:
            sibling_rate = 0.0
        else:
            sibling_rate = sum(
                tf.get(t, 0) / n
                for tf, n in zip(sibling_tfs, sibling_lens, strict=False)
            ) / len(sibling_tfs)
        if gold_rate > 0:
            has_any_overlap = True
        lift += gold_rate - sibling_rate

    if not has_any_overlap:
        return False, "tf-lift-zero-overlap-with-gold"
    if lift <= min_lift:
        return False, f"tf-lift-non-positive ({lift:.4f})"
    return True, ""


def check_temporal(
    q: dict, chain: list[dict], corpus_tokens: set[str],
    *, use_tf_lift: bool = False,
) -> tuple[bool, str]:
    """(a) Temporal vocab OR temporal shape family, AND
    (b) general rule (strict or TF-lift variant)."""
    text = q.get("text", "")
    has_cue = bool(_TEMPORAL_VOCAB.search(text)) or _temporal_parse_hits(text)
    in_shape_fam = q.get("shape", "") in _TEMPORAL_SHAPES
    if not (has_cue or in_shape_fam):
        return False, "no-temporal-vocabulary"
    base = check_general_tf_lift if use_tf_lift else check_general
    ok, reason = base(q, chain, corpus_tokens)
    if not ok:
        return False, f"temporal-{reason}"
    return True, ""


def check_preference(
    q: dict, chain: list[dict], corpus_tokens: set[str],
    *, use_tf_lift: bool = False,
) -> tuple[bool, str]:
    """Query has preference vocab AND gold memory has a pref kind
    AND the chain-anchor rule (strict or TF-lift) holds."""
    text = q.get("text", "")
    has_vocab = bool(_PREFERENCE_VOCAB.search(text))
    if not has_vocab:
        return False, "no-preference-vocabulary"
    gold_mid = q.get("gold_mid", "")
    gold = next((m for m in chain if m["mid"] == gold_mid), None)
    if gold is None:
        return False, "gold-mid-not-in-chain"
    pref_kind = gold.get("metadata", {}).get("preference", "none")
    if pref_kind == "none":
        return False, "gold-memory-has-no-preference-kind"
    base = check_general_tf_lift if use_tf_lift else check_general
    ok, reason = base(q, chain, corpus_tokens)
    if not ok:
        return False, f"preference-{reason}"
    return True, ""


def check_noise(
    q: dict,
    chain: list[dict],
    corpus_tokens: set[str],
    *,
    distinctive_terms: set[str] | None = None,
) -> tuple[bool, str]:
    """Noise: query must not contain any corpus-distinctive term.

    Updated rule (v2): common English tokens no longer count.  A
    noise query fails only if it references a term specific to the
    corpus (named entities, technical identifiers, medical terms —
    things with low document frequency).  ``distinctive_terms`` is
    computed once by :func:`compute_distinctive_terms` and threaded
    through the caller.
    """
    q_tok = tokens(q.get("text", ""))
    if q.get("gold_mid"):
        return False, "noise-query-must-have-empty-gold_mid"
    if distinctive_terms is None:
        # Fallback: no distinctive terms pre-computed → accept
        # (audit without distinctive-set still runs; lower fidelity).
        return True, ""
    hits = q_tok & distinctive_terms
    if hits:
        sample = sorted(hits)[:3]
        return False, f"noise-contains-corpus-term ({sample})"
    return True, ""


RULE_CHECKS = {
    "general":    check_general,
    "temporal":   check_temporal,
    "preference": check_preference,
    "noise":      check_noise,
}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit(
    gold_rows: list[dict], chains: dict[str, list[dict]],
    *, rule_set: str = "strict",
) -> dict:
    # Corpus-wide token pool for anchor checks.
    corpus_tokens: set[str] = set()
    for s, mems in chains.items():
        for m in mems:
            corpus_tokens |= tokens(m.get("content", ""))
    # Distinctive-term set for the noise check (v2 rule).
    distinctive = compute_distinctive_terms(chains)

    verdicts: list[dict] = []
    per_class_pass: Counter[str] = Counter()
    per_class_total: Counter[str] = Counter()
    per_class_failure: dict[str, Counter] = defaultdict(Counter)

    use_tf_lift = (rule_set == "tf-lift")
    for row in gold_rows:
        cls = row.get("query_class", "general")
        per_class_total[cls] += 1
        chain = chains.get(row.get("subject", "")) or []
        if cls == "noise":
            ok, reason = check_noise(
                row, chain, corpus_tokens,
                distinctive_terms=distinctive,
            )
        elif cls == "general":
            fn = check_general_tf_lift if use_tf_lift else check_general
            ok, reason = fn(row, chain, corpus_tokens)
        elif cls == "temporal":
            ok, reason = check_temporal(
                row, chain, corpus_tokens, use_tf_lift=use_tf_lift,
            )
        elif cls == "preference":
            ok, reason = check_preference(
                row, chain, corpus_tokens, use_tf_lift=use_tf_lift,
            )
        else:
            ok, reason = check_general(row, chain, corpus_tokens)
        if ok:
            per_class_pass[cls] += 1
        else:
            per_class_failure[cls][reason] += 1
        verdicts.append({
            "qid": row.get("qid", ""),
            "query_class": cls,
            "shape": row.get("shape", ""),
            "passes_rule": ok,
            "failure_reason": reason,
        })

    summary = {
        "total": sum(per_class_total.values()),
        "passed": sum(per_class_pass.values()),
        "pass_rate": (
            sum(per_class_pass.values()) / max(sum(per_class_total.values()), 1)
        ),
        "per_class": {
            cls: {
                "total": per_class_total[cls],
                "passed": per_class_pass[cls],
                "rate": (
                    per_class_pass[cls] / per_class_total[cls]
                    if per_class_total[cls] else 0.0
                ),
                "failures": dict(per_class_failure[cls].most_common()),
            }
            for cls in sorted(per_class_total)
        },
    }
    return {"summary": summary, "per_query": verdicts}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--labeled-dir", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--emit-passing", type=Path, default=None,
                    help="Optional: write a gold.yaml containing only passing "
                         "queries — the lock-ready subset.")
    ap.add_argument("--rule-set", choices=["strict", "tf-lift"], default="strict",
                    help="strict = chain-unique-anchor (default, for SWE / "
                         "Clinical / SoftwareDev); tf-lift = relative density "
                         "(for Convo and other high-turn chains).")
    args = ap.parse_args()

    try:
        import yaml
        rows = yaml.safe_load(args.gold.read_text(encoding="utf-8")) or []
    except ImportError:
        rows = json.loads(args.gold.read_text(encoding="utf-8"))
    chains = load_chains(args.labeled_dir)

    report = audit(rows, chains, rule_set=args.rule_set)
    args.out.write_text(json.dumps(report, indent=2))

    if args.emit_passing:
        passing_qids = {
            v["qid"] for v in report["per_query"] if v["passes_rule"]
        }
        passing_rows = [r for r in rows if r.get("qid") in passing_qids]
        try:
            import yaml
            body = yaml.safe_dump(
                passing_rows, sort_keys=False, allow_unicode=True,
            )
            args.emit_passing.write_text(
                "# Audited gold — only queries that pass the per-class rule survived.\n"
                "# See benchmarks/mseb/gold_auditor.py for the rule table.\n\n"
                + body,
                encoding="utf-8",
            )
        except ImportError:
            args.emit_passing.write_text(
                json.dumps(passing_rows, indent=2, ensure_ascii=False),
            )

    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    sys.exit(main())
