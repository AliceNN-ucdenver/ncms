"""Phase D: TLG grammar walker path correctness.

For a set of target qids, reconstruct the FULL walker state per
query.  We want to answer:

  - Did subject resolution pick the gold subject?
  - How big is the subject's zone?  Which memories anchor it?
  - Which edge sequence did the walker traverse?
  - Does the returned grammar_answer correspond to a real
    state-evolution answer, or is it just the first/last thing
    in the zone?

Runs after ingest on softwaredev mini.  For each qid:
  1. Classify with SLM.
  2. Resolve subject via vocabulary_cache.
  3. Load the subject's zones (using dispatch._load_subject_zones).
  4. Walk via retrieve_lg — capture every intermediate step via
     proof_steps (if available) or by instrumenting manually.
  5. Compare answer to gold_mid, gold shape.

Also report per-subject zone cardinality: for each induced subject,
how many ENTITY_STATE nodes anchor it + edge count.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from collections import Counter
from pathlib import Path

import yaml

from benchmarks.mseb.backends.ncms_backend import NcmsBackend
from benchmarks.mseb.harness import FeatureSet
from benchmarks.mseb.schema import load_corpus

ROOT = Path("/Users/shawnmccarthy/ncms")


async def main() -> None:
    build = ROOT / "benchmarks/mseb_softwaredev/build_mini"
    corpus = load_corpus(build / "corpus.jsonl")
    queries = yaml.safe_load((ROOT / "benchmarks/mseb_softwaredev/gold_locked.yaml").read_text())
    gold_by_qid = {g["qid"]: g for g in queries}

    backend = NcmsBackend(
        feature_set=FeatureSet(temporal=True, slm=True),
        adapter_domain="software_dev",
    )
    await backend.setup()
    try:
        mid_map = await backend.ingest(corpus)
        # Reverse: memory_id -> mid (for answer translation)
        id_to_mid: dict[str, str] = {v: k for k, v in mid_map.items()}

        svc = backend._svc
        store = svc._store

        # ── Per-subject zone cardinality ────────────────────────────
        from ncms.application.tlg.dispatch import _load_subject_zones

        ctx = await svc._tlg_vocab_cache.get_parser_context(store)
        subjects = sorted(set(ctx.vocabulary.subject_lookup.values()))
        print()
        print("=" * 86)
        print("  PER-SUBJECT ZONE CARDINALITY")
        print("=" * 86)
        print(f"  {'subject':60s} {'zones':>6s} {'nodes':>6s} {'edges':>6s}")
        for subject in subjects:
            try:
                zones, node_index, edges = await _load_subject_zones(
                    store,
                    subject,
                )
            except Exception as exc:
                print(f"  {subject:60s} ERROR: {exc!r}")
                continue
            print(f"  {subject:60s} {len(zones):6d} {len(node_index):6d} {len(edges):6d}")

        # ── Per-query grammar trace ─────────────────────────────────
        print()
        print("=" * 86)
        print("  PER-QUERY GRAMMAR TRACE")
        print("=" * 86)

        # Pick one qid per shape for broad coverage, plus some
        # regression cases from the audit.
        target_qids = [
            "softwaredev-current_state-001",
            "softwaredev-current_state-005",
            "softwaredev-origin-001",
            "softwaredev-origin-005",
            "softwaredev-ordinal_first-001",
            "softwaredev-ordinal_last-001",
            "softwaredev-retirement-001",
            "softwaredev-predecessor-001",
            "softwaredev-sequence-001",
            "softwaredev-causal_chain-001",
            "softwaredev-concurrent-001",
            "softwaredev-before_named-002",
            "softwaredev-transitive_cause-001",
        ]

        grammar_hit = Counter()  # conf → hit count

        for qid in target_qids:
            g = gold_by_qid.get(qid)
            if g is None:
                continue
            qtext = g["text"]
            gold_mid = g["gold_mid"]
            gold_shape = g["shape"]
            gold_subject = g.get("subject", "")

            # Classify with SLM
            head = backend.classify_query(qtext)
            slm_shape = head.get("shape_intent")

            # Resolve subject + entity from the parser context
            from ncms.domain.tlg import lookup_entity, lookup_subject

            resolved_subject = lookup_subject(qtext, ctx.vocabulary)
            resolved_entity = lookup_entity(qtext, ctx.vocabulary)

            # Run retrieve_lg
            trace = await svc.retrieve_lg(
                qtext,
            )
            conf = trace.confidence.value if trace.confidence else None
            grammar_hit[conf] += 1

            # Map grammar_answer (memory_id) back to mid
            grammar_mid = None
            if trace.grammar_answer:
                gmem = await store.get_memory(trace.grammar_answer)
                if gmem is not None:
                    for tag in gmem.tags or []:
                        if tag.startswith("mid:"):
                            grammar_mid = tag.split(":", 1)[1]
                            break

            hit = grammar_mid == gold_mid
            print()
            print(f"  qid={qid}  shape={gold_shape}")
            print(f"    query: {qtext[:120]}")
            print(f"    gold subject: {gold_subject}")
            print(f"    gold_mid:     {gold_mid}")
            _si_conf = head.get("shape_intent_confidence")
            _si_conf_str = f"{_si_conf:.3f}" if _si_conf is not None else "None"
            print(f"    SLM shape_intent: {slm_shape!r} (conf {_si_conf_str})")
            print(f"    Resolved subject (vocab): {resolved_subject!r}")
            print(f"    Resolved entity  (vocab): {resolved_entity!r}")
            print(f"    Grammar intent:   {trace.intent.kind if trace.intent else '-'}")
            print(f"    Grammar subject:  {trace.intent.subject if trace.intent else '-'}")
            print(f"    Grammar entity:   {trace.intent.entity if trace.intent else '-'}")
            print(f"    Grammar conf:     {conf}")
            print(f"    Grammar answer:   {grammar_mid!r}  {'✓' if hit else '✗'}")
            print(f"    Proof:            {trace.proof}")

            # Compare subject resolution to gold
            if resolved_subject != gold_subject and gold_subject:
                print(
                    f"    SUBJECT MISMATCH: resolved={resolved_subject!r} vs gold={gold_subject!r}"
                )

            # If resolved subject matches, but grammar answer doesn't,
            # inspect the zone
            if resolved_subject == gold_subject and not hit:
                try:
                    zones, node_index, edges = await _load_subject_zones(
                        store,
                        gold_subject,
                    )
                    print(
                        f"    ZONE DEBUG: {len(zones)} zones, "
                        f"{len(node_index)} nodes, {len(edges)} edges"
                    )
                    for zi, zone in enumerate(zones[:3]):
                        tt = zone.terminal_mid
                        rt = zone.root_mid
                        term_node = node_index.get(tt)
                        root_node = node_index.get(rt)
                        term_mid = id_to_mid.get(term_node.memory_id, "?") if term_node else "-"
                        root_mid = id_to_mid.get(root_node.memory_id, "?") if root_node else "-"
                        print(
                            f"      zone[{zi}]  terminal={term_mid}  "
                            f"root={root_mid}  size={len(zone.chain)}"
                        )
                except Exception as exc:
                    print(f"    ZONE DEBUG error: {exc!r}")

        print()
        print(f"  Confidence distribution across target queries: {dict(grammar_hit)}")

    finally:
        await backend.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
