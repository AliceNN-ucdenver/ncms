"""MSEB-SoftwareDev miner — ADR + RFC + post-mortem → message tuples.

Phase 1 of the MSEB-SoftwareDev pipeline.  Pluggable per source:
each source is a :class:`Source` subclass that knows how to
produce a stream of raw message dicts.  The orchestrator runs
all registered sources, emitting one ``raw/<subject>.jsonl`` per
document.

Currently registered sources:

- ``adr_jph``  — Architecture Decision Records from
  `joelparkerhenderson/architecture-decision-record`_ (42 English
  examples).  CC-BY-NC-SA.  Each example's ``README.md`` is one
  subject; markdown headings split into sections.
- *(scaffold)* ``adr_log4brains``, ``rfc_ietf``, ``post_mortems``,
  ``threat_models`` — stubs ready to extend in follow-on sprints.

.. _joelparkerhenderson/architecture-decision-record:
   https://github.com/joelparkerhenderson/architecture-decision-record

Output layout::

    raw/
    ├── sdev-adr-jph-<slug>.jsonl    ← per-subject messages
    ├── _sources.json                ← provenance manifest
    └── _stats.json                  ← mining summary

Every mined message carries provenance metadata:
``source_set`` (which source catalogue), ``source_url``, ``license``,
``retrieved_at`` (ISO timestamp of the mine run), and
``source_commit`` for git-backed sources.  These fields survive
through labeling → build → ingest, so any result can be traced
back to a specific line in a specific file at a specific commit.

Usage::

    # Clone the ADR repo once, then mine
    uv run python -m benchmarks.mseb_softwaredev.mine \\
        --source adr_jph \\
        --src-dir /tmp/jph-adr \\
        --out-dir benchmarks/mseb_softwaredev/raw

    # All registered sources (once they're populated)
    uv run python -m benchmarks.mseb_softwaredev.mine --all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("mseb_softwaredev.mine")

DEFAULT_OUT = Path(__file__).parent / "raw"


# ---------------------------------------------------------------------------
# Source abstraction — one per catalogue
# ---------------------------------------------------------------------------


class Source(ABC):
    """One corpus source.  Implementations emit raw message dicts."""

    #: Short source identifier (e.g. ``adr_jph``).  Becomes part of the
    #: subject slug: ``sdev-<source_id>-<doc_slug>``.
    source_id: str = ""
    #: Source license (SPDX-style string).
    license: str = ""
    #: Human-readable provenance string for the DATASHEET.
    description: str = ""
    #: Public URL for the source repo / site.
    source_url: str = ""

    @abstractmethod
    def iter_documents(self, src_dir: Path) -> Iterator[dict]:
        """Yield one ``{"subject", "title", "messages"[]}`` dict per
        document.  ``messages`` is a list of ``{text, section, observed_at?}``.
        """


def _slugify(text: str, max_chars: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.lower()).strip("-")
    return s[:max_chars]


def _git_head(path: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out[:12]
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------------------
# Source: joelparkerhenderson/architecture-decision-record (42 ADRs, CC BY-NC-SA)
# ---------------------------------------------------------------------------


class AdrJosephParkerHendersonSource(Source):
    """Reads ADRs from the ``jph`` repo's ``locales/en/examples/`` tree.

    Each example is a directory with a ``README.md``.  We parse the
    markdown, split on ``^#`` / ``^##`` headings, and emit one
    message per section.
    """

    source_id = "adr_jph"
    license = "CC-BY-NC-SA-4.0"
    description = (
        "Architecture Decision Records — 42 English-language ADR "
        "examples curated by Joel Parker Henderson.  Each ADR follows "
        "one of several templates (Nygard, MADR, Planguage, arc42)."
    )
    source_url = "https://github.com/joelparkerhenderson/architecture-decision-record"

    _HEADING_RE = re.compile(r"^(#+)\s+(.*?)\s*$", re.MULTILINE)
    _DATE_RE = re.compile(
        r"(?i)decision\s+date\s*:\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    )
    _STATUS_RE = re.compile(r"(?i)^status\s*:\s*(.+?)$", re.MULTILINE)

    def iter_documents(self, src_dir: Path) -> Iterator[dict]:
        examples_root = src_dir / "locales" / "en" / "examples"
        if not examples_root.exists():
            logger.error("ADR examples not found at %s", examples_root)
            return
        commit = _git_head(src_dir)
        for d in sorted(examples_root.iterdir()):
            if not d.is_dir():
                continue
            readme = d / "README.md"
            if not readme.exists():
                readme = d / "index.md"
            if not readme.exists():
                continue

            text = readme.read_text(encoding="utf-8", errors="replace")
            title = d.name.replace("-", " ").title()

            # Try to extract decision date + status from frontmatter-style metadata
            date_match = self._DATE_RE.search(text)
            observed_at = self._parse_date(date_match.group(1)) if date_match else None
            status_match = self._STATUS_RE.search(text)
            status = status_match.group(1).strip() if status_match else ""

            sections = self._split_sections(text)
            if not sections:
                continue

            subject = f"sdev-{self.source_id}-{_slugify(d.name)}"
            messages: list[dict] = []
            for i, (heading, body) in enumerate(sections):
                messages.append(
                    {
                        "message_id": f"{subject}::sec-{i:02d}",
                        "text": body.strip()[:4000],
                        "section": heading or "body",
                        "observed_at": observed_at,
                        "source": "adr_section",
                        "source_set": self.source_id,
                        "source_url": (
                            f"{self.source_url}/blob/{commit or 'main'}/"
                            f"locales/en/examples/{d.name}/README.md"
                        ),
                        "license": self.license,
                        "source_commit": commit,
                        "retrieved_at": datetime.now(tz=UTC).isoformat(),
                        "adr_status": status,
                    }
                )

            yield {
                "subject": subject,
                "title": title,
                "messages": messages,
                "metadata": {
                    "source_set": self.source_id,
                    "source_url": (
                        f"{self.source_url}/tree/{commit or 'main'}/locales/en/examples/{d.name}"
                    ),
                    "license": self.license,
                    "source_commit": commit,
                    "retrieved_at": datetime.now(tz=UTC).isoformat(),
                    "adr_status": status,
                },
            }

    @staticmethod
    def _split_sections(text: str) -> list[tuple[str, str]]:
        """Split markdown into (heading, body) pairs.  Body text under
        the top-level H1 becomes the "preamble" section."""
        # Strip code fences first so ```python ...``` doesn't count as headings
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        parts: list[tuple[str, str]] = []
        last_pos = 0
        last_heading = ""
        for m in AdrJosephParkerHendersonSource._HEADING_RE.finditer(text):
            body = text[last_pos : m.start()].strip()
            if body:
                parts.append((last_heading, body))
            last_heading = m.group(2).strip()
            last_pos = m.end()
        tail = text[last_pos:].strip()
        if tail:
            parts.append((last_heading, tail))
        # Drop trivially short sections (< 40 chars of body)
        return [(h, b) for h, b in parts if len(b) >= 40]

    @staticmethod
    def _parse_date(raw: str) -> str | None:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                continue
        return None


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------


SOURCES: dict[str, Source] = {
    "adr_jph": AdrJosephParkerHendersonSource(),
    # TODO: adr_log4brains, rfc_ietf, post_mortems, threat_models
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def mine_source(
    source_id: str,
    src_dir: Path,
    out_dir: Path,
) -> dict:
    """Run one source.  Returns a stats dict for the provenance manifest."""
    source = SOURCES[source_id]
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "source_id": source_id,
        "license": source.license,
        "source_url": source.source_url,
        "description": source.description,
        "src_dir": str(src_dir),
        "src_dir_exists": src_dir.exists(),
        "source_commit": _git_head(src_dir) if src_dir.exists() else "",
        "documents_kept": 0,
        "messages": 0,
        "per_section": {},
        "retrieved_at": datetime.now(tz=UTC).isoformat(),
    }
    if not src_dir.exists():
        logger.error("%s: src_dir not found: %s", source_id, src_dir)
        return stats

    for doc in source.iter_documents(src_dir):
        subject = doc["subject"]
        out_path = out_dir / f"{subject}.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "_meta": doc.get("metadata", {}),
                        "_subject": subject,
                        "_title": doc.get("title", ""),
                    },
                    ensure_ascii=False,
                )
            )
            fh.write("\n")
            for msg in doc["messages"]:
                fh.write(json.dumps(msg, ensure_ascii=False))
                fh.write("\n")
                sec = msg.get("section", "body")
                stats["per_section"].setdefault(sec, 0)
                stats["per_section"][sec] += 1
        stats["documents_kept"] += 1
        stats["messages"] += len(doc["messages"])

    logger.info(
        "source=%s kept=%d messages=%d",
        source_id,
        stats["documents_kept"],
        stats["messages"],
    )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="MSEB-SoftwareDev miner: ADR / RFC / post-mortem → raw messages",
    )
    ap.add_argument("--source", choices=list(SOURCES), help="Single source to mine.")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Mine every registered source.  Requires per-source "
        "--src-dir-<source_id> arguments (or env vars).",
    )
    ap.add_argument(
        "--src-dir",
        type=Path,
        default=None,
        help="Path to the source's root (git clone / download).",
    )
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.source and not args.all:
        ap.error("--source or --all required")

    sources_manifest: dict[str, dict] = {}
    total_stats = {"documents": 0, "messages": 0}

    ids = [args.source] if args.source else list(SOURCES)
    for sid in ids:
        # Per-source src_dir resolution — simple default for one source,
        # extensible for --all.
        src_dir = args.src_dir or Path(f"/tmp/{sid}")
        stats = mine_source(sid, src_dir, args.out_dir)
        sources_manifest[sid] = stats
        total_stats["documents"] += stats["documents_kept"]
        total_stats["messages"] += stats["messages"]

    (args.out_dir / "_sources.json").write_text(
        json.dumps(sources_manifest, indent=2, sort_keys=True),
    )
    (args.out_dir / "_stats.json").write_text(
        json.dumps(
            {
                **total_stats,
                "sources": list(sources_manifest),
                "retrieved_at": datetime.now(tz=UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        ),
    )
    print(json.dumps({**total_stats, "sources": list(sources_manifest)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
