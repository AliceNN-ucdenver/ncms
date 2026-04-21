"""MSEB-SoftwareDev labeler — raw ADR sections → CorpusMemory JSONL.

Each ADR section has a clear role in the state-evolution arc of the
decision.  The labeler uses heading-match first, then regex cues
in the body for the ``Status`` section (which carries supersession
signals like "Superseded by ADR-007").

| Section heading pattern                       | MemoryKind |
|-----------------------------------------------|------------|
| Title / preamble                              | ordinal_anchor |
| Context / Problem / Background                | declaration |
| Decision / Decision Outcome                    | declaration |
| Status (Accepted / Proposed)                   | declaration |
| Status (Deprecated / Superseded by X)          | retirement  |
| Rationale / Decision Drivers / Factors         | causal_link |
| Alternatives / Considered Options / Options    | retirement  (implicitly retires rejected alternatives) |
| Consequences / Outcomes / Implementation       | declaration |
| Supersedes / Deprecates                        | retirement  |
| Conclusion / Summary                            | ordinal_anchor |
| Other                                          | none        |

The rule set is deterministic over heading text; the regex fallbacks
for Status and Supersedes handle author variance.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("mseb_softwaredev.label")

DEFAULT_RAW = Path(__file__).parent / "raw"
DEFAULT_OUT = Path(__file__).parent / "raw_labeled"


# Ordered heading-substring → MemoryKind mapping.  First match wins.
HEADING_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(supersedes|deprecates)\b"), "retirement"),
    (re.compile(r"(?i)\balternatives?\s+considered\b"), "retirement"),
    (re.compile(r"(?i)\b(considered\s+options|options|alternatives)\b"),
        "retirement"),
    (re.compile(r"(?i)\brationale\b"),           "causal_link"),
    (re.compile(r"(?i)\bdecision\s+drivers?\b"), "causal_link"),
    (re.compile(r"(?i)\bfactors?\b"),            "causal_link"),
    (re.compile(r"(?i)\b(decision\s+outcome|decision)\b"), "declaration"),
    (re.compile(r"(?i)\bcontext\b"),             "declaration"),
    (re.compile(r"(?i)\bbackground\b"),          "declaration"),
    (re.compile(r"(?i)\bproblem\b"),             "declaration"),
    (re.compile(r"(?i)\bimplementation\b"),      "declaration"),
    (re.compile(r"(?i)\b(consequences?|outcomes?)\b"), "declaration"),
    (re.compile(r"(?i)\b(conclusion|summary|recap)\b"), "ordinal_anchor"),
    (re.compile(r"(?i)\bstatus\b"),              "declaration"),  # refined by body below
]


_SUPERSEDED_BODY = re.compile(
    r"(?i)\b(superseded|deprecated|obsoleted|replaced)\s+by\b",
)
_ACCEPTED_BODY = re.compile(r"(?i)\b(accepted|approved|ratified)\b")


def classify_section(heading: str, body: str, is_first: bool) -> str:
    """Deterministic rule chain.  See module docstring for the map."""
    heading_l = heading or ""
    # Title/preamble always gets ordinal_anchor when it's first.
    if is_first:
        return "ordinal_anchor"

    # Status section: refine with body-level cues.
    if re.search(r"(?i)\bstatus\b", heading_l):
        if _SUPERSEDED_BODY.search(body):
            return "retirement"
        if _ACCEPTED_BODY.search(body):
            return "declaration"
        return "declaration"

    for pat, kind in HEADING_PATTERNS:
        if pat.search(heading_l):
            return kind
    return "none"


def _mid_for(subject: str, message_id: str) -> str:
    suffix = message_id.split("::", 1)[-1]
    return f"{subject}-{suffix}"


def label_file(raw_path: Path, out_path: Path) -> dict[str, int]:
    lines = [
        json.loads(line) for line in
        raw_path.read_text(encoding="utf-8").split(chr(10)) if line.strip()
    ]
    if not lines:
        return {"total": 0}
    meta, body = lines[0], lines[1:]
    if "_meta" not in meta or not body:
        return {"total": 0, "skipped": "no_meta"}
    subject = meta["_subject"]
    stats: dict[str, int] = {"total": 0, "declaration": 0,
                              "retirement": 0, "causal_link": 0,
                              "ordinal_anchor": 0, "none": 0}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(body):
            heading = row.get("section", "")
            content = row.get("text", "")
            kind = classify_section(heading, content, is_first=(i == 0))
            observed_at = row.get("observed_at") or "1970-01-01T00:00:00Z"
            labeled = {
                "mid": _mid_for(subject, row["message_id"]),
                "subject": subject,
                "content": content,
                "observed_at": observed_at,
                "entities": [],
                "metadata": {
                    "kind": kind,
                    "source": row.get("source", "adr_section"),
                    "section": heading,
                    "source_msg_id": row["message_id"],
                    "source_set": row.get("source_set", ""),
                    "source_url": row.get("source_url", ""),
                    "license": row.get("license", ""),
                    "source_commit": row.get("source_commit", ""),
                    "retrieved_at": row.get("retrieved_at", ""),
                    "adr_status": row.get("adr_status", ""),
                    "domains": ["software_dev", "adr"],
                },
            }
            fh.write(json.dumps(labeled, ensure_ascii=False))
            fh.write("\n")
            stats[kind] += 1
            stats["total"] += 1
    return stats


def label_all(raw_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    files = 0
    for raw in sorted(raw_dir.glob("sdev-*.jsonl")):
        stats = label_file(raw, out_dir / raw.name)
        for k, v in stats.items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
        files += 1
    summary = {"files": files, **totals}
    (out_dir / "_label_stats.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
    )
    logger.info("labeled %d files, %d memories", files, totals.get("total", 0))
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="MSEB-SoftwareDev labeler")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    summary = label_all(args.raw_dir, args.out_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
