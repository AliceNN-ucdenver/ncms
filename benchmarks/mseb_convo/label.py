"""MSEB-Convo labeler — raw turns → CorpusMemory JSONL + preference labels.

Phase 2 of the MSEB-Convo pipeline.  Reads
``raw/user-<id>.jsonl`` (output of ``mine.py``) and emits
``raw_labeled/user-<id>.jsonl`` carrying both:

- ``metadata.kind``        — MemoryKind (declaration / retirement / …)
- ``metadata.preference``  — PreferenceKind (positive / avoidance / …)

This is the labeler that sets up the per-preference evaluation
axis.  We use a rule-based spine (cheap, deterministic) plus a
content-regex classifier for each PreferenceKind.  The rules
target first-person declarative patterns (this corpus is first-
person by construction — it's a user + assistant dialog).

| Pattern class | Examples | PreferenceKind |
| --- | --- | --- |
| Affirmation / use | "I love X", "I use X", "I prefer X", "my favourite" | ``positive`` |
| Negation / avoidance | "I can't eat X", "I avoid Y", "I don't like" | ``avoidance`` |
| Habitual / routine | "every morning", "I usually", "I always" | ``habitual`` |
| Difficulty / struggle | "I struggle with", "Z is hard", "my biggest pain" | ``difficult`` |

The MemoryKind classifier uses cues:

| Pattern | MemoryKind |
| --- | --- |
| "I used to X, now Y" / "I've switched" / "no longer" | ``retirement`` |
| "because", "due to", "since" (causal conjunctions) | ``causal_link`` |
| First turn of the first session | ``ordinal_anchor`` |
| first-person state reveal (I am / I use / I have) | ``declaration`` |
| otherwise | ``none`` |

Assistant turns are always ``none`` for MemoryKind and ``none``
for PreferenceKind — they don't carry user state.

Usage::

    uv run python -m benchmarks.mseb_convo.label \\
        --raw-dir benchmarks/mseb_convo/raw \\
        --out-dir benchmarks/mseb_convo/raw_labeled
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("mseb_convo.label")

DEFAULT_RAW = Path(__file__).parent / "raw"
DEFAULT_OUT = Path(__file__).parent / "raw_labeled"


# ---------------------------------------------------------------------------
# Preference sub-type regexes.  First match wins in enum order.
# ---------------------------------------------------------------------------

PREFERENCE_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("avoidance", [
        re.compile(r"(?i)\bI\s+(?:can(?:'|no)?t|cannot)\s+(?:eat|have|do|use|take|handle)\b"),
        re.compile(r"(?i)\bI\s+(?:am\s+)?allergic\s+to\b"),
        re.compile(r"(?i)\bI\s+(?:don'?t|do not)\s+(?:eat|drink|use|like)\b"),
        re.compile(r"(?i)\bI\s+(?:avoid|stay\s+away\s+from)\b"),
        re.compile(r"(?i)\bno\s+(?:gluten|dairy|sugar|meat|caffeine)\b"),
    ]),
    ("habitual", [
        re.compile(r"(?i)\bevery\s+(?:morning|evening|night|day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
        re.compile(r"(?i)\bI\s+(?:usually|always|typically|normally|tend\s+to)\b"),
        re.compile(r"(?i)\bmy\s+(?:routine|habit|practice)\b"),
        re.compile(r"(?i)\bI\s+(?:regularly|often)\b"),
    ]),
    ("difficult", [
        re.compile(r"(?i)\bI\s+struggle\s+(?:with|to)\b"),
        re.compile(r"(?i)\b(?:is|has\s+been)\s+(?:hard|difficult|challenging|tough)\s+for\s+me\b"),
        re.compile(r"(?i)\bmy\s+(?:biggest|main)\s+(?:pain|challenge|struggle|issue|problem)\b"),
        re.compile(r"(?i)\bI\s+(?:can'?t|have\s+trouble)\s+(?:focus|concentrate|sleep|understand)\b"),
        re.compile(r"(?i)\bI\s+find\s+(?:it\s+)?(?:hard|difficult|challenging)\b"),
    ]),
    ("positive", [
        re.compile(r"(?i)\bI\s+(?:love|adore|enjoy|prefer)\b"),
        re.compile(r"(?i)\bmy\s+(?:favou?rite|go[- ]?to)\b"),
        re.compile(r"(?i)\bI\s+use\s+[A-Z]"),  # "I use Premiere Pro"
        re.compile(r"(?i)\bI\s+(?:am\s+a\s+fan\s+of|really\s+like)\b"),
        re.compile(r"(?i)\bI'?ve\s+been\s+using\b"),
    ]),
]


# ---------------------------------------------------------------------------
# MemoryKind patterns (ordered — first match wins).
# ---------------------------------------------------------------------------

RETIREMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bI\s+used\s+to\b.*\b(?:now|currently|these\s+days)\b"),
    re.compile(r"(?i)\bI'?ve\s+(?:switched|moved)\s+(?:from|away)\b"),
    re.compile(r"(?i)\bno\s+longer\b"),
    re.compile(r"(?i)\b(?:gave\s+up|stopped|quit)\s+(?:using|doing|eating)\b"),
    re.compile(r"(?i)\bI\s+(?:was|had\s+been)\s+[^.]{3,40}?\s+but\s+(?:now|then)\b"),
]

CAUSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bbecause\b"),
    re.compile(r"(?i)\bdue\s+to\b"),
    re.compile(r"(?i)\bsince\s+(?:I|my)\b"),
    re.compile(r"(?i)\bas\s+a\s+result\b"),
]

DECLARATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bI\s+(?:am|'m|have|use|work|live|own|prefer)\b"),
    re.compile(r"(?i)\bmy\s+(?:job|role|goal|plan|project|current)\b"),
]


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------


def classify_preference(text: str) -> str:
    """Return one of the 5 PreferenceKind labels."""
    for label, patterns in PREFERENCE_PATTERNS:
        if any(p.search(text) for p in patterns):
            return label
    return "none"


def classify_kind(
    text: str, *, is_first_turn_of_subject: bool, is_assistant: bool,
) -> str:
    """Return one of the 5 MemoryKind labels."""
    if is_assistant:
        return "none"
    if is_first_turn_of_subject:
        return "ordinal_anchor"
    for p in RETIREMENT_PATTERNS:
        if p.search(text):
            return "retirement"
    # Only label causal_link when there's also some declaration-shape
    # content; bare "because …" without a first-person state reveal
    # is more often chatter.
    has_causal = any(p.search(text) for p in CAUSAL_PATTERNS)
    has_declaration = any(p.search(text) for p in DECLARATION_PATTERNS)
    if has_causal and has_declaration:
        return "causal_link"
    if has_declaration:
        return "declaration"
    return "none"


def _mid_for(subject: str, message_id: str) -> str:
    """Canonical mid: ``convo-<subject>-<turn_id>``.

    Subject already starts with ``user-`` so ``convo-user-xxxx-m00NN``.
    """
    suffix = message_id.split("::", 1)[-1]
    return f"convo-{subject}-{suffix}"


def label_row(row: dict, *, is_first_turn_of_subject: bool) -> dict:
    """Transform one raw turn into a CorpusMemory-shaped dict."""
    content = row.get("text", "")
    role = row.get("role", "user")
    is_assistant = role == "assistant"

    kind = classify_kind(
        content,
        is_first_turn_of_subject=is_first_turn_of_subject,
        is_assistant=is_assistant,
    )
    preference = (
        "none" if is_assistant else classify_preference(content)
    )

    message_id = row["message_id"]
    subject = message_id.split("::", 1)[0]  # "user-xxxx"

    return {
        "mid": _mid_for(subject, message_id),
        "subject": subject,
        "content": content,
        "observed_at": row["timestamp"],
        "entities": [],
        "metadata": {
            "kind": kind,
            "preference": preference,
            "source": row.get("source", "user_turn"),
            "session_id": row.get("session_id", ""),
            "turn_index": row.get("turn_index", 0),
            "role": role,
            "source_msg_id": message_id,
            "domains": ["conversational"],
        },
    }


def label_file(raw_path: Path, out_path: Path) -> dict[str, int]:
    """Label one raw/user-<id>.jsonl → raw_labeled/user-<id>.jsonl."""
    raw_rows = [
        json.loads(line) for line in
        raw_path.read_text(encoding="utf-8").split(chr(10)) if line.strip()
    ]
    if not raw_rows:
        return {"total": 0}

    stats: dict[str, int] = {
        "total": 0,
        **{f"kind_{k}": 0 for k in (
            "declaration", "retirement", "causal_link",
            "ordinal_anchor", "none",
        )},
        **{f"pref_{p}": 0 for p in (
            "positive", "avoidance", "habitual", "difficult", "none",
        )},
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(raw_rows):
            labeled = label_row(row, is_first_turn_of_subject=(i == 0))
            fh.write(json.dumps(labeled, ensure_ascii=False))
            fh.write("\n")
            meta = labeled["metadata"]
            stats[f"kind_{meta['kind']}"] += 1
            stats[f"pref_{meta['preference']}"] += 1
            stats["total"] += 1
    return stats


def label_all(raw_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    file_count = 0
    for raw in sorted(raw_dir.glob("user-*.jsonl")):
        stats = label_file(raw, out_dir / raw.name)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        file_count += 1
    summary = {"files": file_count, **totals}
    (out_dir / "_label_stats.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
    )
    logger.info(
        "labeled %d files, %d turns — preferences: pos=%d avoid=%d habit=%d diff=%d",
        file_count, totals.get("total", 0),
        totals.get("pref_positive", 0), totals.get("pref_avoidance", 0),
        totals.get("pref_habitual", 0), totals.get("pref_difficult", 0),
    )
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="MSEB-Convo labeler: raw turns → CorpusMemory JSONL "
                    "with kind + preference labels",
    )
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    summary = label_all(args.raw_dir, args.out_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
