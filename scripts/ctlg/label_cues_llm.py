"""LLM cue-labeling for CTLG Phase 2 training-data generation.

Given a JSONL of queries or memories, prompt Spark Nemotron to
tag each row with per-token BIO cue labels from the CTLG cue
taxonomy (:mod:`ncms.domain.tlg.cue_taxonomy`).  Output is a
CTLG-schema JSONL (see ``adapters/corpora/ctlg/README.md``).

This script is the Phase 2 training-data bootstrap.  Two modes:

  **relabel mode** — read an existing JSONL with a ``text`` field
  (e.g. the 485 shape_intent gold rows) and LLM-tag each row.

  **generate mode** — ask the LLM for N fresh queries per cue
  family and label them in the same pass.

The pilot pass (100 rows, hand-labeled for annotator-agreement
calibration) is a prerequisite before running this script at scale
— the guidelines doc refinements from the pilot are baked into the
LLM prompt below.

Usage::

    # Relabel existing shape_intent gold
    uv run python scripts/ctlg/label_cues_llm.py relabel \\
        --input adapters/_archive/pre_ctlg/corpora/gold_shape_intent_software_dev.jsonl.pre_v7.2.bak \\
        --output adapters/corpora/ctlg/gold_cues_software_dev.jsonl \\
        --domain software_dev --voice query --limit 485

    # Generate fresh queries
    uv run python scripts/ctlg/label_cues_llm.py generate \\
        --domain software_dev --voice query \\
        --output adapters/corpora/ctlg/gold_cues_software_dev_fresh.jsonl \\
        --per-family 150 --target-families causal,temporal,modal,counterfactual
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.domain.tlg.cue_taxonomy import CUE_LABELS, TaggedToken  # noqa: E402
from ncms.infrastructure.llm.caller import call_llm_json  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("label_cues_llm")


# =====================================================================
# Prompt builders
# =====================================================================


def _label_vocab_block() -> str:
    """Render the BIO cue label list for the LLM prompt."""
    return "\n".join(f"  {label}" for label in CUE_LABELS)


PILOT_GUIDELINES = """\
Cue family summary (tag tokens aggressively — most software-dev queries
have MULTIPLE cue spans):

  CAUSAL_EXPLICIT   = because, due to, since(causal), given that, as(causal), owing to
  CAUSAL_ALTLEX    = led to, resulted in, drove, caused, motivated, the reason,
                     one reason, the motivation, made us, behind (the choice)
  TEMPORAL_BEFORE  = before, prior to, ahead of, predated, until, earlier than
  TEMPORAL_AFTER   = after, following, once, subsequently, succeeded
  TEMPORAL_DURING  = during, while, amid, throughout
  TEMPORAL_SINCE   = since(temporal), as of, ever since, from X on
  TEMPORAL_ANCHOR  = 2023, Q2, last sprint, yesterday, the monolith era
  ORDINAL_FIRST    = first, initial, earliest, original, introduction, opens
  ORDINAL_LAST     = last, latest, final, most recent, current(as ordinal),
                     closing, summary(closing)
  ORDINAL_NTH      = second, third, 2nd, 3rd
  MODAL_HYPOTHETICAL = would have, could have, if not for, had we, suppose, imagine
  ASK_CURRENT      = now, currently, today, at present, right now
  ASK_CHANGE       = what changed, what happened, the transition, the migration(as referent)
  REFERENT         = catalog entity names (Postgres, React, Kubernetes, Bulma, Django, ...)
  SUBJECT          = state-evolving entity (auth-service, user API, the payment service)
  SCOPE            = slot words (database, framework, library, tool, platform, orchestrator)

