"""Forensics A: shape_intent head accuracy on held-out queries.

Runs v7.1 against:
  1. Training gold (sanity check — should be near 100%)
  2. A hand-crafted held-out set (12 shapes × 3 fresh queries = 36)
     with novel surface forms the SLM has never seen.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ncms.application.adapters.corpus.loader import load_jsonl
from ncms.application.adapters.methods.joint_bert_lora import LoraJointBert

ADAPTER = Path("adapters/checkpoints/software_dev/v7.1")
extractor = LoraJointBert(ADAPTER)


# Fresh held-out queries — phrasings distinct from the training templates.
HELD_OUT: list[tuple[str, str]] = [
    # current_state: what's the current value/choice now?
    ("current_state", "Which framework are we currently running in production?"),
    ("current_state", "What is the database we use today?"),
    ("current_state", "Tell me our current CI tool."),

    # before_named: what was in use before X?
    ("before_named", "What did we use before we switched to Postgres?"),
    ("before_named", "Which ORM predated Prisma in the codebase?"),
    ("before_named", "What was our orchestrator before Kubernetes?"),

    # retirement: what got deprecated / replaced?
    ("retirement", "Which technologies did we deprecate last quarter?"),
    ("retirement", "What did we sunset when we adopted the new stack?"),
    ("retirement", "Which option was replaced by the current choice?"),

    # origin: what motivated / started it?
    ("origin", "Why did we originally adopt this architecture?"),
    ("origin", "What problem led to introducing the message queue?"),
    ("origin", "How did we end up using this database?"),

    # sequence: narrative ordering of events
    ("sequence", "Walk me through the migration timeline from monolith to microservices."),
    ("sequence", "Trace the sequence of decisions that led to the current stack."),
    ("sequence", "Step through the history of our auth system."),

    # predecessor: alternatives considered before the final choice
    ("predecessor", "Which alternatives did we evaluate before picking the current framework?"),
    ("predecessor", "What options were on the shortlist before we chose Postgres?"),
    ("predecessor", "Which tools did we consider before settling on our current CI?"),

    # transitive_cause: rationale / drivers
    ("transitive_cause", "What factors justified the move to event-driven architecture?"),
    ("transitive_cause", "Why specifically did we pick Postgres over MongoDB?"),
    ("transitive_cause", "What drove the decision to adopt microservices?"),

    # causal_chain: chain of reasons
    ("causal_chain", "Explain the chain of reasons that led to adopting GraphQL."),
    ("causal_chain", "What sequence of issues caused us to rewrite the auth layer?"),
    ("causal_chain", "Give me the causal chain behind our move off Heroku."),

    # concurrent: consequences / side effects / co-occurring outcomes
    ("concurrent", "What other consequences came with adopting Kubernetes?"),
    ("concurrent", "Which side effects did the Postgres migration bring?"),
    ("concurrent", "What concurrent changes happened alongside the microservices rollout?"),

    # ordinal_first: first / opening / introductory
    ("ordinal_first", "What was the first decision we made about the architecture?"),
    ("ordinal_first", "Show me the opening context of the database ADR."),
    ("ordinal_first", "What initial concern kicked off the refactor?"),

    # ordinal_last: final / closing / last
    ("ordinal_last", "What was the final decision in the rate-limiting ADR?"),
    ("ordinal_last", "Show me the closing summary of the auth migration."),
    ("ordinal_last", "What was the last thing we resolved about the deployment?"),

    # interval: time-range / during period
    ("interval", "What were we running during 2023?"),
    ("interval", "Which tools were in use between the monolith era and now?"),
    ("interval", "What databases did we use during the legacy phase?"),

    # none: not a TLG query
    ("none", "I love coffee."),
    ("none", "The sky is blue."),
    ("none", "Restart the server."),
]


def score(rows: list[tuple[str, str]], name: str) -> None:
    confusion: dict[tuple[str, str], int] = defaultdict(int)
    per_shape_correct: Counter = Counter()
    per_shape_total: Counter = Counter()
    low_conf: list[tuple[str, str, str, float]] = []
    for gold_shape, text in rows:
        out = extractor.extract(text, domain="software_dev")
        pred = out.shape_intent or "none"
        conf = out.shape_intent_confidence or 0.0
        confusion[(gold_shape, pred)] += 1
        per_shape_total[gold_shape] += 1
        if pred == gold_shape:
            per_shape_correct[gold_shape] += 1
        elif conf > 0.7:
            # high-confidence wrong — the most interesting errors
            low_conf.append((gold_shape, pred, text, conf))

    total_correct = sum(per_shape_correct.values())
    total = sum(per_shape_total.values())
    print(f"\n=== {name}: {total_correct}/{total} = {total_correct/total:.3f} ===")
    print(f"{'shape':<18} acc")
    print("-" * 30)
    for shape in sorted(per_shape_total):
        c, n = per_shape_correct[shape], per_shape_total[shape]
        print(f"{shape:<18} {c}/{n} = {c/n:.3f}")

    if low_conf:
        print(f"\n  high-confidence WRONG predictions ({len(low_conf)}):")
        for gold, pred, text, conf in low_conf[:10]:
            print(f"    gold={gold!r:>18}  pred={pred!r:<18} conf={conf:.2f}")
            print(f"      {text[:100]!r}")


# ── 1. Sanity check on training gold ────────────────────────────
train_rows = [
    (r.shape_intent or "none", r.text)
    for r in load_jsonl("adapters/corpora/gold_shape_intent_software_dev.jsonl")
    if r.shape_intent is not None
]
score(train_rows, f"training gold (N={len(train_rows)})")

# ── 2. Held-out test ─────────────────────────────────────────────
score(HELD_OUT, f"held-out (N={len(HELD_OUT)})")
