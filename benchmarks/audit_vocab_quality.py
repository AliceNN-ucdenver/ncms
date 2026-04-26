"""Phase B: vocabulary quality audit.

For each induced subject in the L1 vocabulary:
  - how many primary tokens route to it?
  - how many ambiguous / secondary tokens?
  - any suspicious tokens (too-generic, too-short, all-stopwords)?
  - are the distinctiveness counters sane?

Also cross-check the entity universe:
  - any UUIDs leaked (post Part 1 fix — should be zero)
  - generic-word entities (GLiNER-style noise) vs domain entities
  - duplicate surface forms that should be aliased

Re-ingests softwaredev mini to get the same state the harness saw,
then dumps a vocabulary report.
"""

from __future__ import annotations

import asyncio
import os
import re

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from collections import Counter
from pathlib import Path

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus

ROOT = Path("/Users/shawnmccarthy/ncms")

GENERIC_WORDS = frozenset(
    {
        # pronouns / determiners / fillers GLiNER often picks up
        "we",
        "it",
        "they",
        "this",
        "that",
        "these",
        "those",
        "our",
        "us",
        "the",
        "a",
        "an",
        # generic nouns
        "decision",
        "decisions",
        "approach",
        "approaches",
        "team",
        "teams",
        "option",
        "options",
        "choice",
        "choices",
        "alternative",
        "alternatives",
        "consideration",
        "considerations",
    }
)


def is_uuid(s: str) -> bool:
    return bool(
        re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            s,
        )
    )


async def main() -> None:
    build = ROOT / "benchmarks/mseb_softwaredev/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    print(f"[vocab] corpus={len(corpus)}", flush=True)

    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain="software_dev",
    )
    await backend.setup()
    try:
        await backend.ingest(corpus)
        svc = backend._svc
        store = svc._store

        # Fetch the full induced vocabulary
        ctx = await svc._tlg_vocab_cache.get_parser_context(store)
        vocab = ctx.vocabulary
        aliases = ctx.aliases
        domain_nouns = ctx.domain_nouns

        print()
        print("=" * 86)
        print("  VOCABULARY SUMMARY")
        print("=" * 86)
        subjects = sorted(set(vocab.subject_lookup.values()))
        print(f"  subjects: {len(subjects)}")
        print(f"  entity tokens: {len(vocab.entity_lookup)}")
        print(f"  aliases (surface→group): {len(aliases)}")
        print(f"  domain_nouns: {len(domain_nouns)}")

        # ── UUID smell test ─────────────────────────────────────────
        uuid_subjects = [s for s in subjects if is_uuid(s)]
        uuid_entities = [e for e in vocab.entity_lookup.values() if is_uuid(e)]
        print(
            f"\n  UUID leakage: subjects={len(uuid_subjects)}, "
            f"entities={len(uuid_entities)}  (expected 0 post-Part 1)"
        )

        # ── Generic-word entities ───────────────────────────────────
        generic_ents = sorted(e for e in vocab.entity_lookup.values() if e.lower() in GENERIC_WORDS)
        print(f"\n  Generic-word entities ({len(generic_ents)}):  {generic_ents[:12]}")

        # ── Subject distinctiveness ────────────────────────────────
        print()
        print("=" * 86)
        print("  PER-SUBJECT TOKEN ROUTING")
        print("=" * 86)
        by_subject: dict[str, list[str]] = {}
        for token, subj in vocab.subject_lookup.items():
            by_subject.setdefault(subj, []).append(token)
        for subj, tokens in sorted(by_subject.items(), key=lambda x: -len(x[1])):
            primary = [t for t in tokens if t in vocab.primary_tokens]
            secondary = [t for t in tokens if t not in vocab.primary_tokens]
            print(f"\n  subject: {subj}")
            print(
                f"    primary tokens   ({len(primary):3d}): "
                f"{sorted(primary, key=len, reverse=True)[:6]}"
            )
            print(
                f"    secondary tokens ({len(secondary):3d}): "
                f"{sorted(secondary, key=len, reverse=True)[:6]}"
            )
            # Short-and-suspicious tokens
            short = [t for t in tokens if len(t) <= 3]
            if short:
                print(f"    WARN: short tokens (len≤3): {short[:8]}")

        # ── Ambiguous tokens (appear in >1 subject) ────────────────
        ambiguous = [
            (tok, counts) for tok, counts in vocab.distinctiveness.items() if len(counts) > 1
        ]
        print()
        print("=" * 86)
        print("  AMBIGUOUS TOKENS (route to >1 subject):")
        print("=" * 86)
        print(
            f"  total: {len(ambiguous)} of {len(vocab.subject_lookup)} "
            f"tokens ({len(ambiguous) / max(1, len(vocab.subject_lookup)) * 100:.1f}%)"
        )
        # Worst offenders — high degree (many subjects) + short tokens
        ambiguous_sorted = sorted(
            ambiguous,
            key=lambda x: (-len(x[1]), len(x[0])),
        )
        print("\n  Top 20 most ambiguous tokens (more subjects = worse):")
        for tok, counts in ambiguous_sorted[:20]:
            winner = counts.most_common(1)[0][0]
            total = sum(counts.values())
            print(
                f"    token={tok!r:24}  "
                f"routes_to={winner!r:45}  "
                f"n_subjects={len(counts):2d}  total_mentions={total}"
            )

        # ── Entity type distribution ────────────────────────────────
        print()
        print("=" * 86)
        print("  ENTITY TYPES (how GLiNER + slot-head labeled things)")
        print("=" * 86)
        # Pull all entity records with types
        all_entities = await store.list_entities()
        type_counts = Counter(e.type for e in all_entities)
        print(f"  total entity rows: {len(all_entities)}")
        for t, n in type_counts.most_common():
            print(f"    {t!r:25}  {n:5d}")

        # Sample suspicious generic entities
        suspect = [e for e in all_entities if e.name.lower() in GENERIC_WORDS][:15]
        print("\n  Generic-word entity rows (first 15):")
        for e in suspect:
            attrs = e.attributes or {}
            src = attrs.get("source", "(unset)")
            print(f"    name={e.name!r:30}  type={e.type!r:18}  source={src!r}")

    finally:
        await backend.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