Disambiguation rules:

  - "since" defaults to TEMPORAL_SINCE unless "because" substitution works.
  - "first thing every morning" is NOT ordinal (it's habitual frequency).
  - Multi-word cues are ONE span (B-LABEL + I-LABEL, never two B-).
  - Dates/anchors are TEMPORAL_ANCHOR regardless of nearby TEMPORAL_SINCE.
  - Whole tokens only — no BERT wordpiece splits.

Here are THREE worked examples with expected labels (study the density):

Example 1:
  Text: "What decision was adopted in: Decided on Bulma. Open to new CSS framework choices."
  Labels:
    What             O
    decision         O
    was              O
    adopted          O
    in               O
    :                O
    Decided          O
    on               O
    Bulma            B-REFERENT
    .                O
    Open             O
    to               O
    new              O
    CSS              B-REFERENT
    framework        B-SCOPE
    choices          O
    .                O

Example 2:
  Text: "What problem motivated the decision to migrate from Postgres to CockroachDB?"
  Labels:
    What             O
    problem          O
    motivated        B-CAUSAL_ALTLEX
    the              O
    decision         O
    to               O
    migrate          O
    from             B-TEMPORAL_BEFORE
    Postgres         B-REFERENT
    to               O
    CockroachDB      B-REFERENT
    ?                O

Example 3:
  Text: "What would our current database be if we hadn't switched to YugabyteDB?"
  Labels:
    What             O
    would            B-MODAL_HYPOTHETICAL
    our              O
    current          B-ASK_CURRENT
    database         B-SCOPE
    be               O
    if               O
    we               O
    hadn             I-MODAL_HYPOTHETICAL
    't               I-MODAL_HYPOTHETICAL
    switched         O
    to               O
    YugabyteDB       B-REFERENT
    ?                O

Note how example 3 has FIVE cue spans across only 14 tokens — tag AGGRESSIVELY.
Most real queries will have 2-5 non-O tokens, not zero.
"""


def build_label_prompt(text: str, voice: str, domain: str) -> str:
    """Single-row cue-labeling prompt."""
    return f"""You are a BIO cue-labeling annotator for CTLG — a causal-temporal \
semantic parser for {domain} memory retrieval.

Given a {voice}-voice text, assign each WORD one BIO cue label from \
this list (use "O" for outside any cue span):

{_label_vocab_block()}

{PILOT_GUIDELINES}

Text ({voice}-voice): {text!r}

Return ONLY a JSON object matching this schema:

  {{
    "tokens": [
      {{"surface": "<word>", "char_start": <int>, "char_end": <int>,
        "cue_label": "<one of the labels above>"}}
    ]
  }}

- Tokenize at whitespace + punctuation boundaries (not BERT wordpieces).
- char_start / char_end are indices into the ORIGINAL text (Python
  slice-compatible: text[char_start:char_end] == surface).
- Each token gets exactly one label.  No prose, no markdown fences.
"""


# =====================================================================
# Core labeling
# =====================================================================


async def label_one(
    text: str,
    *,
    domain: str,
    voice: str,
    model: str,
    api_base: str | None,
) -> list[TaggedToken] | None:
    """Label a single text.  Returns None on LLM failure or invalid output."""
    prompt = build_label_prompt(text, voice=voice, domain=domain)
    try:
        result = await call_llm_json(
            prompt=prompt, model=model, api_base=api_base,
            max_tokens=1500, temperature=0.0,
        )
    except Exception as exc:
        log.warning("LLM call failed for text[:60]=%r: %s", text[:60], exc)
        return None
    if not isinstance(result, dict) or "tokens" not in result:
        return None
    out: list[TaggedToken] = []
    for tok in result.get("tokens") or []:
        try:
            label = tok["cue_label"]
            if label not in CUE_LABELS:
                continue
            out.append(TaggedToken(
                char_start=int(tok["char_start"]),
                char_end=int(tok["char_end"]),
                surface=str(tok["surface"]),
                cue_label=label,
                confidence=1.0,  # LLM-source — flag via row-level provenance
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def token_to_dict(tok: TaggedToken) -> dict:
    return {
        "char_start": tok.char_start,
        "char_end": tok.char_end,
        "surface": tok.surface,
        "cue_label": tok.cue_label,
        "confidence": tok.confidence,
    }


async def relabel_corpus(
    *,
    input_path: Path,
    output_path: Path,
    domain: str,
    voice: str,
    model: str,
    api_base: str | None,
    limit: int | None,
    log_every: int,
) -> int:
    """Read an existing JSONL with ``text`` fields, label each via LLM,
    write out CTLG-schema JSONL.
    """
    rows: list[dict] = []
    with input_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if limit:
        rows = rows[:limit]
    log.info("relabel: %d rows from %s", len(rows), input_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    with output_path.open("w") as outf:
        for i, row in enumerate(rows):
            text = row.get("text")
            if not text:
                continue
            tokens = await label_one(
                text, domain=domain, voice=voice,
                model=model, api_base=api_base,
            )
            if tokens is None:
                continue
            out_row = {
                "text": text,
                "domain": domain,
                "voice": voice,
                "tokens": [token_to_dict(t) for t in tokens],
                "source": f"spark_llm model={model}",
                "split": "gold",
                # Preserve prior classification label for cross-reference
                "legacy_shape_intent": row.get("shape_intent"),
            }
            outf.write(json.dumps(out_row) + "\n")
            n_ok += 1
            if (i + 1) % log_every == 0:
                log.info("  %d / %d labeled (%d emitted)",
                         i + 1, len(rows), n_ok)
    log.info("done: %d rows → %s", n_ok, output_path)
    return n_ok


# =====================================================================
# Generate-mode stub
# =====================================================================


async def generate_fresh(
    *,
    domain: str,
    voice: str,
    output_path: Path,
    per_family: int,
    target_families: list[str],
    model: str,
    api_base: str | None,
) -> int:
    """Generate fresh queries + label in one LLM pass, per cue family.

    TODO: implement in Phase 2 — the prompt design requires the
    cue-guidelines pilot-agreement round first.  Stubbed here so
    the CLI interface is stable; raises NotImplementedError until
    the pilot completes.
    """
    raise NotImplementedError(
        "generate mode lands in Phase 2 after the pilot calibration. "
        "Use relabel mode on the archived shape_intent gold for now.",
    )


# =====================================================================
# CLI
# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM cue-labeling for CTLG training data",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # relabel
    p_rel = sub.add_parser("relabel",
        help="relabel an existing JSONL with `text` fields")
    p_rel.add_argument("--input", required=True, type=Path)
    p_rel.add_argument("--output", required=True, type=Path)
    p_rel.add_argument("--domain", required=True,
        choices=["software_dev", "clinical", "conversational", "swe_diff"])
    p_rel.add_argument("--voice", default="query",
        choices=["query", "memory"])
    p_rel.add_argument("--model",
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16")
    p_rel.add_argument("--api-base",
        default="http://spark-ee7d.local:8000/v1")
    p_rel.add_argument("--limit", type=int, default=None)
    p_rel.add_argument("--log-every", type=int, default=25)

    # generate (stub)
    p_gen = sub.add_parser("generate",
        help="generate fresh queries + labels (Phase 2+)")
    p_gen.add_argument("--domain", required=True)
    p_gen.add_argument("--voice", default="query")
    p_gen.add_argument("--output", required=True, type=Path)
    p_gen.add_argument("--per-family", type=int, default=150)
    p_gen.add_argument("--target-families",
        default="causal,temporal,modal,ordinal,counterfactual")
    p_gen.add_argument("--model",
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16")
    p_gen.add_argument("--api-base",
        default="http://spark-ee7d.local:8000/v1")

    args = parser.parse_args()

    if args.mode == "relabel":
        asyncio.run(relabel_corpus(
            input_path=args.input, output_path=args.output,
            domain=args.domain, voice=args.voice,
            model=args.model, api_base=args.api_base,
            limit=args.limit, log_every=args.log_every,
        ))
    elif args.mode == "generate":
        asyncio.run(generate_fresh(
            domain=args.domain, voice=args.voice,
            output_path=args.output,
            per_family=args.per_family,
            target_families=args.target_families.split(","),
            model=args.model, api_base=args.api_base,
        ))


if __name__ == "__main__":
    main()
