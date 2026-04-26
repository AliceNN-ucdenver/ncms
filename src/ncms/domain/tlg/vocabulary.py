"""L1 — Subject-vocabulary induction (pure).

Port of ``experiments/temporal_trajectory/vocab_induction.py`` adapted
for NCMS: the corpus is passed as an argument (a list of
:class:`SubjectMemory` records) instead of reading a global
``ADR_CORPUS``.  Application-layer code (``application/tlg/induction``)
is responsible for composing the input from the MemoryStore — this
module is stateless and has no infrastructure dependencies.

The induction philosophy from the research code is preserved intact:

* Every entity mentioned in a memory of subject S becomes a token
  that routes queries to S.  No hand-maintained dictionary.
* Ambiguous tokens (appearing in multiple subjects) resolve to the
  subject with the most mentions.
* Individual words of multi-word entities are registered as secondary
  tokens so ``"physical therapy"`` surfaces tokens ``"physical"``
  and ``"therapy"`` in addition to the full phrase.
* Length and content filters: ≤2-char lowercase tokens and pure-digit
  tokens are dropped; 2-char all-caps (``"PT"``, ``"UI"``, ``"QA"``)
  stay because they carry signal.
* :func:`lookup_subject` resolves a query by scoring matched tokens on
  ``(primary-ness, distinctiveness, length)`` — distinctiveness is the
  reciprocal of the token's subject-ambiguity.

See ``docs/temporal-linguistic-geometry.md`` §4 for the theory.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

import snowballstemmer

_STEMMER = snowballstemmer.stemmer("english")


# ---------------------------------------------------------------------------
# Inputs + outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectMemory:
    """A single memory tagged with the subject it pertains to.

    The caller picks the subject mapping.  For NCMS the natural choice
    is the ``entity_id`` of the entity-state root: every memory that
    records a state for entity E is a :class:`SubjectMemory` with
    ``subject = E``.
    """

    subject: str
    entities: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class InducedVocabulary:
    """Lookup tables produced by :func:`induce_vocabulary`."""

    # lowercase token → subject (most frequent wins)
    subject_lookup: dict[str, str]

    # lowercase token → canonical (original-casing) entity name
    entity_lookup: dict[str, str]

    # Tokens ordered longest-first for greedy matching.
    subject_tokens_ranked: list[str]
    entity_tokens_ranked: list[str]

    # Tokens that were registered as *exact* entity names (not word
    # splits).  Primary tokens win ties against split-derived tokens
    # in :func:`lookup_subject`.
    primary_tokens: frozenset[str]

    # (token → {subject: count}).  Captured so :func:`lookup_subject`
    # can reason about ambiguity without re-scanning the corpus.
    distinctiveness: dict[str, Counter[str]]

    # Inverted indexes keyed by the stem of each word in the token.
    # Turns :func:`lookup_subject` / :func:`lookup_entity` from
    # O(|vocab|) token iteration into O(|query_words|) lookups + a
    # small candidate set.  Values are tokens (the same strings that
    # appear in ``*_tokens_ranked``).
    subject_stem_index: dict[str, list[str]] = field(default_factory=dict)
    entity_stem_index: dict[str, list[str]] = field(default_factory=dict)

    # Precomputed token → {stem} frozensets.  Saves re-stemming every
    # candidate during :func:`_candidate_tokens` subset-intersection.
    # Keyed by the same strings used in ``*_tokens_ranked``.
    subject_token_stems: dict[str, frozenset[str]] = field(default_factory=dict)
    entity_token_stems: dict[str, frozenset[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Induction
# ---------------------------------------------------------------------------


def _token_acceptable(tok_lower: str, orig: str) -> bool:
    """Filter: drop digits; drop <2-char tokens; drop 2-char lowercase.

    2-char all-caps tokens stay (``PT``, ``UI``, ``QA``) — they're
    abbreviations that carry real signal.
    """
    if not tok_lower or tok_lower.isdigit():
        return False
    if len(tok_lower) < 2:
        return False
    # 2-char lowercase words ("on", "it") are noise; 2-char all-caps
    # abbreviations ("PT", "UI") carry signal.
    return not (len(tok_lower) == 2 and not orig.isupper())


def induce_vocabulary(memories: Iterable[SubjectMemory]) -> InducedVocabulary:
    """Build subject + entity lookup tables from a corpus of memories.

    Returns an :class:`InducedVocabulary` ready for use with
    :func:`lookup_subject` and :func:`lookup_entity`.  Runs in
    O(|memories| * |entities per memory|); pure function, no side
    effects.
    """
    subject_counts: dict[str, Counter[str]] = {}
    entity_canon: dict[str, str] = {}
    primary: set[str] = set()

    def _register(
        token: str,
        orig: str,
        subject: str,
        *,
        is_primary: bool,
    ) -> None:
        tok = token.strip().lower()
        orig = orig.strip()
        if not _token_acceptable(tok, orig):
            return
        subject_counts.setdefault(tok, Counter())[subject] += 1
        entity_canon.setdefault(tok, orig)
        if is_primary:
            primary.add(tok)

    for mem in memories:
        if not mem.subject:
            continue
        for ent in mem.entities:
            _register(ent, ent, mem.subject, is_primary=True)
            # Split multi-word entities into secondary tokens so
            # "physical therapy" surfaces "physical" + "therapy" both
            # pointing to the same subject.
            stripped = ent.strip()
            stripped_lower = stripped.lower()
            for word in stripped.split():
                if word.lower() != stripped_lower:
                    _register(word, word, mem.subject, is_primary=False)

    subject_lookup: dict[str, str] = {
        token: counts.most_common(1)[0][0] for token, counts in subject_counts.items()
    }

    subject_tokens_ranked = sorted(
        subject_lookup.keys(),
        key=len,
        reverse=True,
    )
    entity_tokens_ranked = sorted(
        entity_canon.keys(),
        key=len,
        reverse=True,
    )

    # Stem-keyed inverted indexes + precomputed per-token stem sets.
    # The index maps stem → candidate tokens; the stem set on each
    # token lets the filter stage test subset membership against
    # the query's stem set in O(1) without re-stemming.
    subject_stem_index: dict[str, list[str]] = {}
    subject_token_stems: dict[str, frozenset[str]] = {}
    for token in subject_tokens_ranked:
        stems = {_stem(word) for word in re.findall(r"\w+", token) if word}
        stems.discard("")
        subject_token_stems[token] = frozenset(stems)
        for stem in stems:
            subject_stem_index.setdefault(stem, []).append(token)

    entity_stem_index: dict[str, list[str]] = {}
    entity_token_stems: dict[str, frozenset[str]] = {}
    for token in entity_tokens_ranked:
        stems = {_stem(word) for word in re.findall(r"\w+", token) if word}
        stems.discard("")
        entity_token_stems[token] = frozenset(stems)
        for stem in stems:
            entity_stem_index.setdefault(stem, []).append(token)

    return InducedVocabulary(
        subject_lookup=subject_lookup,
        entity_lookup=entity_canon,
        subject_tokens_ranked=subject_tokens_ranked,
        entity_tokens_ranked=entity_tokens_ranked,
        primary_tokens=frozenset(primary),
        distinctiveness=subject_counts,
        subject_stem_index=subject_stem_index,
        entity_stem_index=entity_stem_index,
        subject_token_stems=subject_token_stems,
        entity_token_stems=entity_token_stems,
    )


# ---------------------------------------------------------------------------
# Token matching — shared between subject + entity lookup
# ---------------------------------------------------------------------------


def _stem(word: str) -> str:
    return _STEMMER.stemWord(word.lower())


def _token_in_query(token: str, query_lower: str) -> bool:
    """Word-boundary match with Snowball-stemmer fallback.

    ``token`` may be a phrase: we stem word-by-word on both sides and
    compare stem sequences.  Matches ``authenticate`` against
    ``authentication`` because both stem to ``authent``.
    """
    pattern = r"\b" + re.escape(token) + r"\b"
    if re.search(pattern, query_lower) is not None:
        return True
    token_stems = [_stem(w) for w in token.split() if w]
    if not token_stems:
        return False
    query_stems = [_stem(w) for w in re.findall(r"\w+", query_lower)]
    if not query_stems:
        return False
    window = len(token_stems)
    for i in range(len(query_stems) - window + 1):
        if query_stems[i : i + window] == token_stems:
            return True
    return False


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------


def _candidate_tokens(
    query_lower: str,
    stem_index: dict[str, list[str]],
    token_stems: dict[str, frozenset[str]],
) -> list[str]:
    """Return vocab tokens whose stem inventory is a subset of the
    query's stems.

    Uses the precomputed ``token_stems`` map so the filter stage
    skips re-stemming on every candidate — membership check is
    a single O(1) subset test.
    """
    query_stems: set[str] = set()
    for word in re.findall(r"\w+", query_lower):
        stem = _stem(word)
        if stem:
            query_stems.add(stem)
    if not query_stems:
        return []

    seeds: set[str] = set()
    for stem in query_stems:
        seeds.update(stem_index.get(stem, ()))

    candidates = [
        token for token in seeds if token_stems.get(token, frozenset()).issubset(query_stems)
    ]
    candidates.sort(key=len, reverse=True)
    return candidates


def lookup_subject(
    query: str,
    vocab: InducedVocabulary,
) -> str | None:
    """Return the subject most strongly implied by ``query``, or None.

    Scoring — applied in order, highest-rank wins:

    1. **primary-ness** (the token was registered as an exact entity
       match, not a word split)
    2. **distinctiveness** (1 / |subjects the token could route to|)
    3. **length** (longer tokens carry more signal)
    """
    q = query.lower()
    candidates = _candidate_tokens(
        q,
        vocab.subject_stem_index,
        vocab.subject_token_stems,
    )
    matches: list[tuple[bool, float, int, str]] = []
    for token in candidates:
        if not _token_in_query(token, q):
            continue
        counts = vocab.distinctiveness.get(token, Counter())
        n_subjects = max(len(counts), 1)
        distinctiveness = 1.0 / n_subjects
        is_primary = token in vocab.primary_tokens
        matches.append((is_primary, distinctiveness, len(token), vocab.subject_lookup[token]))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][3]


def lookup_entity(
    query: str,
    vocab: InducedVocabulary,
) -> str | None:
    """Return the canonical form of the longest matching entity, or None.

    Uses the stem-keyed candidate set to avoid iterating every token
    in the vocabulary — falls back to longest-token order when two
    candidates both match.
    """
    q = query.lower()
    candidates = _candidate_tokens(
        q,
        vocab.entity_stem_index,
        vocab.entity_token_stems,
    )
    longest_hit: str | None = None
    longest_len = -1
    for token in candidates:
        if not _token_in_query(token, q):
            continue
        if len(token) > longest_len:
            longest_len = len(token)
            longest_hit = token
    if longest_hit is None:
        return None
    return vocab.entity_lookup[longest_hit]


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def summary(vocab: InducedVocabulary) -> str:
    """Human-readable dump of what induction produced."""
    lines = [
        "Induced vocabulary",
        "=" * 60,
        f"Entity tokens:  {len(vocab.entity_lookup)}",
        f"Subject tokens: {len(vocab.subject_lookup)}",
        "",
    ]
    by_subject: dict[str, list[str]] = {}
    for token, subj in vocab.subject_lookup.items():
        by_subject.setdefault(subj, []).append(token)
    for subj, tokens in sorted(by_subject.items()):
        lines.append(f"[{subj}] ({len(tokens)} tokens)")
        for t in sorted(tokens, key=len, reverse=True)[:10]:
            lines.append(f"    {t}")
        if len(tokens) > 10:
            lines.append(f"    … and {len(tokens) - 10} more")
    return "\n".join(lines)
