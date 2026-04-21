"""Build MSEB-Convo gold.yaml directly from LongMemEval questions.

The template-based gold author assumed global search across all
users.  LMEval is designed to be scoped per-user: each question is
asked against one user's haystack.  Our global-search framing
meant queries like "What does the user currently prefer?"
ambiguously matched any of 15 users' preference turns — rank-1
was near-random.

This tool uses the LMEval question text directly — those questions
are naturally user-scoped because they reference specific topics
("How much did I earn at the Downtown Farmers Market?", "What's
the phone number of the Speyer tourism board?") that uniquely
anchor to one user's haystack content.

Gold selection: LMEval provides ``answer_session_ids`` per
question — these are the sessions (not turns) that contain the
answer.  We identify gold memories as the USER turns within
those sessions (assistant turns rarely contain the answer
verbatim — the user established the fact).  If no user turn is
found we fall back to the first turn in the answer session.

Shape mapping from LMEval question_type → MSEB intent shape:

| LMEval question_type         | MSEB shape       | rationale |
|------------------------------|------------------|-----------|
| knowledge-update             | retirement       | state evolved |
| temporal-reasoning           | ordinal_first / ordinal_last / sequence | order-sensitive |
| multi-session                | causal_chain     | spans sessions |
| single-session-user          | current_state    | single fact |
| single-session-assistant     | origin           | first asked earlier |
| single-session-preference    | current_state (with preference sub-type) | preference lookup |
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("mseb_convo.gold_from_lmeval")


# ---------------------------------------------------------------------------
# LMEval question_type → MSEB shape
# ---------------------------------------------------------------------------

_QTYPE_TO_SHAPE: dict[str, str] = {
    "knowledge-update":          "retirement",
    "multi-session":             "causal_chain",
    "single-session-user":       "current_state",
    "single-session-assistant":  "origin",
    "single-session-preference": "current_state",
}


# "order", "first", "before", etc. cues in temporal-reasoning questions —
# map to the most specific shape available.
_ORDER_CUES = re.compile(r"(?i)\border\b")
_FIRST_CUES = re.compile(r"(?i)\b(first|earlier|initial(?:ly)?|original)\b")
_LAST_CUES  = re.compile(r"(?i)\b(last|recent|most\s+recent|latest|final)\b")


def _shape_for_temporal(question: str) -> str:
    if _ORDER_CUES.search(question):
        return "sequence"
    if _FIRST_CUES.search(question):
        return "ordinal_first"
    if _LAST_CUES.search(question):
        return "ordinal_last"
    return "sequence"


def shape_for_question(question: str, qtype: str) -> str:
    if qtype == "temporal-reasoning":
        return _shape_for_temporal(question)
    return _QTYPE_TO_SHAPE.get(qtype, "current_state")


def preference_for_question(qtype: str, answer: str) -> str:
    """LMEval's single-session-preference answers blend positive /
    negative into prose.  Cheap classifier: look for explicit cues
    in the answer text."""
    if qtype != "single-session-preference":
        return "none"
    a = (answer or "").lower()
    if "would not" in a or "avoid" in a or "dislike" in a:
        return "avoidance"
    if "every " in a or "always" in a or "routine" in a or "usually" in a:
        return "habitual"
    if "struggle" in a or "difficult" in a or "hard" in a:
        return "difficult"
    return "positive"


# ---------------------------------------------------------------------------
# Gold memory selection from labeled corpus
# ---------------------------------------------------------------------------


def _load_labeled_by_subject(
    labeled_dir: Path,
) -> dict[str, list[dict]]:
    """Group labeled memories by subject + preserve turn order."""
    out: dict[str, list[dict]] = {}
    for jsonl in sorted(labeled_dir.glob("user-*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            subj = row["subject"]
            out.setdefault(subj, []).append(row)
    for subj in out:
        out[subj].sort(key=lambda r: (
            r.get("metadata", {}).get("session_id", ""),
            r.get("metadata", {}).get("turn_index", 0),
        ))
    return out


def pick_gold_memory(
    chain: list[dict], answer_session_ids: list[str], qtype: str,
) -> tuple[str, list[str]]:
    """Pick (gold_mid, gold_alt_list) for one LMEval question.

    Primary strategy: a USER turn within one of the answer sessions.
    Secondary:        the FIRST turn in any answer session.
    For temporal ``ordinal_last`` we reverse the order to prefer the
    latest user turn instead.
    """
    if not chain or not answer_session_ids:
        return "", []
    answer_set = set(answer_session_ids)
    user_turns = [
        m for m in chain
        if m.get("metadata", {}).get("session_id") in answer_set
        and m.get("metadata", {}).get("role") == "user"
    ]
    if qtype.startswith("temporal-reasoning"):
        # Prefer latest for "most recent" queries.  Not perfect but
        # better than arbitrary.
        user_turns.reverse()
    if user_turns:
        gold = user_turns[0]["mid"]
        alts = [m["mid"] for m in user_turns[1:3]]
        return gold, alts
    # Fall back to first turn in answer session.
    any_turns = [m for m in chain
                 if m.get("metadata", {}).get("session_id") in answer_set]
    if any_turns:
        return any_turns[0]["mid"], [m["mid"] for m in any_turns[1:3]]
    return "", []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_gold(
    questions_path: Path, labeled_dir: Path, out_path: Path,
) -> dict:
    by_subject = _load_labeled_by_subject(labeled_dir)
    rows: list[dict] = []
    per_shape: dict[str, int] = {}
    per_preference: dict[str, int] = {}
    missing_gold = 0

    for line_no, line in enumerate(
        questions_path.read_text(encoding="utf-8").split("\n"), start=1,
    ):
        line = line.strip()
        if not line:
            continue
        q = json.loads(line)
        subject = q.get("subject", "")
        if subject not in by_subject:
            continue
        qtext = q.get("question", "").strip()
        if not qtext:
            continue
        qtype = q.get("question_type", "")
        answer = q.get("answer", "")
        answer_sessions = q.get("answer_session_ids", []) or []

        shape = shape_for_question(qtext, qtype)
        pref = preference_for_question(qtype, answer)

        gold_mid, gold_alt = pick_gold_memory(
            by_subject[subject], answer_sessions, qtype,
        )
        if not gold_mid:
            missing_gold += 1
            continue

        qid = f"convo-{shape}-{len(rows)+1:04d}"
        rows.append({
            "qid": qid,
            "shape": shape,
            "text": qtext,
            "subject": subject,
            "gold_mid": gold_mid,
            "gold_alt": gold_alt,
            "preference": pref,
            "note": f"lmeval:{qtype}",
        })
        per_shape[shape] = per_shape.get(shape, 0) + 1
        per_preference[pref] = per_preference.get(pref, 0) + 1

    # Add a handful of noise queries (unrelated topics) for rejection
    # testing.  LMEval doesn't ship these — we hand-write 10.
    noise_texts = [
        "What is the airspeed velocity of an unladen swallow?",
        "How do you season a cast iron skillet for the first time?",
        "What were the total casualties at the Battle of Trafalgar?",
        "How does quantum entanglement violate Bell's inequality?",
        "What's the recipe for authentic Neapolitan pizza dough?",
        "Which novel won the Booker Prize in 2014?",
        "What's the melting point of tungsten?",
        "How many islands comprise the Japanese archipelago?",
        "What is the square root of 2 to ten decimal places?",
        "Who discovered the penicillium mold?",
    ]
    for i, t in enumerate(noise_texts, start=1):
        rows.append({
            "qid": f"convo-noise-{i:03d}",
            "shape": "noise",
            "text": t,
            "subject": "",
            "gold_mid": "",
            "gold_alt": [],
            "preference": "none",
            "note": "adversarial / off-topic",
        })

    # YAML dump
    try:
        import yaml
        body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
    except ImportError:
        body = json.dumps(rows, indent=2, ensure_ascii=False)
    header = (
        "# MSEB-Convo gold — rebuilt from LongMemEval question texts.\n"
        "# Each query is the original LMEval question (naturally user-scoped\n"
        "# because it references user-specific topics).  gold_mid is a user\n"
        "# turn from the LMEval-designated answer session.  See\n"
        "# benchmarks/mseb_convo/gold_from_lmeval.py for the shape mapping.\n\n"
    )
    out_path.write_text(header + body, encoding="utf-8")

    stats = {
        "total": len(rows),
        "missing_gold": missing_gold,
        "per_shape": per_shape,
        "per_preference": per_preference,
    }
    logger.info("built: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", type=Path,
                    default=Path("benchmarks/mseb_convo/raw/_questions.jsonl"))
    ap.add_argument("--labeled-dir", type=Path,
                    default=Path("benchmarks/mseb_convo/raw_labeled"))
    ap.add_argument("--out", type=Path,
                    default=Path("benchmarks/mseb_convo/gold.yaml"))
    args = ap.parse_args()
    stats = build_gold(args.questions, args.labeled_dir, args.out)
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
