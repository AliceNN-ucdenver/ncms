"""MSEB gold-query authoring helper.

Produces hand-reviewable gold.yaml candidates from a labeled
corpus.  The idea is the same pattern BEIR uses: you don't
author queries from scratch, you author *query templates per
intent shape* and anchor each generated query at a real corpus
memory.  The author then reviews / edits the candidates before
shipping them.

Two entry points:

- :func:`generate_candidates` — pure-Python candidate generator
  given labeled memories + per-domain templates.  Returns a list
  of dicts shaped like a ``GoldQuery``.
- CLI — wires domain → template module → labeled JSONL → YAML.

Template modules live alongside each domain labeler:
``benchmarks/mseb_<domain>/gold_templates.py``.  They export a
``TEMPLATES`` dict mapping ``(shape, MemoryKind)`` → list of
query-template strings that reference ``{title}`` / ``{entity}``
/ ``{subject}`` placeholders.  The generator fills the
placeholders from the mined memory's content + metadata.

The generator is intentionally *conservative*: it only emits
candidates where it has high confidence the gold is in the
labeled memory (e.g. ``origin`` → memory tagged
``ordinal_anchor`` within a subject chain).  The author still
reviews every row before commit.

Usage::

    uv run python -m benchmarks.mseb.gold_author \\
        --domain swe \\
        --labeled-dir benchmarks/mseb_swe/raw_labeled \\
        --out        benchmarks/mseb_swe/gold_candidates.yaml \\
        --max-per-shape 30
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("mseb.gold_author")


# ---------------------------------------------------------------------------
# Labeled-memory loading
# ---------------------------------------------------------------------------


def load_labeled_memories(labeled_dir: Path) -> list[dict]:
    """Read every ``*.jsonl`` under ``labeled_dir`` into a flat list."""
    rows: list[dict] = []
    for jsonl in sorted(labeled_dir.glob("*.jsonl")):
        if jsonl.name.startswith("_"):
            continue
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def group_by_subject(rows: list[dict]) -> dict[str, list[dict]]:
    """Group labeled memories into subject chains."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["subject"]].append(r)
    # Preserve observed_at ordering within each chain.
    for k in out:
        out[k].sort(key=lambda r: (r.get("observed_at", ""), r.get("mid", "")))
    return dict(out)


# ---------------------------------------------------------------------------
# Helpers that every template module can use
# ---------------------------------------------------------------------------


def first_sentence(text: str, max_chars: int = 200) -> str:
    """Extract the first real sentence (for query phrasing).  Cheap
    regex — avoids pulling a sentence tokenizer for author tooling."""
    text = text.replace("\n", " ").strip()
    m = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    out = m[0] if m else text
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0]
    return out.strip()


