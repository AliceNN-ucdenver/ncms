"""Phase 2b: generate fresh queries filling CTLG cue-family gaps.

E1 on the 485-row relabeled corpus confirmed zero-occurrence gaps:

  * MODAL_HYPOTHETICAL   — "would have" / "if not for" / counterfactual
  * ASK_CHANGE           — "what changed" / "what happened to"
  * ORDINAL_NTH          — "2nd" / "third"
  * TEMPORAL_SINCE       — "since" (temporal sense) / "as of"
  * TEMPORAL_ANCHOR      — concrete dates / named periods (sparse, 12 tags)
  * SUBJECT              — subject-voice queries (sparse, 4 tags)

This script prompts Spark Nemotron to:
  1. Generate N natural-language queries per gap bucket
  2. Tag each token with BIO cue labels in the same pass

Output is appended to ``gold_cues_software_dev.jsonl`` so v8
training sees the full distribution.

Usage::

    uv run python scripts/ctlg/gen_gap_queries.py \\
        --per-bucket 75 --log-every 5
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

from ncms.domain.tlg.cue_taxonomy import CUE_LABELS  # noqa: E402
from ncms.infrastructure.llm.caller import call_llm_json  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gen_gap_queries")


# =====================================================================
# Buckets: one per missing cue family
# =====================================================================

BUCKETS: dict[str, dict] = {
    "modal_counterfactual": {
        "description": (
            "counterfactual / hypothetical — 'what would X be if...', "
            "'had we kept Y', 'if not for Z', 'would we have...'"
        ),
        "example_queries": [
            "What would we be using if we hadn't switched to Yugabyte?",
            "Had the audit not happened, would we still be on CockroachDB?",
            "If not for the outage, would we have migrated to Postgres?",
            "What could we have used instead of Kubernetes?",
            "Suppose we'd stayed with Heroku — where would we be now?",
        ],
        "required_cues": ["B-MODAL_HYPOTHETICAL"],
    },
    "ask_change": {
        "description": (
            "questions about TRANSITIONS / TRANSFORMATIONS / "
            "historical changes — 'what changed', 'what happened to X', "
            "'describe the migration', 'tell me the transition'"
        ),
        "example_queries": [
            "What changed about our authentication stack last year?",
            "What happened to our Redis cluster?",
            "Tell me what changed when we adopted microservices.",
            "Describe the database migration in 2023.",
            "What changed in our CI tooling recently?",
        ],
        "required_cues": ["B-ASK_CHANGE"],
    },
    "temporal_since": {
        "description": (
            "queries with TEMPORAL_SINCE markers — 'since', 'as of', "
            "'ever since', 'from X onward'"
        ),
        "example_queries": [
            "What have we been running since Q2?",
            "What's our database as of the last release?",
            "Ever since we adopted Kubernetes, what's been our stack?",
            "From Q3 onward, which framework have we standardized on?",
            "Since the migration, what's been current?",
        ],
        "required_cues": ["B-TEMPORAL_SINCE"],
    },
    "temporal_anchor": {
        "description": (
            "queries grounded in concrete dates or named periods — "
            "'in 2023', 'Q2', 'last sprint', 'during the monolith era'"
        ),
        "example_queries": [
            "What database did we run during 2023?",
            "Which framework was current in Q2 2024?",
            "What tools were we using last sprint?",
            "What ran during the monolith era?",
            "What was the stack in the pre-Kubernetes phase?",
        ],
        "required_cues": ["B-TEMPORAL_ANCHOR"],
    },
    "subject_voice": {
        "description": (
            "queries naming a specific SUBJECT (a service or product) — "
            "'auth-service uses X', 'what does the billing-service run on', "
            "'user-api's database'"
        ),
        "example_queries": [
            "What does the auth-service use for its database?",
            "Which framework powers the billing-service?",
            "What CI tool does the notification-service rely on?",
            "The user-api's data store — what is it?",
            "Tell me what the payment service runs on.",
        ],
        "required_cues": ["B-SUBJECT"],
    },
    "ordinal_nth": {
        "description": (
            "queries about a SPECIFIC ordinal position beyond first/last — "
            "'the second database', 'our third framework', '2nd migration'"
        ),
        "example_queries": [
            "What was our second database choice?",
            "The third framework we tried — which was it?",
            "Which 2nd-generation tool replaced the first?",
            "The fourth architecture we considered — what was it?",
            "What was our 3rd deployment platform?",
        ],
        "required_cues": ["B-ORDINAL_NTH"],
    },
}


def _label_vocab_block() -> str:
    return "\n".join(f"  {label}" for label in CUE_LABELS)


CUE_GUIDELINES_BLOCK = """\
Cue families (tag AGGRESSIVELY — most queries have 2-5 non-O tokens):

  CAUSAL_EXPLICIT   = because, due to, since(causal), given that, owing to
  CAUSAL_ALTLEX    = led to, resulted in, drove, caused, motivated
  TEMPORAL_BEFORE  = before, prior to, ahead of, predated, until
  TEMPORAL_AFTER   = after, following, once, subsequently
  TEMPORAL_DURING  = during, while, amid, throughout
  TEMPORAL_SINCE   = since(temporal), as of, ever since, from X on
  TEMPORAL_ANCHOR  = 2023, Q2, last sprint, yesterday, the monolith era
  ORDINAL_FIRST    = first, initial, earliest, original, introduction, opens
  ORDINAL_LAST     = last, latest, final, most recent, closing, summary
  ORDINAL_NTH      = second, third, 2nd, 3rd, fourth, 4th
  MODAL_HYPOTHETICAL = would have, could have, if not for, had we, suppose
  ASK_CURRENT      = now, currently, today, at present, right now
  ASK_CHANGE       = what changed, what happened, the transition, the migration
  REFERENT         = catalog entity names (Postgres, React, Kubernetes, …)
  SUBJECT          = state-evolving entity (auth-service, user-api, payment service)
  SCOPE            = slot words (database, framework, library, tool, platform)

