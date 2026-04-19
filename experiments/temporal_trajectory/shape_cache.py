"""Query-shape cache — self-improving routing via memoization.

Two self-improvement loops run in the temporal-trajectory system.
This module is the QUERY-SIDE loop; the INGEST-SIDE loop lives
in Layers 1/2/aliases/issue-entities modules that already grow
with every new memory and edge.

### Query-side loop

After a query is successfully parsed (productions resolved to a
specific intent with slots filled), its **skeleton** is extracted
and cached against the resolved intent.  A skeleton is the query
with vocabulary entities replaced by positional placeholders
(``<X>``, ``<Y>``) — so "What came after OAuth?" and "What came
after JWT?" share the skeleton ``what came after <X>``.

When a new query arrives, the cache is consulted first.  A skeleton
hit returns the cached intent immediately, with slots re-extracted
from the actual query tokens.  A miss falls through to the
production list (as before), and whatever production succeeds is
memoized for next time.

### What this buys us

* **Cross-variant robustness.**  The cache grows with every
  successful parse.  Once ``what came after <X>`` is learned, every
  future query fitting that shape routes without re-parsing.
* **Persistable** — the cache serializes to a dict.  In NCMS
  integration this persists across restarts; run-to-run the grammar
  gets faster and broader.
* **Production-induction hook.**  When the cache sees consistent
  (skeleton → intent) mappings from varied queries, it can propose
  NEW productions to surface for human review.  That's the research
  hook for further grammar-structure self-improvement.

### What this does NOT do

Does not rewrite the production regexes or add new seed markers.
Those are English-grammar invariants (how WH-questions are
structured in English) and should not drift with corpus ingest.
The data-layer inductions (vocab, markers, aliases) already cover
the corpus-dependent parts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


_PLACEHOLDERS = ("<X>", "<Y>", "<Z>")
_DETERMINER_RE = re.compile(
    r"\b(?:the|a|an|our|their|its|his|her|my)\s+", re.IGNORECASE,
)


@dataclass
class CachedShape:
    skeleton: str
    intent: str
    slot_names: tuple[str, ...] = ()      # placeholders used, in order
    hit_count: int = 0
    last_used: datetime | None = None

    def touch(self) -> None:
        self.hit_count += 1
        self.last_used = datetime.now()


def _extract_skeleton(query: str) -> tuple[str, dict[str, str]]:
    """Normalize ``query`` into a skeleton with vocabulary entities
    replaced by positional placeholders.

    Returns ``(skeleton, slots)`` where ``slots`` maps placeholder
    names to the canonical entity surface forms found in the query.

    Normalization steps:

    1. Lowercase, strip trailing punctuation.
    2. Strip determiners ("the", "a", "our", "my", …).
    3. Greedy longest-match replacement of Layer 1 vocab tokens
       with ``<X>`` / ``<Y>`` / ``<Z>`` in order of appearance.
    4. Stem remaining content words so "came" / "come" normalize.

    The skeleton is deterministic for a given (query, vocab) pair,
    so lookup-time and learn-time produce identical keys.
    """
    from experiments.temporal_trajectory.vocab_induction import VOCAB, _stem

    q = query.strip().rstrip("?.!,").lower()
    q = _DETERMINER_RE.sub("", q)

    # Collect candidate entity spans (longest-first).
    spans: list[tuple[int, int, str]] = []
    for tok in VOCAB.entity_tokens_ranked:
        pattern = rf"\b{re.escape(tok)}\w*\b"
        for m in re.finditer(pattern, q):
            spans.append((m.start(), m.end(), VOCAB.entity_lookup.get(tok, tok)))

    # Greedy non-overlapping claim, longest-first.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    claimed: list[tuple[int, int, str]] = []
    for start, end, canon in spans:
        if not any(cs <= start < ce or cs < end <= ce for cs, ce, _ in claimed):
            claimed.append((start, end, canon))
    claimed.sort(key=lambda s: s[0])

    # Rebuild with placeholders, stemming intervening text.
    slots: dict[str, str] = {}
    parts: list[str] = []
    cursor = 0
    slot_idx = 0
    for start, end, canon in claimed:
        if slot_idx >= len(_PLACEHOLDERS):
            # Leave further entities as-is (stemmed) — rare.
            break
        segment = q[cursor:start]
        stemmed = " ".join(_stem(w) for w in re.findall(r"\w+", segment))
        if stemmed:
            parts.append(stemmed)
        ph = _PLACEHOLDERS[slot_idx]
        parts.append(ph)
        slots[ph] = canon
        slot_idx += 1
        cursor = end
    tail = q[cursor:]
    tail_stemmed = " ".join(_stem(w) for w in re.findall(r"\w+", tail))
    if tail_stemmed:
        parts.append(tail_stemmed)

    skeleton = " ".join(parts).strip()
    skeleton = re.sub(r"\s+", " ", skeleton)
    return skeleton, slots


class QueryShapeCache:
    """In-process memo of (skeleton → intent) mappings.

    Minimal API:

      * :meth:`lookup(query)` — returns ``(intent, slots)`` if the
        query's skeleton is cached, else ``None``.
      * :meth:`learn(query, intent)` — caches the shape under the
        given intent.  Idempotent — repeats increment ``hit_count``.
      * :meth:`summary()` — introspection for the experiment.

    Persistable via :meth:`to_dict` / :meth:`from_dict`.
    """

    def __init__(self) -> None:
        self._cache: dict[str, CachedShape] = {}

    def lookup(self, query: str) -> tuple[str, dict[str, str]] | None:
        skel, slots = _extract_skeleton(query)
        cached = self._cache.get(skel)
        if cached is None:
            return None
        cached.touch()
        return cached.intent, slots

    def learn(self, query: str, intent: str) -> None:
        if intent in ("none", "abstain"):
            return  # don't cache abstentions — they'd pollute the cache
        skel, slots = _extract_skeleton(query)
        if not skel:
            return
        existing = self._cache.get(skel)
        if existing is None:
            self._cache[skel] = CachedShape(
                skeleton=skel,
                intent=intent,
                slot_names=tuple(slots.keys()),
                hit_count=1,
                last_used=datetime.now(),
            )
        else:
            # Conflict check — if the same skeleton would route to
            # different intents, DON'T cache (keep the current
            # mapping).  This is rare but indicates an actual
            # ambiguity the productions need to resolve.
            if existing.intent == intent:
                existing.touch()

    def summary(self) -> str:
        lines = ["Query-shape cache", "=" * 50]
        if not self._cache:
            lines.append("  (empty)")
            return "\n".join(lines)
        by_intent: dict[str, list[CachedShape]] = {}
        for shape in self._cache.values():
            by_intent.setdefault(shape.intent, []).append(shape)
        for intent, shapes in sorted(by_intent.items()):
            shapes.sort(key=lambda s: -s.hit_count)
            lines.append(f"[{intent}] ({len(shapes)} shapes)")
            for s in shapes[:5]:
                lines.append(f"  {s.hit_count}×  {s.skeleton}")
            if len(shapes) > 5:
                lines.append(f"  … and {len(shapes) - 5} more")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            skel: {
                "intent": sh.intent,
                "slot_names": list(sh.slot_names),
                "hit_count": sh.hit_count,
            }
            for skel, sh in self._cache.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueryShapeCache":
        cache = cls()
        for skel, entry in data.items():
            cache._cache[skel] = CachedShape(
                skeleton=skel,
                intent=entry["intent"],
                slot_names=tuple(entry.get("slot_names", ())),
                hit_count=entry.get("hit_count", 0),
            )
        return cache

    def __len__(self) -> int:
        return len(self._cache)


# Process-global cache instance — productions register hits into
# this.  In NCMS integration this would be backed by the
# ``memory_nodes`` / a dedicated cache table rather than in-memory.
GLOBAL_CACHE = QueryShapeCache()