def short_title(text: str, max_chars: int = 80) -> str:
    """Single-line title-ish fragment.  Uses the first code-free line
    of the content; degrades to the first N chars."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("```", "diff ", "@@", "+++", "---")):
            continue
        if len(line) > max_chars:
            return line[:max_chars].rsplit(" ", 1)[0]
        return line
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------


def generate_candidates(
    labeled_memories: list[dict],
    *,
    domain: str,
    templates: dict[str, list[dict[str, Any]]],
    max_per_shape: int = 30,
    max_noise: int = 20,
    seed: int = 42,
) -> list[dict]:
    """Generate gold-query candidates.

    ``templates`` is the per-domain template catalogue:

        {
          "current_state": [
             {
               "text_template": "What is the current state of {title}?",
               "gold_kind": "retirement",
               "gold_source_filter": ["patch", "final_diagnosis"],
               "preference": "none",
             }, …
          ],
          …
        }

    Each entry declares how to find the gold memory:
    ``gold_kind`` filters by ``metadata.kind``; ``gold_source_filter``
    (optional) further filters by ``metadata.source``.

    Returns a list of gold dicts (shape compatible with
    ``benchmarks/mseb/build.py``'s gold.yaml reader).
    """
    rng = random.Random(seed)
    by_subject = group_by_subject(labeled_memories)

    candidates: list[dict] = []
    shape_counts: dict[str, int] = defaultdict(int)

    subjects = list(by_subject.keys())
    rng.shuffle(subjects)

    for subject in subjects:
        chain = by_subject[subject]
        if not chain:
            continue
        for shape, shape_templates in templates.items():
            if shape == "noise":
                continue  # handled below
            if shape_counts[shape] >= max_per_shape:
                continue
            for tpl in shape_templates:
                gold = _pick_gold(chain, tpl)
                if gold is None:
                    continue
                qid = (
                    f"{domain}-{shape}-{shape_counts[shape]+1:03d}"
                )
                # Optional: pull the human-readable title from a
                # different memory in the chain (e.g. SWE patch
                # queries phrase themselves with the issue body's
                # prose title, not raw diff text).
                title_source = tpl.get("title_from_source")
                title_src_memory = gold
                if title_source:
                    for m in chain:
                        if m.get("metadata", {}).get("source") in title_source:
                            title_src_memory = m
                            break
                title = short_title(title_src_memory["content"])
                first = first_sentence(title_src_memory["content"])
                text = tpl["text_template"].format(
                    title=title,
                    first_sentence=first,
                    subject=subject,
                    entity=tpl.get("entity_placeholder", ""),
                )
                # Optional ordinal-anchor alternate (the first
                # memory in the chain is frequently an acceptable
                # alternate for origin/ordinal_first style queries).
                alt_mids = [
                    m["mid"] for m in chain
                    if m["mid"] != gold["mid"]
                    and m.get("metadata", {}).get("kind") == gold.get("metadata", {}).get("kind")
                ][:1]
                candidates.append({
                    "qid": qid,
                    "shape": shape,
                    "text": text,
                    "subject": subject,
                    "gold_mid": gold["mid"],
                    "gold_alt": alt_mids,
                    "preference": tpl.get("preference", "none"),
                    "note": tpl.get("note", ""),
                })
                shape_counts[shape] += 1
                if shape_counts[shape] >= max_per_shape:
                    break  # inner shape_templates loop

    # Noise queries — pull from a different subject, gold_mid="".
    noise_templates = templates.get("noise", [])
    if noise_templates:
        noise_count = 0
        for subject in subjects:
            if noise_count >= max_noise:
                break
            for tpl in noise_templates:
                text = tpl["text_template"].format(
                    title="", first_sentence="", subject=subject, entity="",
                )
                candidates.append({
                    "qid": f"{domain}-noise-{noise_count+1:03d}",
                    "shape": "noise",
                    "text": text,
                    "subject": subject,
                    "gold_mid": "",   # deliberately no gold
                    "gold_alt": [],
                    "preference": "none",
                    "note": "adversarial / off-topic; all top-5 should miss",
                })
                noise_count += 1
                if noise_count >= max_noise:
                    break

    logger.info(
        "generated %d candidates across %d shapes (%s)",
        len(candidates), len(shape_counts), dict(shape_counts),
    )
    return candidates


def _pick_gold(chain: list[dict], tpl: dict[str, Any]) -> dict | None:
    """Find a memory in the chain that satisfies the template's filters."""
    want_kind = tpl.get("gold_kind")
    want_sources = set(tpl.get("gold_source_filter") or [])
    want_pref = tpl.get("gold_preference")
    for m in chain:
        meta = m.get("metadata", {})
        if want_kind and meta.get("kind") != want_kind:
            continue
        if want_sources and meta.get("source") not in want_sources:
            continue
        if want_pref and meta.get("preference") != want_pref:
            continue
        return m
    return None


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


def dump_yaml(candidates: list[dict], path: Path, *, domain: str) -> None:
    """Write a human-readable YAML for author review.

    Uses pyyaml when available; falls back to a hand-formatted
    YAML-ish markdown that YAML parsers still accept.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# MSEB gold query candidates — domain: {domain}\n"
        f"# Generated by benchmarks/mseb/gold_author.py.  Review each row\n"
        f"# before shipping: the text_templates are conservative but\n"
        f"# surface form still benefits from a human pass.\n\n"
    )
    try:
        import yaml
        body = yaml.safe_dump(candidates, sort_keys=False, allow_unicode=True)
    except ImportError:  # pragma: no cover
        body = json.dumps(candidates, indent=2, ensure_ascii=False)
    path.write_text(header + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Template loading per domain
# ---------------------------------------------------------------------------


def load_templates(domain: str) -> dict[str, list[dict[str, Any]]]:
    """Import ``benchmarks.mseb_<domain>.gold_templates`` and return its TEMPLATES."""
    import importlib
    module = importlib.import_module(f"benchmarks.mseb_{domain}.gold_templates")
    templates = getattr(module, "TEMPLATES", None)
    if templates is None:
        raise RuntimeError(
            f"benchmarks.mseb_{domain}.gold_templates missing TEMPLATES export",
        )
    return templates


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="MSEB gold authoring: labeled corpus → gold.yaml candidates",
    )
    ap.add_argument("--domain", required=True,
                    choices=["swe", "clinical", "convo"])
    ap.add_argument("--labeled-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-per-shape", type=int, default=30)
    ap.add_argument("--max-noise", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    templates = load_templates(args.domain)
    memories = load_labeled_memories(args.labeled_dir)
    logger.info(
        "loaded %d labeled memories across %d subjects",
        len(memories), len({m['subject'] for m in memories}),
    )

    candidates = generate_candidates(
        memories,
        domain=args.domain,
        templates=templates,
        max_per_shape=args.max_per_shape,
        max_noise=args.max_noise,
        seed=args.seed,
    )
    dump_yaml(candidates, args.out, domain=args.domain)
    print(json.dumps({
        "domain": args.domain,
        "candidates": len(candidates),
        "out": str(args.out),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
