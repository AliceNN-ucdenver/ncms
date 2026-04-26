"""Audit MSEB convo gold against TLG grammar semantics.

Runs a rule-based classifier over each of the 269 MSEB convo gold
queries and produces a per-query recommendation:

* ``KEEP``   — current shape label matches TLG semantics
* ``REMAP:<shape>`` — label mis-matches; a different TLG shape fits
* ``DROP``   — query doesn't map to ANY TLG grammar shape and
  should be excluded from shape_intent training

Write the audit to /tmp/convo_gold_audit.jsonl (one line per query)
and /tmp/convo_gold_audit.md (human-readable summary).

The rule set follows the TLG grammar's production definitions:

* ``ordinal_first`` — queries asking "which X was first" / "which
  X came first, A or B" — these look legitimate in convo.
* ``ordinal_last``  — "which X last" / "most recent X" / "X I last
  Y'd" — legitimate.
* ``sequence``      — "order of X" / "X in chronological order" /
  "how many days between X and Y" → REMAP to ``interval`` for
  between-queries; KEEP for true order queries.
* ``retirement``    — "when did I stop X" / "what did I give up" /
  "X I used to do but don't anymore".  Most convo "retirement"
  rows are DURATION queries ("How long have I had X") — those
  should DROP or REMAP to current_state.
* ``origin``        — "what did I first X" / "when did I start X".
  Convo "origin" queries are mostly "remind me what you said
  about X earlier" — DROP (they're memory recall, not state
  origin).
* ``current_state`` — "what is my current X" / "where do I X" /
  "what do I use now".  Many convo current_state rows are actually
  past fact lookups ("where did I attend Y") — DROP those;
  KEEP queries with present-tense structure.
* ``causal_chain``  — "what factors led to X" / "chain of events
  that caused Y".  Convo causal_chain is mostly AGGREGATION (
  "total amount spent", "how many days in total") — DROP.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import yaml

GOLD_PATH = Path("/Users/shawnmccarthy/ncms/benchmarks/mseb_convo/gold_locked.yaml")
OUT_JSONL = Path("/tmp/convo_gold_audit.jsonl")
OUT_MD = Path("/tmp/convo_gold_audit.md")


def classify(text: str, current_shape: str) -> tuple[str, str]:
    """Return (verdict, reason).

    Verdicts:
        "keep"         — current shape matches TLG semantics
        "remap:<X>"    — label mis-matches, another TLG shape fits
        "remap:none"   — query is LongMemEval but not a TLG shape;
                         train the adapter to return shape_intent=none
                         on queries like this (abstain).  Includes
                         aggregation, memory-recall, past-fact-lookup
                         patterns.
        "drop"         — rare edge cases where the query is so
                         ambiguous that training on it is noisy.
    """
    t = text.lower()

    # ─── ordinal_first ───
    if current_shape == "ordinal_first":
        # "which X came first, A or B" / "who did Y first" / "first X in"
        if re.search(r"\b(first|initial|earliest)\b", t):
            return ("keep", "contains 'first/initial/earliest' marker")
        return ("drop", "ordinal_first without 'first' marker")

    # ─── ordinal_last ───
    if current_shape == "ordinal_last":
        if re.search(
            r"\b(last|most recent|latest|final)\b|since i last",
            t,
        ):
            return ("keep", "contains 'last/most recent/latest' marker")
        return ("drop", "ordinal_last without recency marker")

    # ─── sequence ───
    if current_shape == "sequence":
        # Legitimate sequence: "order of" / "in chronological order"
        if re.search(r"\border of\b|chronological order", t):
            return ("keep", "contains 'order of' or 'chronological order'")
        # "how many days between X and Y" → interval
        if re.search(
            r"(days|weeks|months) (had )?passed between|"
            r"between .+ and .+",
            t,
        ):
            return ("remap:interval", "between-queries map to interval shape")
        # "how many weeks ago did I" → ordinal_last (recency)
        if re.search(r"(days|weeks|months|years) ago", t):
            return (
                "remap:ordinal_last",
                "'X ago' queries map to ordinal_last (recency)",
            )
        # "most recently" → ordinal_last
        if "most recently" in t or "most recent" in t:
            return ("remap:ordinal_last", "'most recent' → ordinal_last")
        # "how long had I been X" → current_state (duration of current)
        if re.search(r"how long had i been|how long have i been", t):
            return (
                "remap:current_state",
                "'how long have I been X' → current_state (ongoing)",
            )
        # "what time do I" → current_state (routine)
        if re.search(r"\bwhat time do i\b", t):
            return (
                "remap:current_state",
                "routine query → current_state",
            )
        # "which X did I Y most recently" → ordinal_last
        if "most recently" in t:
            return ("remap:ordinal_last", "→ ordinal_last")
        return (
            "remap:none",
            "sequence without clear order marker → abstain",
        )

    # ─── retirement ───
    if current_shape == "retirement":
        # Legitimate retirement: "stopped X" / "no longer X" / "used
        # to X but now Y" / "when did I give up X".
        if re.search(
            r"\bstopped\b|no longer|used to.*but now|"
            r"\bgave up\b|\bgiving up\b|\bquit\b|\babandoned\b|"
            r"\bdiscontinued\b|\bretired\b",
            t,
        ):
            return ("keep", "contains retirement marker")
        # "how long have I had X" → current_state (duration of current)
        if re.search(r"how long (have|had) i (had|been)", t):
            return (
                "remap:current_state",
                "'how long have I had' = duration of current state",
            )
        # "where did I go on my most recent X" → ordinal_last
        if re.search(r"most recent|latest", t):
            return ("remap:ordinal_last", "→ ordinal_last")
        # "what is my current X" → current_state (mis-labeled)
        if re.search(r"\b(current|currently|today|now)\b", t):
            return (
                "remap:current_state",
                "contains current-state marker, not retirement",
            )
        # "do I have" / "how many X do I have now" → current_state
        if re.search(r"do i (still )?have|how many.*(do i|did i have)", t):
            return ("remap:current_state", "possession query → current_state")
        # "what type of vehicle am I working on" → current_state
        if re.search(r"what (am i|kind|type|brand).*(currently|now)", t):
            return ("remap:current_state", "current-activity query")
        # "how many/often do I X" = routine/count-of-current → current_state
        if re.search(
            r"how (many|often|much time) (do|have) i",
            t,
        ):
            return (
                "remap:current_state",
                "routine-count / current-accumulation → current_state",
            )
        # "what time do I" / "where did I" → past fact / routine
        if re.search(r"\bwhat time do i\b", t):
            return (
                "remap:current_state",
                "routine query → current_state",
            )
        # Everything else — abstain (not a state-evolution query)
        return (
            "remap:none",
            "retirement without state-change marker → abstain",
        )

    # ─── origin ───
    if current_shape == "origin":
        # True TLG origin: "when did I first X" / "what started Y" /
        # "how did I originally get into X"
        if re.search(
            r"\b(originally|first started|when did i (first|start)|"
            r"how did i (start|begin|first get))\b|"
            r"\bwhat started\b|\bwhat first\b",
            t,
        ):
            return ("keep", "contains origin/start marker")
        # "remind me what you said" / "looking back at our previous
        # conversation" — these are memory RECALL, not state origin.
        # Train the adapter to abstain (shape_intent=none) on them.
        if re.search(
            r"remind me|previous conversation|previous chat|"
            r"earlier.*(conversation|chat|discussion)|"
            r"looking back at|going through|discussed earlier|"
            r"we talked about|you (mentioned|told me|said|provided|gave)",
            t,
        ):
            return (
                "remap:none",
                "memory-recall query → shape_intent=none (abstain)",
            )
        return (
            "remap:none",
            "origin without start marker → abstain",
        )

    # ─── current_state ───
    if current_shape == "current_state":
        # Legitimate: "what is my current X" / "what am I currently
        # doing" / "where do I X"
        if re.search(
            r"\b(current|currently|today|now|these days)\b|"
            r"\bwhat am i\b|\bwhere do i\b|"
            r"\bwhat (do|am) i (currently|usually|typically)\b",
            t,
        ):
            return ("keep", "present-tense / current marker")
        # "what book am I reading" (present-tense)
        if re.search(
            r"\b(am i reading|am i working on|am i watching|"
            r"do i use|do i take|do i wake up|do i go)\b",
            t,
        ):
            return ("keep", "present-tense activity query")
        # "how much time do I X every day" / "how often do I X" /
        # "how many X do I Y in a typical week" → routine current_state
        if re.search(
            r"how (much time|often) do i|"
            r"how many .* (in a (typical|usual) (day|week|month))|"
            r"how many .* do i (have|use|make|take|own)",
            t,
        ):
            return ("keep", "routine / count-of-current")
        # Past-tense specific events: "how much RAM did I upgrade to"
        # / "where did I attend Y" — NOT current state; these are
        # past fact-lookups.  Train adapter to abstain on them.
        if re.search(
            r"\bhow (much|many) (did|had|was)\b|"
            r"\bwhere did i (attend|go|travel|move|eat|visit|complete)\b|"
            r"\bwhat (did i|play|movie|book|recipe) .*(last|in )\b",
            t,
        ):
            return (
                "remap:none",
                "past-event fact lookup → abstain",
            )
        # "how long have I been X" → current_state (duration)
        if re.search(r"how long (have|had) i been", t):
            return ("keep", "duration-of-current state")
        # "what was my previous X" / "before I changed it" → retirement
        if re.search(r"\bprevious|\bformer\b|\bbefore.*changed", t):
            return ("remap:retirement", "previous-state query → retirement")
        # "how many X do I have" → current_state (count of current)
        if re.search(r"how many.*do i have", t):
            return ("keep", "count-of-current")
        return (
            "remap:none",
            "current_state without present-tense marker → abstain",
        )

    # ─── causal_chain ───
    if current_shape == "causal_chain":
        # True causal: "chain of events that led to X" / "why did I
        # end up doing Y" / "what caused X"
        if re.search(
            r"chain of|what caused|what led to|"
            r"series of events|what factors",
            t,
        ):
            return ("keep", "contains causal marker")
        # Aggregation queries: "how many X in total" / "total amount
        # spent" — no TLG shape; train adapter to abstain.
        if re.search(
            r"\bhow many\b|\btotal\b|\bin total\b|\binfinity\b|"
            r"what (is|was) the (total|sum|count|percentage|average)",
            t,
        ):
            return (
                "remap:none",
                "aggregation query → shape_intent=none (abstain)",
            )
        return (
            "remap:none",
            "causal_chain without causal marker → abstain",
        )

    # ─── noise — keep as-is
    if current_shape == "noise":
        return ("keep", "noise query — expected grammar abstain")

    return ("drop", f"unhandled shape: {current_shape}")


def main() -> None:
    rows = yaml.safe_load(GOLD_PATH.read_text())
    audits: list[dict] = []
    for row in rows:
        verdict, reason = classify(row["text"], row["shape"])
        audits.append(
            {
                "qid": row["qid"],
                "current_shape": row["shape"],
                "text": row["text"],
                "verdict": verdict,
                "reason": reason,
            }
        )

    OUT_JSONL.write_text(
        "\n".join(json.dumps(a) for a in audits) + "\n",
    )

    # Summary
    by_verdict_and_shape: dict[str, Counter] = {}
    for a in audits:
        verdict_bucket = a["verdict"]
        if verdict_bucket.startswith("remap:"):
            verdict_bucket = "remap"
        by_verdict_and_shape.setdefault(a["current_shape"], Counter())[verdict_bucket] += 1

    lines: list[str] = [
        "# Convo Gold TLG-Semantic Audit\n",
        f"Total queries: {len(audits)}\n",
        "## Per-shape verdict distribution\n",
        "| current shape | n | keep | remap | drop |",
        "|---|---:|---:|---:|---:|",
    ]
    overall = Counter()
    for shape in sorted(by_verdict_and_shape):
        counts = by_verdict_and_shape[shape]
        total = sum(counts.values())
        lines.append(
            f"| {shape} | {total} | "
            f"{counts.get('keep', 0)} | "
            f"{counts.get('remap', 0)} | "
            f"{counts.get('drop', 0)} |"
        )
        overall["keep"] += counts.get("keep", 0)
        overall["remap"] += counts.get("remap", 0)
        overall["drop"] += counts.get("drop", 0)

    n = len(audits)
    lines.append(
        f"| **TOTAL** | **{n}** | "
        f"**{overall['keep']} ({100 * overall['keep'] / n:.0f}%)** | "
        f"**{overall['remap']} ({100 * overall['remap'] / n:.0f}%)** | "
        f"**{overall['drop']} ({100 * overall['drop'] / n:.0f}%)** |"
    )

    # Remap distribution
    remaps: Counter = Counter()
    for a in audits:
        if a["verdict"].startswith("remap:"):
            remaps[a["verdict"][6:]] += 1
    if remaps:
        lines.append("\n## Remap targets\n")
        for tgt, cnt in remaps.most_common():
            lines.append(f"- **{tgt}**: {cnt}")

    # Sample verdicts for each bucket
    lines.append("\n## Sample drops (5 per shape)\n")
    for shape in sorted(by_verdict_and_shape):
        drops = [a for a in audits if a["current_shape"] == shape and a["verdict"] == "drop"]
        if not drops:
            continue
        lines.append(f"\n### {shape} drops (showing 5 of {len(drops)})\n")
        for a in drops[:5]:
            lines.append(f"- `{a['qid']}` — {a['text'][:120]}\n  - reason: *{a['reason']}*")

    lines.append("\n## Sample remaps (3 per target)\n")
    for tgt in remaps:
        remapped = [a for a in audits if a["verdict"] == f"remap:{tgt}"]
        lines.append(f"\n### remap:{tgt} (showing 3 of {len(remapped)})\n")
        for a in remapped[:3]:
            lines.append(
                f"- `{a['qid']}` (was {a['current_shape']}) — "
                f"{a['text'][:120]}\n  - reason: *{a['reason']}*"
            )

    OUT_MD.write_text("\n".join(lines) + "\n")
    print(f"\nWrote audit to {OUT_MD} and {OUT_JSONL}")
    print(f"\n{overall['keep']} keep, {overall['remap']} remap, {overall['drop']} drop ({n} total)")


if __name__ == "__main__":
    main()
