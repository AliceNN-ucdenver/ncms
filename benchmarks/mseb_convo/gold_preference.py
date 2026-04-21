"""Generate Convo preference gold queries from labeled preference turns.

Walks the labeled corpus for user turns where
``metadata.preference`` is a real value (not ``"none"``).  For each
turn, extracts the user's *preference target* (the noun phrase that
follows the trigger verb) and emits a query that:

1. uses preference vocabulary that ``check_preference`` recognises
   (``prefer``, ``favourite``, ``avoid``, ``struggle``, ``every``),
2. references the target noun phrase (so TF-lift can fire),
3. is flagged ``query_class: preference``, ``preference: <kind>``.

The auditor's TF-lift rule then accepts these as preference-class
queries.  Output: new ``gold_preference.yaml`` to be merged into
``gold.yaml``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Target extraction — cheap NP extractor keyed to the trigger verb
# ---------------------------------------------------------------------------

_TARGET_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "positive": [
        re.compile(r"(?i)\bI\s+(?:love|adore|enjoy|prefer|really\s+like)\s+(?:using\s+|to\s+use\s+)?([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bmy\s+(?:favou?rite|go[- ]?to)\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bI\s+use\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bI'?ve\s+been\s+using\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
    ],
    "avoidance": [
        re.compile(r"(?i)\bI\s+can(?:'|no)?t\s+(?:eat|have|use|take|handle)\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bI\s+(?:am\s+)?allergic\s+to\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bI\s+(?:avoid|don'?t\s+(?:eat|drink|use|like))\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
    ],
    "habitual": [
        re.compile(r"(?i)\bevery\s+(?:morning|evening|night|day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:I\s+)?([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bI\s+(?:usually|always|typically|tend\s+to)\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bmy\s+(?:routine|habit|practice)\s+(?:is\s+|involves\s+)?([A-Za-z0-9][\w\s.&'-]{2,40})"),
    ],
    "difficult": [
        re.compile(r"(?i)\bI\s+struggle\s+(?:with|to)\s+([A-Za-z0-9][\w\s.&'-]{2,40})"),
        re.compile(r"(?i)\bmy\s+(?:biggest|main)\s+(?:pain|challenge|struggle|issue|problem)\s+(?:is\s+|with\s+)?([A-Za-z0-9][\w\s.&'-]{2,40})"),
    ],
}


# Query templates per preference kind — each uses vocab the auditor recognises
# and references {target} so TF-lift against the chain fires.
_QUERY_TEMPLATES: dict[str, list[str]] = {
    "positive": [
        "What does the user prefer when it comes to {target}?",
        "Which {target} is the user's go-to?",
        "What's the user's favourite approach to {target}?",
    ],
    "avoidance": [
        "What does the user avoid regarding {target}?",
        "Which {target} does the user not prefer?",
    ],
    "habitual": [
        "What's the user's routine involving {target}?",
        "How often does the user engage with {target}?",
    ],
    "difficult": [
        "What does the user struggle with about {target}?",
        "What's a challenge the user has with {target}?",
    ],
}


def _clean_target(raw: str) -> str:
    """Normalise an extracted target phrase.  Drops trailing punctuation
    and common sentence continuations; keeps up to 40 chars of noun
    phrase."""
    raw = raw.strip().rstrip(".,;:")
    # Cut at conjunction-y words — likely end of the noun phrase.
    for stop in (" for ", " because ", " since ", " when ", " at ", " on ",
                 " that ", " which ", " with ", " and "):
        idx = raw.lower().find(stop)
        if idx > 4:
            raw = raw[:idx]
            break
    return raw.strip()[:60]


def _extract_target(content: str, pref_kind: str) -> str:
    for pattern in _TARGET_PATTERNS.get(pref_kind, []):
        m = pattern.search(content)
        if m:
            return _clean_target(m.group(1))
    return ""


def build_preference_gold(
    labeled_dir: Path, max_per_kind: int = 15,
) -> list[dict]:
    """Walk labeled turns, emit preference queries per kind."""
    rng = random.Random(42)
    picked: dict[str, list[dict]] = {k: [] for k in _TARGET_PATTERNS}
    out: list[dict] = []

    files = sorted(labeled_dir.glob("user-*.jsonl"))
    rng.shuffle(files)
    for jsonl in files:
        for line in jsonl.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            meta = row.get("metadata", {})
            if meta.get("role") != "user":
                continue
            kind = meta.get("preference", "none")
            if kind == "none" or kind not in _QUERY_TEMPLATES:
                continue
            if len(picked[kind]) >= max_per_kind:
                continue
            target = _extract_target(row.get("content", ""), kind)
            if len(target) < 4:
                continue
            # Pick a query template; mix them so one user doesn't always
            # produce the identical wording.
            tpl = rng.choice(_QUERY_TEMPLATES[kind])
            query_text = tpl.format(target=target)
            picked[kind].append({"row": row, "target": target, "tpl": tpl,
                                 "text": query_text})

    for kind, items in picked.items():
        for i, item in enumerate(items, start=1):
            row = item["row"]
            qid = f"convo-pref-{kind}-{i:03d}"
            out.append({
                "qid": qid,
                "shape": "current_state",
                "query_class": "preference",
                "text": item["text"],
                "subject": row["subject"],
                "gold_mid": row["mid"],
                "gold_alt": [],
                "preference": kind,
                "note": f"preference/{kind}/derived-from-user-turn",
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labeled-dir", type=Path,
                    default=Path("benchmarks/mseb_convo/raw_labeled"))
    ap.add_argument("--out", type=Path,
                    default=Path("benchmarks/mseb_convo/gold_preference.yaml"))
    ap.add_argument("--max-per-kind", type=int, default=15)
    args = ap.parse_args()

    rows = build_preference_gold(args.labeled_dir, max_per_kind=args.max_per_kind)
    try:
        import yaml
        body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
    except ImportError:
        body = json.dumps(rows, indent=2, ensure_ascii=False)
    args.out.write_text(
        "# Convo preference gold — derived from labeled user turns.\n"
        "# See benchmarks/mseb_convo/gold_preference.py.\n\n" + body,
        encoding="utf-8",
    )
    from collections import Counter
    by_kind = Counter(r["preference"] for r in rows)
    print(f"wrote {len(rows)} preference queries to {args.out}")
    print(f"by kind: {dict(by_kind)}")


if __name__ == "__main__":
    sys.exit(main())
