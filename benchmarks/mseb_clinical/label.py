"""MSEB-Clinical labeler — raw sections → CorpusMemory JSONL.

Phase 2 of the MSEB-Clinical pipeline.  Reads
``raw/PMC<id>.jsonl`` (output of ``mine.py``) and emits
``raw_labeled/PMC<id>.jsonl``.

Clinical labels are trickier than SWE because the narrative
section names are partly author-chosen.  Approach:

1. **Section-heading rule base** (fast, deterministic).
2. **Content regex fallback** for the ``other`` bucket.
3. **LLM post-pass** (optional, stubbed) for ambiguous rows.

| Section source | MemoryKind | Rationale |
| --- | --- | --- |
| ``abstract`` | ``ordinal_anchor`` | always the first section; summarises the arc |
| ``introduction`` / ``background`` | ``none`` | contextual framing |
| ``case presentation`` / ``case report`` / ``history`` | ``declaration`` | initial state |
| ``physical examination`` / ``investigations`` / ``workup`` | ``declaration`` | new evidence |
| ``differential diagnosis`` / ``initial diagnosis`` | ``declaration`` | working hypothesis |
| ``management`` / ``treatment`` / ``course`` | ``causal_link`` | intervention + response |
| ``outcome`` / ``follow-up`` | ``declaration`` | state update |
| ``final diagnosis`` | ``retirement`` | replaces earlier working dx |
| ``discussion`` | ``causal_link`` | retrospective reasoning |
| ``conclusion`` | ``ordinal_anchor`` | final outcome anchor |
| ``other`` | routed via content regex | catches bespoke headings |

The ``other``-bucket regex catches state-evolution cues on
headings like "Case Description", "Lessons learned", "Patient
and observation", "Case Summary", etc.

Usage::

    uv run python -m benchmarks.mseb_clinical.label \\
        --raw-dir benchmarks/mseb_clinical/raw \\
        --out-dir benchmarks/mseb_clinical/raw_labeled
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("mseb_clinical.label")

DEFAULT_RAW = Path(__file__).parent / "raw"
DEFAULT_OUT = Path(__file__).parent / "raw_labeled"


# Deterministic heading → MemoryKind map.
SOURCE_TO_KIND: dict[str, str] = {
    "abstract":                "ordinal_anchor",
    "introduction":            "none",
    "background":              "none",
    "case presentation":       "declaration",
    "case report":             "declaration",
    "presentation":            "declaration",
    "history":                 "declaration",
    "physical examination":    "declaration",
    "investigations":          "declaration",
    "workup":                  "declaration",
    "differential diagnosis":  "declaration",
    "initial diagnosis":       "declaration",
    "management":              "causal_link",
    "treatment":               "causal_link",
    "course":                  "causal_link",
    "outcome":                 "declaration",
    "follow-up":               "declaration",
    "final diagnosis":         "retirement",
    "conclusion":              "ordinal_anchor",
    "discussion":              "causal_link",
}


# Regexes that fire against the section_title / content for the
# "other" bucket.  Order matters — first match wins.  Tested
# against the 13 methods-like samples flagged in the pilot.
OTHER_HEADING_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(lessons?\s+learned|take[- ]?aways?)\b"),  "ordinal_anchor"),
    (re.compile(r"(?i)\bfinal\b.*\b(diagnosis|outcome)\b"),        "retirement"),
    (re.compile(r"(?i)\bcase\s+description\b"),                    "declaration"),
    (re.compile(r"(?i)\bcase\s+summary\b"),                        "declaration"),
    (re.compile(r"(?i)\bpatient\s+and\s+observation\b"),           "declaration"),
    (re.compile(r"(?i)\bcase\s+\d+\b"),                            "declaration"),
    (re.compile(r"(?i)\binvestigation\s+(?:and|&)\s+results?\b"),  "declaration"),
    (re.compile(r"(?i)\b(methods?|system\s+overview|evaluation)\b"), "none"),
    (re.compile(r"(?i)\bablation\b"),                              "none"),
]


# Content-level retirement hints (applied to ANY section when strong).
# Catches cases where diagnosis is revised mid-discussion.
CONTENT_RETIREMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\binitially\s+(?:diagnosed|thought|suspected)\b"),
    re.compile(r"(?i)\b(?:was\s+)?ruled\s+out\b"),
    re.compile(r"(?i)\brevised\s+diagnosis\b"),
    re.compile(r"(?i)\b(?:further|additional)\s+testing\s+revealed\b"),
    re.compile(r"(?i)\bmisdiagnosis\b"),
]


def _mid_for(pmcid: str, message_id: str) -> str:
    """Canonical mid: ``clin-pmc<id>-<suffix>`` (lowercase)."""
    suffix = message_id.split("::", 1)[-1]
    return f"clin-pmc{pmcid.lower()}-{suffix}"


def classify(source: str, section_title: str, content: str) -> str:
    """Primary classification: heading rule-base, then regex on 'other'."""
    # 1. Canonical heading → kind.
    base = SOURCE_TO_KIND.get(source)
    if base is not None:
        kind = base
    elif source == "other":
        # 2. Regex match on the author-chosen heading.
        kind = "none"
        for pat, target in OTHER_HEADING_PATTERNS:
            if pat.search(section_title or ""):
                kind = target
                break
    else:
        kind = "none"

    # 3. Content retirement override — even a section labeled
    # "discussion" carries retirement weight if it explicitly
    # talks about a revised diagnosis.
    if kind in {"none", "causal_link"}:
        for pat in CONTENT_RETIREMENT_PATTERNS:
            if pat.search(content):
                return "retirement"
    return kind


def label_row(row: dict, *, pmcid: str) -> dict:
    """Transform one raw row into a CorpusMemory-shaped dict."""
    source = row.get("source", "other")
    section_title = row.get("section_title", "")
    content = row.get("text", "")
    kind = classify(source, section_title, content)
    message_id = row["message_id"]
    subject = f"clin-pmc{pmcid.lower()}"
    return {
        "mid": _mid_for(pmcid, message_id),
        "subject": subject,
        "content": content,
        "observed_at": row["timestamp"],
        "entities": [],
        "metadata": {
            "kind": kind,
            "source": source,
            "section_title": section_title,
            "source_msg_id": message_id,
            "pmcid": pmcid,
            "domains": ["clinical"],
        },
    }


def _post_filter_methods_paper(meta: dict, sources: set[str]) -> bool:
    """Tighten pub-type filter as noted in mseb_clinical README §6.1.

    Returns ``True`` if the paper should be KEPT as a case report.
    """
    case_markers = {
        "case presentation", "case report", "history",
        "physical examination", "investigations", "presentation",
    }
    if case_markers & sources:
        return True
    # Headings mined into the "other" bucket that still indicate a
    # case narrative.
    heading_pattern = re.compile(
        r"(?i)\b(case\s+description|case\s+summary|"
        r"patient\s+and\s+observation|case\s+\d+)\b",
    )
    section_titles = meta.get("_section_titles", [])
    return any(heading_pattern.search(t or "") for t in section_titles)


def label_file(raw_path: Path, out_path: Path) -> dict[str, object]:
    """Label one raw/PMC<id>.jsonl → raw_labeled/PMC<id>.jsonl.

    Skips papers that fail the case-report post-filter.
    """
    lines = [
        json.loads(line) for line in
        raw_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not lines:
        return {"total": 0, "skipped": "empty_file"}

    # First line is the meta header emitted by mine.py
    meta, body = lines[0], lines[1:]
    if "_pmcid" not in meta:
        return {"total": 0, "skipped": "no_meta_header"}
    pmcid = meta["_pmcid"]

    sources = {row.get("source", "") for row in body}
    section_titles = [row.get("section_title", "") for row in body]
    if not _post_filter_methods_paper(
        {**meta, "_section_titles": section_titles}, sources,
    ):
        logger.info("PMC%s: skipped (methods-like, no case markers)", pmcid)
        return {"total": 0, "skipped": "methods_like"}

    stats: dict[str, int | str] = {"total": 0}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in body:
            labeled = label_row(row, pmcid=pmcid)
            fh.write(json.dumps(labeled, ensure_ascii=False))
            fh.write("\n")
            kind = labeled["metadata"]["kind"]
            stats[kind] = int(stats.get(kind, 0)) + 1  # type: ignore[arg-type]
            stats["total"] = int(stats["total"]) + 1
    return stats


def label_all(raw_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    files_labeled = 0
    files_skipped = 0
    skip_reasons: dict[str, int] = {}
    for raw in sorted(raw_dir.glob("PMC*.jsonl")):
        stats = label_file(raw, out_dir / raw.name)
        if stats.get("total", 0) == 0 and "skipped" in stats:
            files_skipped += 1
            reason = str(stats["skipped"])
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        for k, v in stats.items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
        files_labeled += 1
    summary = {
        "files_labeled": files_labeled,
        "files_skipped": files_skipped,
        "skip_reasons": skip_reasons,
        **totals,
    }
    (out_dir / "_label_stats.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
    )
    logger.info("labeled %d files, skipped %d (%s)",
                files_labeled, files_skipped, skip_reasons)
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="MSEB-Clinical labeler: raw sections → CorpusMemory JSONL",
    )
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    summary = label_all(args.raw_dir, args.out_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
