"""MSEB-Clinical miner — NCBI PMC Open Access → message tuples.

Phase 1 of the MSEB-Clinical pipeline.  Queries NCBI eutils for
CC-BY case reports with explicit differential-diagnosis /
diagnostic-error MeSH tags, fetches the full XML, extracts
narrative sections, and emits one **raw message JSONL** per
paper.

Only papers in the **PMC Open Access Subset** are fetched — the
``open access[filter]`` qualifier in the esearch query restricts
to CC-BY / CC-0 / equivalent.  Full-text fetch uses the ``pmc``
database's ``rettype=xml`` endpoint.  See
``benchmarks/mseb_clinical/README.md`` §3 for the fetch policy
(rate limit, throttle, error handling) and §4 for the search
pattern catalogue.

Output layout::

    raw/
    ├── PMC1234567.xml      ← full JATS XML (cached for re-runs)
    ├── PMC1234567.jsonl    ← message tuples extracted from sections
    ├── _esearch.json       ← raw esearch response (PMCID list + counts)
    └── _stats.json         ← mining summary

Rate limits (NCBI eutils policy):
    - 3 requests/second without an API key.
    - 10 requests/second with ``NCBI_API_KEY`` in env.
    - Batch efetch up to 200 IDs per call.

Usage::

    # pilot — 50 papers
    uv run python -m benchmarks.mseb_clinical.mine --limit 50

    # full scale — 200 papers
    uv run python -m benchmarks.mseb_clinical.mine --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    raise RuntimeError(
        "MSEB-clinical miner requires the `requests` package",
    ) from None

try:
    from benchmarks.env import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger("mseb_clinical.mine")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_OUT = Path(__file__).parent / "raw"

# ---------------------------------------------------------------------------
# Search query — see README §4
# ---------------------------------------------------------------------------

# MeSH + publication-type + open-access filter.  Restricts to CC-BY /
# CC-0 English case reports about differential diagnosis or diagnostic
# errors — the publications whose narrative structure IS the
# state-evolution arc we're trying to evaluate.
DEFAULT_QUERY = (
    '("Diagnosis, Differential"[MeSH Terms] '
    'OR "Diagnostic Errors"[MeSH Terms]) '
    'AND "Case Reports"[Publication Type] '
    'AND "open access"[filter] '
    "AND English[Language]"
)

# Narrative-section headings we surface as separate messages.  JATS
# section names aren't standardised, so we match case-insensitively
# on substring.  Order matches the clinical case-report arc.
SECTION_PRIORITY = (
    "abstract",
    "introduction",
    "background",
    "case presentation",
    "case report",
    "presentation",
    "history",
    "physical examination",
    "investigations",
    "workup",
    "differential diagnosis",
    "initial diagnosis",
    "management",
    "treatment",
    "course",
    "outcome",
    "follow-up",
    "final diagnosis",
    "conclusion",
    "discussion",
)


# ---------------------------------------------------------------------------
# HTTP helpers with rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple per-second request pacer for eutils policy."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        if dt < self._interval:
            time.sleep(self._interval - dt)
        self._last = time.monotonic()


def _get(
    session: requests.Session,
    url: str,
    params: dict,
    rate: RateLimiter,
    retries: int = 3,
) -> requests.Response:
    """GET with retries + rate limiting.  Returns the Response."""
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params = {**params, "api_key": api_key}
    params = {
        **params,
        "email": os.environ.get(
            "NCBI_EMAIL",
            "mseb@example.com",
        ),
    }

    last_exc: Exception | None = None
    for attempt in range(retries):
        rate.wait()
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            last_exc = exc
            # Retry on 429 (rate limit) + 5xx
            if resp.status_code in (429, 500, 502, 503, 504):
                logger.warning(
                    "%s → %d on attempt %d — backing off",
                    url,
                    resp.status_code,
                    attempt + 1,
                )
                time.sleep(2**attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"GET {url} failed after {retries} attempts")


# ---------------------------------------------------------------------------
# esearch → PMCID list
# ---------------------------------------------------------------------------


def esearch(
    session: requests.Session,
    *,
    query: str,
    retmax: int,
    rate: RateLimiter,
) -> list[str]:
    """Query PMC for matching PMCIDs.  Returns the ID list."""
    logger.info("esearch db=pmc retmax=%d query=%s", retmax, query)
    resp = _get(
        session,
        f"{EUTILS}/esearch.fcgi",
        params={
            "db": "pmc",
            "term": query,
            "retmode": "json",
            "retmax": str(retmax),
            "sort": "pub+date",
        },
        rate=rate,
    )
    data = resp.json()
    count = int(data.get("esearchresult", {}).get("count", "0"))
    ids: list[str] = data.get("esearchresult", {}).get("idlist", [])
    logger.info("esearch → %d matching, returning first %d", count, len(ids))
    return ids


# ---------------------------------------------------------------------------
# efetch → full JATS XML
# ---------------------------------------------------------------------------


def efetch(
    session: requests.Session,
    *,
    pmcid: str,
    out_xml: Path,
    rate: RateLimiter,
) -> bytes:
    """Fetch one paper's JATS XML.  Cached on disk."""
    if out_xml.exists():
        return out_xml.read_bytes()
    resp = _get(
        session,
        f"{EUTILS}/efetch.fcgi",
        params={
            "db": "pmc",
            "id": pmcid,
            "rettype": "xml",
        },
        rate=rate,
    )
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    out_xml.write_bytes(resp.content)
    return resp.content


