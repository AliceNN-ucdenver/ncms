"""v9 Phase B'.0b: catalog coverage audit against benchmark corpora.

Measures how well our per-domain gazetteer catalogs cover real
text from the MSEB benchmark datasets.  Outputs:

  1. Per-domain **catalog hit rate**: % of rows where
     ``detect_spans`` finds at least one catalog surface.
  2. Per-domain **surface coverage**: the top N noun phrases that
     appear ≥ ``--min-mentions`` times in the corpus but are NOT
     in the catalog.  These are the concrete backfill targets.
  3. **Per-slot distribution** of detected surfaces — shows
     whether any catalog slot is under-represented.

Usage::

    uv run python scripts/v9/catalog_coverage.py
    uv run python scripts/v9/catalog_coverage.py --domain software_dev
    uv run python scripts/v9/catalog_coverage.py \\
        --domain clinical --min-mentions 3 --top-n 100

The missing-surface list is produced by a simple heuristic:
lowercase + strip-punctuation + tokenise, then keep contiguous
n-gram windows (1-3 words) that:

  * contain at least one noun-like token (capitalised in source,
    OR longer than 4 chars, OR matches a domain-specific hint
    regex)
  * appear ≥ ``--min-mentions`` times
  * are NOT already in the catalog (canonical or alias)

The heuristic is deliberately loose — it's a recall-oriented
surfacing tool, not a precision-oriented NER.  Human review
(me, the builder) decides which of the surfaced candidates are
real entities worth adding to the catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.application.adapters.sdg.catalog.normalize import (  # noqa: E402
    detect_spans,
    lookup,
)
from ncms.application.adapters.schemas import Domain  # noqa: E402


#: Per-domain benchmark corpus paths (JSONL with ``{"content": ...}``).
_BENCHMARK_CORPORA: dict[Domain, list[Path]] = {
    "software_dev": [
        _REPO / "benchmarks/mseb_softwaredev/build_mini/corpus.jsonl",
        _REPO / "benchmarks/mseb_softwaredev/build_tlg_stress/corpus.jsonl",
    ],
    "clinical": [
        _REPO / "benchmarks/mseb_clinical/build_mini/corpus.jsonl",
        _REPO / "benchmarks/mseb_clinical/build_tlg_stress/corpus.jsonl",
    ],
    "conversational": [
        _REPO / "benchmarks/mseb_convo/build_mini/corpus.jsonl",
    ],
}


@dataclass
class CoverageReport:
    domain: Domain
    n_rows: int
    n_rows_with_hit: int
    n_total_surfaces: int
    per_slot_hits: Counter[str]
    missing_top_ngrams: list[tuple[str, int]]  # (ngram, count), sorted desc
    zero_hit_samples: list[str]  # Sampled rows with no catalog hits

    @property
    def hit_rate(self) -> float:
        return self.n_rows_with_hit / self.n_rows if self.n_rows else 0.0

    def render_markdown(self) -> str:
        lines: list[str] = []
        w = lines.append
        w(f"### Domain: `{self.domain}`")
        w("")
        w(
            f"- Rows audited: **{self.n_rows}**"
        )
        w(
            f"- Rows with ≥1 catalog hit: **{self.n_rows_with_hit} "
            f"({self.hit_rate:.1%})**"
        )
        w(
            f"- Total detected surfaces: **{self.n_total_surfaces}**"
        )
        w("")
        w("**Per-slot surfaces detected**:")
        w("")
        for slot, n in self.per_slot_hits.most_common():
            w(f"- `{slot}`: {n}")
        w("")
        w("**Top candidate backfill targets** (noun-like n-grams "
          "appearing ≥N times, NOT in catalog):")
        w("")
        w("| rank | n-gram | count |")
        w("|---:|---|---:|")
        for i, (ng, count) in enumerate(self.missing_top_ngrams, 1):
            w(f"| {i} | `{ng}` | {count} |")
        w("")
        if self.zero_hit_samples:
            w("**Sample rows with ZERO catalog hits** (direct human review):")
            w("")
            for i, sample in enumerate(self.zero_hit_samples, 1):
                snippet = sample[:280].replace("\n", " ")
                w(f"{i}. `{snippet}{'...' if len(sample) > 280 else ''}`")
            w("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Missing-surface mining
# ---------------------------------------------------------------------------


# Crude token + n-gram heuristic.  Keeps candidates with at least one
# noun-like token (Capitalised OR longer than 4 chars).  Noun-like
# isn't semantic; it's a cheap filter so we don't surface "the of
# and" as a candidate n-gram.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on",
    "at", "by", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did", "will", "would",
    "should", "could", "may", "might", "must", "can", "this", "that",
    "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "my", "your", "his", "her", "its", "our", "their", "me", "him",
    "us", "them", "what", "which", "who", "whom", "whose", "where",
    "when", "why", "how", "if", "while", "as", "with", "from", "into",
    "after", "before", "during", "over", "under", "about", "through",
    "because", "since", "not", "no", "yes", "so", "but", "also",
    "just", "only", "very", "really", "much", "more", "most", "many",
    "some", "any", "all", "each", "every", "one", "two", "three",
    "there", "here", "than", "then", "now", "again", "out", "up",
    "down", "off", "like", "want", "wants", "need", "needs", "get",
    "gets", "got", "go", "goes", "going", "going", "know", "think",
    "use", "uses", "using", "used", "make", "makes", "made", "see",
    "sees", "saw", "seen", "find", "finds", "found", "help", "please",
    "okay", "ok", "yeah", "well", "thanks", "thank",
    "looks", "looking", "look", "say", "says", "said", "tell", "told",
    "try", "tries", "tried", "let", "lets", "give", "gives", "gave",
    "new", "old", "same", "different", "other", "another",
    "good", "bad", "great", "better", "best", "worse", "worst",
    "high", "low", "long", "short", "big", "small", "large",
    "sure", "certain", "definitely", "probably", "maybe", "possibly",
})

# Abstract nouns common in ADR / technical prose that are NOT
# entities — dropping these from candidate n-grams cuts noise by >10x.
_ABSTRACT_NOUN_STOPWORDS: frozenset[str] = frozenset({
    # ADR / decision prose
    "decision", "decisions", "decided", "decide", "choose", "chose",
    "chosen", "consideration", "considered", "considering",
    "option", "options", "approach", "approaches", "alternatives",
    "solution", "solutions", "strategy", "strategies", "plan", "plans",
    "goal", "goals", "objective", "objectives", "context", "status",
    "result", "results", "outcome", "outcomes", "implementation",
    "implementations", "deployment", "deployments",
    # Generic software concepts
    "support", "supports", "supported", "feature", "features",
    "features.", "functionality", "capability", "capabilities",
    "performance", "scalability", "security", "reliability",
    "availability", "compatibility", "flexibility", "extensibility",
    "maintainability", "usability", "quality", "complexity",
    "development", "developer", "developers", "engineer",
    "engineers", "engineering", "programming", "programmer",
    "programmers", "software", "hardware", "product", "products",
    "project", "projects", "component", "components", "module",
    "modules", "service", "services", "application", "applications",
    "system", "systems", "tool", "tools", "library", "libraries",
    "framework", "frameworks", "language", "languages", "platform",
    "platforms", "database", "databases", "interface", "interfaces",
    "api", "apis", "code", "function", "functions", "method",
    "methods", "class", "classes", "object", "objects",
    "version", "versions", "release", "releases", "update", "updates",
    "change", "changes", "changelog", "update.", "improvement",
    "improvements", "advantage", "advantages", "disadvantage",
    "disadvantages", "benefit", "benefits", "drawback", "drawbacks",
    "pros", "cons", "issue", "issues", "problem", "problems",
    "challenge", "challenges", "concern", "concerns",
    "team", "teams", "user", "users", "customer", "customers",
    "company", "companies", "organization", "organizations",
    "community", "communities", "ecosystem", "ecosystems",
    "environment", "environments", "production", "staging", "dev",
    "testing", "tests", "test", "integration", "integrations",
    "time", "times", "cost", "costs", "price", "prices", "value",
    "values", "size", "sizes", "number", "numbers", "example",
    "examples", "case", "cases", "type", "types", "kind", "kinds",
    "data", "information", "content", "contents", "document",
    "documents", "documentation", "docs", "doc", "readme", "readmes",
    "section", "sections", "chapter", "chapters", "title", "titles",
    "name", "names", "label", "labels", "description", "descriptions",
    "overview", "summary", "introduction", "conclusion",
    "experience", "knowledge", "skill", "skills", "practice",
    "practices", "standard", "standards", "pattern", "patterns",
    "design", "designs", "architecture", "architectures",
    "structure", "structures", "process", "processes", "processing",
    "workflow", "workflows", "pipeline", "pipelines",
    "technology", "technologies", "tech", "techs",
    "way", "ways", "step", "steps", "stage", "stages", "phase",
    "phases", "level", "levels", "degree", "degrees",
    "amount", "amounts", "range", "ranges", "period", "periods",
    "part", "parts", "aspect", "aspects", "area", "areas",
    "field", "fields", "topic", "topics", "subject", "subjects",
    # ADR section headers
    "status", "context", "consequences", "considered", "positive",
    "negative", "neutral", "background", "overview",
    # Benchmark boilerplate
    "h1", "h2", "h3", "h4", "h5", "h6", "etc", "etc.", "i.e.", "e.g.",
    "related", "including", "includes", "included",
    "provides", "provide", "provided", "providing",
    "working", "works", "worked", "work",
    "learning", "learned", "learns", "learn",
    "building", "builds", "built", "build",
    "managing", "manages", "managed", "manage",
    "running", "runs", "ran", "run",
    "developing", "develops", "developed", "develop",
    "creating", "creates", "created", "create",
    "existing", "exists", "existed", "exist",
    "using", "uses", "used", "use",
    "modern", "traditional", "classic", "legacy",
    "available", "unavailable", "accessible", "compatible",
    "efficient", "effective", "robust", "reliable",
    "important", "essential", "key", "main", "primary", "core",
    "specific", "general", "common", "popular", "standard",
    "current", "previous", "latest", "recent", "final", "initial",
})


def _is_abstract_noun(tok: str) -> bool:
    return tok.lower() in _ABSTRACT_NOUN_STOPWORDS


_TECH_SIGNAL_RE = re.compile(
    r"^[A-Z][a-z]+[A-Z]"  # CamelCase (PostgreSQL, RedisCache)
    r"|^[A-Z]{2,5}(?:$|[-_.0-9])"  # Acronym prefix (AWS, GCP, K8s)
    r"|[a-z][A-Z]"  # camelCase mid-word (kubernetes / openShift)
    r"|[0-9]"  # Contains digit (Python3, C++17, PostgreSQL15)
    r"|[.\-_+#]"  # Technical separator (fly.io, gRPC, C#, node-fetch)
)


def _is_tech_like(tok: str) -> bool:
    """Strong signal that a token is a technology name / identifier.

    Catches: PostgreSQL, k8s, C++, .NET, fly.io, gRPC, Python3,
    OAuth2, AWS, GCP, node-fetch, etc.

    Misses: plain lowercase multi-syllable names like 'kubernetes'
    or 'redis' — those are caught by the ``>4 chars + not stopword
    + not abstract-noun`` fallback.
    """
    return bool(_TECH_SIGNAL_RE.search(tok))


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#\-_./]*")


def _tokenise(text: str) -> list[str]:
    tokens = []
    for m in _WORD_RE.finditer(text):
        tok = m.group(0)
        # Strip trailing punctuation — regex allows ``.`` + ``/`` in
        # the middle (for "fly.io", "c/c++"), but a trailing period
        # or slash is end-of-sentence noise.
        tok = tok.rstrip("./-_+#")
        if tok:
            tokens.append(tok)
    return tokens


def _is_entity_like(tok: str) -> bool:
    """Strict filter: is this token plausibly an *entity name*?

    An entity is a proper noun referring to a specific thing
    (tool / framework / product / medication / etc).  Common
    abstract nouns ("support", "performance", "decision") are NOT
    entities even though they appear frequently — those are
    filtered by ``_is_abstract_noun``.

    Rules:
      - Stopword? → no
      - Abstract-noun stopword? → no
      - Tech-like signal (CamelCase, acronym, digit, `.-_+#`) → yes
      - Mid-sentence capitalisation (not first token, starts upper) → yes
      - Everything else → no  (stricter than the old _is_noun_like)
    """
    if not tok:
        return False
    tl = tok.lower()
    if tl in _STOPWORDS or tl in _ABSTRACT_NOUN_STOPWORDS:
        return False
    if _is_tech_like(tok):
        return True
    # Uppercase-starting bare word — could be a proper noun, but this
    # fires a lot of false positives on sentence starters.  Require
    # length >= 5 to cut "The" / "This" / "That" etc.
    if tok[0].isupper() and len(tok) >= 5:
        return True
    return False


def _extract_candidate_ngrams(
    text: str, max_n: int = 3,
) -> list[str]:
    """Yield 1..max_n-gram candidates that pass the entity-like filter.

    For n=1: token must pass ``_is_entity_like``.
    For n>1: at least one token must be entity-like, AND boundary
    tokens must not be stopwords or abstract nouns.
    """
    tokens = _tokenise(text)
    out: list[str] = []
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            window = tokens[i : i + n]
            first_tl = window[0].lower()
            last_tl = window[-1].lower()
            if first_tl in _STOPWORDS or first_tl in _ABSTRACT_NOUN_STOPWORDS:
                continue
            if last_tl in _STOPWORDS or last_tl in _ABSTRACT_NOUN_STOPWORDS:
                continue
            if not any(_is_entity_like(t) for t in window):
                continue
            ng = " ".join(window)
            out.append(ng.lower())
    return out


def _load_corpus_contents(paths: list[Path]) -> list[str]:
    """Read the ``content`` field from every JSONL row across paths."""
    contents: list[str] = []
    for p in paths:
        if not p.is_file():
            continue
        with p.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = row.get("content")
                if isinstance(text, str) and text:
                    contents.append(text)
    return contents


def audit_domain(
    domain: Domain,
    *,
    min_mentions: int = 3,
    top_n: int = 50,
    max_ngram: int = 3,
    zero_hit_sample_n: int = 10,
    zero_hit_seed: int = 42,
) -> CoverageReport:
    """Audit one domain's catalog against the benchmark corpus."""
    import random as _random

    paths = _BENCHMARK_CORPORA.get(domain, [])
    if not paths:
        raise ValueError(f"no benchmark corpora registered for {domain!r}")
    contents = _load_corpus_contents(paths)
    if not contents:
        raise RuntimeError(
            f"no rows found in any of {paths} for domain {domain!r}",
        )

    per_slot_hits: Counter[str] = Counter()
    n_rows_with_hit = 0
    n_total_surfaces = 0
    zero_hit_rows: list[str] = []

    # N-gram frequency over the whole corpus.
    ngram_counts: Counter[str] = Counter()

    for text in contents:
        spans = detect_spans(text, domain=domain)
        if spans:
            n_rows_with_hit += 1
            n_total_surfaces += len(spans)
            for sp in spans:
                per_slot_hits[sp.slot] += 1
        else:
            zero_hit_rows.append(text)
        for ng in _extract_candidate_ngrams(text, max_n=max_ngram):
            ngram_counts[ng] += 1

    # Filter out n-grams that ARE in the catalog (any canonical or
    # alias match in any slot).
    missing: list[tuple[str, int]] = []
    for ng, count in ngram_counts.items():
        if count < min_mentions:
            continue
        if lookup(ng, domain=domain) is not None:
            continue
        # Also skip if any token in the n-gram is itself catalog'd
        # — the catalog already covers a subsumed surface.  This
        # trims "postgres database" when we already have "postgres".
        tokens = ng.split()
        if len(tokens) > 1 and any(
            lookup(t, domain=domain) is not None for t in tokens
        ):
            continue
        missing.append((ng, count))

    missing.sort(key=lambda t: (-t[1], t[0]))

    # Stratified sample of zero-hit rows for direct judge-review.
    rng = _random.Random(zero_hit_seed)
    samples = (
        rng.sample(zero_hit_rows, zero_hit_sample_n)
        if len(zero_hit_rows) >= zero_hit_sample_n else zero_hit_rows
    )

    return CoverageReport(
        domain=domain,
        n_rows=len(contents),
        n_rows_with_hit=n_rows_with_hit,
        n_total_surfaces=n_total_surfaces,
        per_slot_hits=per_slot_hits,
        missing_top_ngrams=missing[:top_n],
        zero_hit_samples=samples,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--domain", choices=["conversational", "clinical", "software_dev"],
        default=None,
        help="Audit a single domain (default: all three).",
    )
    p.add_argument(
        "--min-mentions", type=int, default=3,
        help="Only report candidate n-grams with ≥ this many mentions.",
    )
    p.add_argument(
        "--top-n", type=int, default=50,
        help="Number of top candidate backfill targets to print.",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Optional path to write the full report as markdown "
             "(default: stdout).",
    )
    args = p.parse_args()

    domains: list[Domain] = (
        [args.domain] if args.domain
        else ["software_dev", "conversational", "clinical"]
    )

    buf: list[str] = ["# v9 catalog coverage audit", ""]
    buf.append(
        "Generated by `scripts/v9/catalog_coverage.py` against the "
        "MSEB benchmark datasets.  The 'hit rate' is the fraction "
        "of benchmark rows where `catalog.detect_spans` finds at "
        "least one surface.  The 'top candidate backfill targets' "
        "are noun-like n-grams with ≥ `--min-mentions` mentions "
        "that are NOT yet in the catalog."
    )
    buf.append("")
    buf.append(f"- min-mentions: **{args.min_mentions}**")
    buf.append(f"- top-n: **{args.top_n}**")
    buf.append("")

    for dom in domains:
        report = audit_domain(
            dom, min_mentions=args.min_mentions, top_n=args.top_n,
        )
        buf.append(report.render_markdown())
        # Also print a terse stdout line per domain.
        print(
            f"  {dom:16}  rows={report.n_rows:5}  "
            f"hit_rate={report.hit_rate:5.1%}  "
            f"surfaces={report.n_total_surfaces:4}  "
            f"missing_candidates={len(report.missing_top_ngrams)}",
        )

    md = "\n".join(buf)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md)
        print(f"\nFull report written to {args.output}")
    else:
        print()
        print(md)


if __name__ == "__main__":
    main()
