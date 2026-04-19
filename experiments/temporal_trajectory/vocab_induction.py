"""Corpus-derived vocabulary induction.

Replaces the hand-maintained subject/entity dicts in
``lg_retriever.py`` with tables built programmatically from the
corpus at load time.  The whole philosophy:

* **Every entity mentioned in a memory of subject S is, by default,
  a token that should route queries to S.**  No manual list.
* **Ambiguous tokens** (appearing in multiple subjects' memories)
  are resolved by preferring the subject with the most occurrences.
* **Single-token form** (lowercase, whitespace-normalized) is used
  so "Physical Therapy" and "physical therapy" both match.

This makes the classifier generalize automatically: add a new
subject to the corpus and queries using its entity vocabulary route
correctly, no hand-edit required.

Level 2 and 3 (edge-derived transition markers, parse-based intent)
are sketched in the diary but not implemented here.  The experiment
validates Level 1 alone is sufficient to restore LG to 18-20/20
rank-1 accuracy across multiple domains.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from experiments.temporal_trajectory.corpus import ADR_CORPUS, EDGES


@dataclass(frozen=True)
class InducedVocabulary:
    """Lookup tables derived from the corpus."""

    # lowercase token → subject (most-frequent wins ties)
    subject_lookup: dict[str, str]

    # lowercase token → canonical entity name (original casing)
    entity_lookup: dict[str, str]

    # Sorted tokens longest-first, so "physical therapy" beats "therapy".
    subject_tokens_ranked: list[str]
    entity_tokens_ranked: list[str]

    # Primary tokens = exact entity matches (not word splits).  Used
    # by disambiguation to prefer "roadmap" (a full entity) over
    # "project" (a word split from "payments project").
    primary_tokens: frozenset[str]


def induce_vocabulary() -> InducedVocabulary:
    """Build subject and entity lookup tables from the corpus.

    Induces from every memory entity, and ALSO from each word in
    multi-word entities (so 'arthroscopic surgery' yields both the
    full phrase and 'surgery' and 'arthroscopic' as individual
    tokens, each pointing to the same subject).  This makes single-
    word queries match multi-word entities without hand-listing.

    Short words (≤2 chars) and pure-digit tokens are skipped — no
    informational value.
    """
    subject_counts: dict[str, Counter[str]] = {}
    entity_canon: dict[str, str] = {}
    primary: set[str] = set()

    def _register(
        token: str, orig: str, subject: str, is_primary: bool,
    ) -> None:
        tok = token.strip().lower()
        orig = orig.strip()
        if tok.isdigit():
            return
        # Accept 2-char tokens ONLY when they're all-uppercase in the
        # original — these are abbreviations ("PT", "UI", "QA") that
        # carry real signal.  Skip 2-char lowercase words ("on", "it",
        # "is") which are noise.  3+ char tokens always pass.
        if len(tok) < 2:
            return
        if len(tok) == 2 and not orig.isupper():
            return
        subject_counts.setdefault(tok, Counter())[subject] += 1
        entity_canon.setdefault(tok, orig)
        if is_primary:
            primary.add(tok)

    for mem in ADR_CORPUS:
        if mem.subject is None:
            continue
        for ent in mem.entities:
            # Full phrase — primary.
            _register(ent, ent, mem.subject, is_primary=True)
            # Individual words in multi-word entities — secondary.
            for word in ent.strip().split():
                if word.lower() != ent.strip().lower():
                    _register(word, word, mem.subject, is_primary=False)

    subject_lookup: dict[str, str] = {}
    for token, counts in subject_counts.items():
        subject_lookup[token] = counts.most_common(1)[0][0]

    subject_tokens_ranked = sorted(
        subject_lookup.keys(), key=len, reverse=True,
    )
    entity_tokens_ranked = sorted(
        entity_canon.keys(), key=len, reverse=True,
    )

    return InducedVocabulary(
        subject_lookup=subject_lookup,
        entity_lookup=entity_canon,
        subject_tokens_ranked=subject_tokens_ranked,
        entity_tokens_ranked=entity_tokens_ranked,
        primary_tokens=frozenset(primary),
    )


# Module-level cache — the corpus is static for the experiment.
VOCAB = induce_vocabulary()


def lookup_subject(query: str) -> str | None:
    """Scan the query for entity tokens and return the matched
    subject, preferring distinctive tokens over shared/generic ones.

    A token is "distinctive" when it appears in only one subject.
    Longest-match alone is wrong: "project" appears in multiple
    subjects' entities via token-splitting, so longest-match picks
    the multi-word phrase containing "project" (e.g., "payments
    project").  But if the query also mentions "roadmap" (which is
    unique to identity_project), that should win.

    Algorithm:
      1. Collect all matching tokens in the query (case-insensitive,
         word-boundary).
      2. For each match, compute distinctiveness = 1 / (# subjects
         this token could route to).  Distinct tokens score 1.0;
         tokens shared across N subjects score 1/N.
      3. Pick the token with the highest (distinctiveness, length)
         — distinctive wins ties by length.
    """
    q = query.lower()
    matches: list[tuple[str, str, float, int, bool]] = []
    dist_counter: dict[str, "Counter[str]"] = _distinctiveness_counter()
    for token in VOCAB.subject_tokens_ranked:
        if not _token_in_query(token, q):
            continue
        counts = dist_counter.get(token, Counter())
        n_subjects = len(counts)
        distinctiveness = 1.0 / max(n_subjects, 1)
        is_primary = token in VOCAB.primary_tokens
        matches.append((
            token, VOCAB.subject_lookup[token],
            distinctiveness, len(token), is_primary,
        ))
    if not matches:
        return None
    # Prefer: primary over split-derived, then higher distinctiveness,
    # then longer token.  Primary = the token was an exact entity in
    # the corpus; split-derived = it was a word inside a longer entity.
    matches.sort(
        key=lambda m: (m[4], m[2], m[3]),
        reverse=True,
    )
    return matches[0][1]


def _distinctiveness_counter() -> dict[str, "Counter[str]"]:
    """Rebuild the (token → {subject: count}) counter that induction
    threw away.  Small overhead; called rarely from lookup_subject.

    Mirrors ``_register``'s filter: allows 2-char all-caps
    abbreviations, drops 2-char lowercase noise, allows 3+ always.
    """
    from collections import Counter

    def _acceptable(tok_lower: str, orig: str) -> bool:
        if tok_lower.isdigit():
            return False
        if len(tok_lower) < 2:
            return False
        if len(tok_lower) == 2 and not orig.isupper():
            return False
        return True

    out: dict[str, Counter[str]] = {}
    for mem in ADR_CORPUS:
        if mem.subject is None:
            continue
        for ent in mem.entities:
            tok = ent.strip().lower()
            if _acceptable(tok, ent.strip()):
                out.setdefault(tok, Counter())[mem.subject] += 1
            for word in ent.strip().split():
                wlow = word.strip().lower()
                if _acceptable(wlow, word.strip()) and wlow != tok:
                    out.setdefault(wlow, Counter())[mem.subject] += 1
    return out


def lookup_entity(query: str) -> str | None:
    """Scan the query for the longest matching entity token; return
    its canonical form."""
    q = query.lower()
    for token in VOCAB.entity_tokens_ranked:
        if _token_in_query(token, q):
            return VOCAB.entity_lookup[token]
    return None


import re as _re

import snowballstemmer

_STEMMER = snowballstemmer.stemmer("english")


def _stem(word: str) -> str:
    return _STEMMER.stemWord(word.lower())


def _token_in_query(token: str, query_lower: str) -> bool:
    """Word-boundary match with Snowball-stemmer normalization.

    ``token`` may be a phrase; we stem word-by-word on both sides
    and compare stem-sequences.  Handles morphology uniformly:
    ``authenticate`` and ``authentication`` both stem to ``authent``,
    so either surface form matches.  Replaces the earlier prefix-
    match kludge.
    """
    # Exact word-boundary match is the cheapest check — try it first.
    pattern = r"\b" + _re.escape(token) + r"\b"
    if _re.search(pattern, query_lower) is not None:
        return True
    # Stem-based match: break both into word lists, stem each, compare.
    token_stems = [_stem(w) for w in token.split() if w]
    if not token_stems:
        return False
    query_words = _re.findall(r"\w+", query_lower)
    query_stems = [_stem(w) for w in query_words]
    # Look for the token's stem sequence as a contiguous subsequence
    # of the query's stems.
    if not query_stems:
        return False
    k = len(token_stems)
    for i in range(len(query_stems) - k + 1):
        if query_stems[i:i + k] == token_stems:
            return True
    return False


# ── Introspection: useful for the experiment's write-up ─────────────

def summary() -> str:
    """Human-readable summary of what the induction produced."""
    lines: list[str] = []
    lines.append("Corpus-derived vocabulary")
    lines.append("=" * 60)
    lines.append(f"Total entity tokens:  {len(VOCAB.entity_lookup)}")
    lines.append(f"Total subject tokens: {len(VOCAB.subject_lookup)}")
    # Group subject-lookup by subject for a clean table.
    by_subject: dict[str, list[str]] = {}
    for token, subj in VOCAB.subject_lookup.items():
        by_subject.setdefault(subj, []).append(token)
    lines.append("")
    for subj, tokens in sorted(by_subject.items()):
        lines.append(f"[{subj}] ({len(tokens)} tokens)")
        for t in sorted(tokens, key=len, reverse=True)[:10]:
            lines.append(f"    {t}")
        if len(tokens) > 10:
            lines.append(f"    … and {len(tokens) - 10} more")
    return "\n".join(lines)