# ---------------------------------------------------------------------------
# JATS XML → message tuples
# ---------------------------------------------------------------------------


def _clean_text(el: ET.Element) -> str:
    """Flatten inline elements into plain text (preserving whitespace)."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_clean_text(child))
        if child.tail:
            parts.append(child.tail)
    txt = " ".join(p.strip() for p in parts if p and p.strip())
    return " ".join(txt.split())


def _pub_date(root: ET.Element) -> str:
    """Extract the publication date as ISO-8601.  Best-effort."""
    for pub in root.iter("pub-date"):
        year = pub.findtext("year") or ""
        month = pub.findtext("month") or "01"
        day = pub.findtext("day") or "01"
        if year:
            try:
                dt = datetime(int(year), int(month), int(day), tzinfo=UTC)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                continue
    return "1970-01-01T00:00:00Z"


def _license_ok(root: ET.Element) -> tuple[bool, str]:
    """Return (allowed, license_string).  Only CC-BY-* counts as OK."""
    lic = None
    for el in root.iter("license"):
        href = el.get(
            "{http://www.w3.org/1999/xlink}href",
            "",
        ) or el.get("license-type", "")
        if href:
            lic = href
            break
        txt = _clean_text(el)
        if "Creative Commons" in txt or "CC BY" in txt.upper():
            lic = txt[:200]
            break
    lic = lic or ""
    allowed = any(
        tag in lic.lower()
        for tag in ("cc-by", "cc by", "by/4.0", "by/3.0", "creativecommons.org/licenses/by")
    )
    return allowed, lic


def xml_to_messages(pmcid: str, xml_bytes: bytes) -> dict:
    """Parse JATS XML into a structured message set.

    Returns a dict with ``messages`` (list) + ``metadata`` (dict).
    Skips + flags papers that fail license / structure checks.
    """
    out: dict = {
        "pmcid": pmcid,
        "messages": [],
        "metadata": {"license_ok": False, "skipped_reason": None},
    }
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        out["metadata"]["skipped_reason"] = f"xml_parse_error: {exc}"
        return out

    # Only keep CC-BY content — respect redistributability.
    allowed, lic = _license_ok(root)
    out["metadata"]["license"] = lic
    out["metadata"]["license_ok"] = allowed
    if not allowed:
        out["metadata"]["skipped_reason"] = "non_cc_by_license"
        return out

    title = (
        _clean_text(
            next(iter(root.iter("article-title")), ET.Element("x")),
        )
        or f"PMC{pmcid}"
    )
    out["metadata"]["title"] = title[:400]
    out["metadata"]["pub_date"] = _pub_date(root)

    # Walk <sec> elements, prioritised by heading.
    subject = f"pmc{pmcid}"
    msg_seq = 0
    base_ts = out["metadata"]["pub_date"]

    # Abstract first (if present).
    for abstract in root.iter("abstract"):
        txt = _clean_text(abstract)
        if txt:
            msg_seq += 1
            out["messages"].append(
                {
                    "message_id": f"{subject}::abstract",
                    "text": txt[:4000],
                    "timestamp": base_ts,
                    "source": "abstract",
                    "section_title": "Abstract",
                }
            )
        break

    # Then body sections.  JATS puts narrative under <body><sec>.
    for sec in root.iter("sec"):
        title_el = sec.find("title")
        heading = _clean_text(title_el) if title_el is not None else ""
        if not heading:
            continue
        text = _clean_text(sec)
        if len(text) < 40:
            continue
        # Lowercased substring match against our priority list;
        # unmatched headings still land but under a generic source.
        heading_lower = heading.lower()
        matched = next(
            (s for s in SECTION_PRIORITY if s in heading_lower),
            "other",
        )
        msg_seq += 1
        out["messages"].append(
            {
                "message_id": f"{subject}::sec-{msg_seq:02d}",
                "text": text[:4000],
                "timestamp": base_ts,
                "source": matched,
                "section_title": heading[:200],
            }
        )

    return out


# ---------------------------------------------------------------------------
# Top-level mine orchestration
# ---------------------------------------------------------------------------


def mine(
    *,
    limit: int,
    out_dir: Path,
    query: str = DEFAULT_QUERY,
    rps: float | None = None,
) -> dict:
    """Fetch + extract.  Emits one JSONL per paper + stats."""
    out_dir.mkdir(parents=True, exist_ok=True)
    xml_cache = out_dir / "xml"
    xml_cache.mkdir(exist_ok=True)

    # NCBI rate policy: 3/s without key, 10/s with key.  Give the
    # caller a manual knob if they need to be gentler.
    if rps is None:
        rps = 10.0 if os.environ.get("NCBI_API_KEY") else 3.0
    rate = RateLimiter(rps=rps)

    with requests.Session() as session:
        session.headers["User-Agent"] = "mseb-clinical-miner/0.1"

        ids = esearch(
            session,
            query=query,
            retmax=limit,
            rate=rate,
        )
        (out_dir / "_esearch.json").write_text(
            json.dumps(
                {
                    "query": query,
                    "retmax": limit,
                    "returned": len(ids),
                    "pmcids": ids,
                    "fetched_at": datetime.now(tz=UTC).isoformat(),
                },
                indent=2,
            )
        )

        stats = {
            "pmcids_requested": len(ids),
            "papers_kept": 0,
            "papers_skipped_non_cc_by": 0,
            "papers_skipped_other": 0,
            "messages": 0,
            "per_source": {},
            "skipped_reasons": {},
        }

        for i, pmcid in enumerate(ids):
            try:
                xml_bytes = efetch(
                    session,
                    pmcid=pmcid,
                    out_xml=xml_cache / f"PMC{pmcid}.xml",
                    rate=rate,
                )
            except Exception as exc:
                logger.warning(
                    "efetch PMC%s failed: %s — skipping",
                    pmcid,
                    exc,
                )
                stats["papers_skipped_other"] += 1
                stats["skipped_reasons"].setdefault("efetch_failed", 0)
                stats["skipped_reasons"]["efetch_failed"] += 1
                continue

            parsed = xml_to_messages(pmcid, xml_bytes)
            if parsed["metadata"].get("skipped_reason"):
                reason = parsed["metadata"]["skipped_reason"]
                stats["skipped_reasons"].setdefault(reason, 0)
                stats["skipped_reasons"][reason] += 1
                if reason == "non_cc_by_license":
                    stats["papers_skipped_non_cc_by"] += 1
                else:
                    stats["papers_skipped_other"] += 1
                logger.info(
                    "[%d/%d] PMC%s skipped: %s",
                    i + 1,
                    len(ids),
                    pmcid,
                    reason,
                )
                continue

            # Emit per-paper JSONL.
            out_path = out_dir / f"PMC{pmcid}.jsonl"
            with out_path.open("w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "_meta": parsed["metadata"],
                            "_pmcid": pmcid,
                        },
                        ensure_ascii=False,
                    )
                )
                fh.write("\n")
                for msg in parsed["messages"]:
                    fh.write(json.dumps(msg, ensure_ascii=False))
                    fh.write("\n")
                    stats["per_source"].setdefault(msg["source"], 0)
                    stats["per_source"][msg["source"]] += 1

            stats["papers_kept"] += 1
            stats["messages"] += len(parsed["messages"])

            if (i + 1) % 5 == 0 or (i + 1) == len(ids):
                logger.info(
                    "[%d/%d] PMC%s kept: %d sections (cum: %d papers, %d msgs)",
                    i + 1,
                    len(ids),
                    pmcid,
                    len(parsed["messages"]),
                    stats["papers_kept"],
                    stats["messages"],
                )

    (out_dir / "_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="MSEB-Clinical miner: PMC Open Access → raw messages",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--query", default=DEFAULT_QUERY, help="Override the esearch term (advanced)"
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=None,
        help="Override requests/sec (default: 3/s without NCBI_API_KEY, 10/s with)",
    )
    args = parser.parse_args()

    stats = mine(
        limit=args.limit,
        out_dir=args.out_dir,
        query=args.query,
        rps=args.rps,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
