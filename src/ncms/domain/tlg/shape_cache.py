"""Query-shape cache — self-improving routing via memoization.

Port of ``experiments/temporal_trajectory/shape_cache.py``.  The
pure skeleton extraction lives here in the domain layer; the
persistent store-backed cache lives in
:class:`ncms.application.tlg.shape_cache_store` (next commit) and
persists skeletons into the ``grammar_shape_cache`` table that
schema v12 already created.

**Skeleton extraction** — turns a query into a deterministic
shape string with vocabulary entities replaced by positional
placeholders (``<X>``, ``<Y>``, ``<Z>``).  ``"What came after
OAuth?"`` and ``"What came after JWT?"`` share the skeleton
``what came after <X>``.

**The cache** — minimal memo: skeleton → (intent, slot names).
Lookups return the resolved intent without re-running all 12
productions.  Learn emits ``(skeleton, intent)`` pairs after a
successful parse.  Conflicts (same skeleton, different intents)
refuse to cache — the productions remain the authority.

What this does NOT do: rewrite productions or mint new seed
markers.  Those are English-grammar invariants.  Only the
vocabulary, aliases, and skeleton memo grow with the corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ncms.domain.tlg.vocabulary import InducedVocabulary, _stem

_PLACEHOLDERS: tuple[str, ...] = ("<X>", "<Y>", "<Z>")
_DETERMINER_RE = re.compile(
    r"\b(?:the|a|an|our|their|its|his|her|my)\s+",
    re.IGNORECASE,
)


@dataclass
class CachedShape:
    """In-memory record of one memoised query shape."""

    skeleton: str
    intent: str
    slot_names: tuple[str, ...] = field(default_factory=tuple)
    hit_count: int = 0
    last_used: datetime | None = None

    def touch(self) -> None:
        self.hit_count += 1
        self.last_used = datetime.now(UTC)


def extract_skeleton(
    query: str, vocabulary: InducedVocabulary,
) -> tuple[str, dict[str, str]]:
    """Normalize ``query`` into a placeholder skeleton + slot map.

    Steps:

    1. Lowercase + strip trailing punctuation.
    2. Strip leading determiners so ``"the OAuth"`` and ``"OAuth"``
       produce the same shape.
    3. Greedy longest-match replacement of vocab entity tokens with
       ``<X>`` / ``<Y>`` / ``<Z>`` in order of appearance.
    4. Stem intervening content words so morphological variants
       (``came`` / ``come``) collapse.

    Deterministic for a fixed ``(query, vocabulary)`` pair.
    """
    q = query.strip().rstrip("?.!,").lower()
    q = _DETERMINER_RE.sub("", q)

    # Candidate entity spans, longest-first claim.
    spans: list[tuple[int, int, str]] = []
    for token in vocabulary.entity_tokens_ranked:
        pattern = rf"\b{re.escape(token)}\w*\b"
        for m in re.finditer(pattern, q):
            spans.append((
                m.start(), m.end(),
                vocabulary.entity_lookup.get(token, token),
            ))

    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    claimed: list[tuple[int, int, str]] = []
    for start, end, canon in spans:
        if not any(
            cs <= start < ce or cs < end <= ce
            for cs, ce, _ in claimed
        ):
            claimed.append((start, end, canon))
    claimed.sort(key=lambda s: s[0])

    # Rebuild.
    slots: dict[str, str] = {}
    parts: list[str] = []
    cursor = 0
    for slot_idx, (start, end, canon) in enumerate(claimed):
        if slot_idx >= len(_PLACEHOLDERS):
            break
        segment = q[cursor:start]
        stemmed = " ".join(_stem(w) for w in re.findall(r"\w+", segment))
        if stemmed:
            parts.append(stemmed)
        placeholder = _PLACEHOLDERS[slot_idx]
        parts.append(placeholder)
        slots[placeholder] = canon
        cursor = end
    tail = q[cursor:]
    tail_stemmed = " ".join(_stem(w) for w in re.findall(r"\w+", tail))
    if tail_stemmed:
        parts.append(tail_stemmed)
    skeleton = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return skeleton, slots


class QueryShapeCache:
    """In-memory memo of ``skeleton → (intent, slot_names)``.

    Thread-safety: NCMS is asyncio-single-threaded on the hot path;
    a plain dict is sufficient.  The persistent table-backed cache
    (in ``application/tlg/shape_cache_store``) serialises across
    restarts.
    """

    def __init__(self) -> None:
        self._cache: dict[str, CachedShape] = {}

    def __len__(self) -> int:
        return len(self._cache)

    def lookup(
        self, query: str, vocabulary: InducedVocabulary,
    ) -> tuple[str, dict[str, str]] | None:
        skel, slots = extract_skeleton(query, vocabulary)
        cached = self._cache.get(skel)
        if cached is None:
            return None
        cached.touch()
        return cached.intent, slots

    def learn(
        self, query: str, intent: str, vocabulary: InducedVocabulary,
    ) -> None:
        """Memoise ``(skeleton → intent)`` from a successful parse.

        * Skips ``intent`` values in ``{"none", "abstain"}`` —
          polluting the cache with abstentions would misroute
          future queries.
        * On conflict (same skeleton, different intent), keeps the
          existing mapping.  Productions remain the authority for
          ambiguous shapes.
        """
        if intent in ("none", "abstain"):
            return
        skel, slots = extract_skeleton(query, vocabulary)
        if not skel:
            return
        existing = self._cache.get(skel)
        if existing is None:
            self._cache[skel] = CachedShape(
                skeleton=skel,
                intent=intent,
                slot_names=tuple(slots.keys()),
                hit_count=1,
                last_used=datetime.now(UTC),
            )
        elif existing.intent == intent:
            existing.touch()

    def snapshot(self) -> dict[str, dict]:
        """Dict-serializable snapshot for persistence."""
        return {
            skel: {
                "intent": sh.intent,
                "slot_names": list(sh.slot_names),
                "hit_count": sh.hit_count,
                "last_used": (
                    sh.last_used.isoformat() if sh.last_used else None
                ),
            }
            for skel, sh in self._cache.items()
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, dict]) -> QueryShapeCache:
        cache = cls()
        for skel, entry in data.items():
            last_used_str = entry.get("last_used")
            last_used = (
                datetime.fromisoformat(last_used_str)
                if last_used_str else None
            )
            cache._cache[skel] = CachedShape(
                skeleton=skel,
                intent=entry["intent"],
                slot_names=tuple(entry.get("slot_names", ())),
                hit_count=entry.get("hit_count", 0),
                last_used=last_used,
            )
        return cache

    def items(self):
        """Yield ``CachedShape`` records ordered by ``hit_count`` desc."""
        return sorted(self._cache.values(), key=lambda s: -s.hit_count)