Disambiguation:
  - "since" defaults to TEMPORAL_SINCE unless "because" fits the sense.
  - Multi-word cues are ONE span (B- + I-); don't split "led to" into two B-.
  - Whole-word tokenization; no wordpiece splits.
"""


def _build_prompt(bucket_name: str, bucket: dict, n: int) -> str:
    examples = "\n".join(f"  - {q}" for q in bucket["example_queries"])
    required = ", ".join(bucket["required_cues"])
    return f"""You are generating + cue-labeling training data for a software-dev
memory retrieval system.

BUCKET: {bucket_name}
CATEGORY: {bucket["description"]}

Example queries that fit this category:
{examples}

REQUIRED: each query MUST contain at least one cue of type {required}.
This is the whole point of generating this batch.

{CUE_GUIDELINES_BLOCK}

Label vocabulary (BIO format):
{_label_vocab_block()}

Generate EXACTLY {n} diverse, natural queries for this category, AND
label each token with a BIO cue label.  Return ONLY a JSON array
where each element matches this schema:

  {{
    "text": "<query text>",
    "tokens": [
      {{"surface": "<word>", "char_start": <int>, "char_end": <int>,
        "cue_label": "<one of the BIO labels above>"}}
    ]
  }}

Tokenize at whitespace + punctuation boundaries.  char_start/char_end
must satisfy text[char_start:char_end] == surface.  No markdown fences.
"""


async def generate_bucket(
    bucket_name: str, bucket: dict, *,
    n: int, model: str, api_base: str,
) -> list[dict]:
    """Generate + label N queries for one gap bucket."""
    prompt = _build_prompt(bucket_name, bucket, n)
    try:
        result = await call_llm_json(
            prompt=prompt, model=model, api_base=api_base,
            max_tokens=8000, temperature=0.7,
        )
    except Exception as exc:
        log.warning("bucket=%s: LLM failed: %s", bucket_name, exc)
        return []
    # Accept both top-level list and {"queries": [...]} wrapping.
    items = result if isinstance(result, list) else (
        result.get("queries") if isinstance(result, dict) else None
    )
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        tokens = item.get("tokens")
        if not isinstance(text, str) or not isinstance(tokens, list):
            continue
        # Validate + normalize tokens.
        clean_tokens: list[dict] = []
        for t in tokens:
            try:
                label = t["cue_label"]
                if label not in CUE_LABELS:
                    continue
                clean_tokens.append({
                    "char_start": int(t["char_start"]),
                    "char_end": int(t["char_end"]),
                    "surface": str(t["surface"]),
                    "cue_label": label,
                    "confidence": 1.0,
                })
            except (KeyError, TypeError, ValueError):
                continue
        if not clean_tokens:
            continue
        out.append({
            "text": text,
            "domain": "software_dev",
            "voice": "query",
            "tokens": clean_tokens,
            "source": f"spark_ctlg_phase2b bucket={bucket_name}",
            "split": "gold",
            "legacy_shape_intent": None,
        })
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-bucket", type=int, default=75)
    parser.add_argument(
        "--model",
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
    )
    parser.add_argument(
        "--api-base",
        default="http://spark-ee7d.local:8000/v1",
    )
    parser.add_argument(
        "--output",
        default="adapters/corpora/ctlg/gold_cues_software_dev.jsonl",
    )
    parser.add_argument("--log-every", type=int, default=1)
    args = parser.parse_args()

    out_path = _REPO / args.output
    assert out_path.exists(), f"expected target file at {out_path}"

    log.info(
        "generating %d queries/bucket across %d buckets -> %s",
        args.per_bucket, len(BUCKETS), out_path,
    )

    total_appended = 0
    with out_path.open("a") as outf:
        for bucket_name, bucket in BUCKETS.items():
            log.info("bucket=%s: generating %d...", bucket_name, args.per_bucket)
            items = await generate_bucket(
                bucket_name, bucket,
                n=args.per_bucket,
                model=args.model, api_base=args.api_base,
            )
            for item in items:
                outf.write(json.dumps(item) + "\n")
            log.info(
                "bucket=%s: wrote %d (requested %d)",
                bucket_name, len(items), args.per_bucket,
            )
            total_appended += len(items)

    log.info(
        "done: appended %d rows across %d buckets to %s",
        total_appended, len(BUCKETS), out_path,
    )


if __name__ == "__main__":
    asyncio.run(main())
