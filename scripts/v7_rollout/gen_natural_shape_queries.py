"""Generate natural-query shape_intent training data via Spark LLM.

For each of the 12 TLG grammar shapes, prompt the LLM for 25
natural user-voice query phrasings that a real software engineer
would type.  Append the output to
``gold_shape_intent_software_dev.jsonl`` so the shape_intent head
sees both the original 181 template-wrapped rows AND the new
~300 natural phrasings.

Keeps the template rows — removing them would lose coverage of
ADR-style content retrieval where queries do match templates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from ncms.infrastructure.llm.caller import call_llm_json

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gen_shape_queries")

SHAPES: dict[str, tuple[str, list[str]]] = {
    "current_state": (
        "questions asking for the CURRENT / LATEST state or value",
        [
            "What's our current database?",
            "Which framework is in production right now?",
            "What CI tool are we using today?",
        ],
    ),
    "before_named": (
        "questions asking what was used BEFORE a specific named thing",
        [
            "What did we use before Postgres?",
            "Which ORM predated Prisma?",
            "What was the orchestrator before Kubernetes took over?",
        ],
    ),
    "origin": (
        "questions asking about the ORIGIN / motivation / starting point",
        [
            "Why did we originally pick this stack?",
            "What was the first database the auth service used?",
            "How did we end up choosing Postgres?",
        ],
    ),
    "retirement": (
        "questions asking what was DEPRECATED / RETIRED / REPLACED",
        [
            "Which technologies did we retire last quarter?",
            "What did we sunset when we adopted microservices?",
            "Which framework was replaced in the migration?",
        ],
    ),
    "sequence": (
        "questions asking to TRACE / WALK THROUGH the history of decisions",
        [
            "Walk me through our database migration history.",
            "Trace the sequence of auth decisions we've made.",
            "Step through the evolution of our CI pipeline.",
        ],
    ),
    "predecessor": (
        "questions asking what ALTERNATIVES were considered before the final choice",
        [
            "Which alternatives did we evaluate before picking Postgres?",
            "What options were on the shortlist before we chose React?",
            "Which tools did we consider before settling on ruff?",
        ],
    ),
    "transitive_cause": (
        "questions asking what FACTORS / DRIVERS / REASONS led to a decision",
        [
            "What drove the decision to adopt event sourcing?",
            "Why specifically did we pick Postgres over MongoDB?",
            "What justified the move to microservices?",
        ],
    ),
    "causal_chain": (
        "questions asking for the CHAIN OF REASONS, multi-step why",
        [
            "Explain the chain of issues that led us to rewrite auth.",
            "Give me the causal chain behind leaving Heroku.",
            "What sequence of decisions led to our current architecture?",
        ],
    ),
    "concurrent": (
        "questions asking what OTHER decisions / side effects happened alongside",
        [
            "What else changed when we adopted Kubernetes?",
            "Which other services were affected by the Postgres migration?",
            "What concurrent changes rolled out with microservices?",
        ],
    ),
    "interval": (
        "questions about a specific TIME RANGE / period / era",
        [
            "What databases were we running during 2023?",
            "Which tools were in use between the monolith era and now?",
            "What stack did we have during the legacy phase?",
        ],
    ),
    "ordinal_first": (
        "questions about the FIRST / OPENING / INITIAL item",
        [
            "What was our very first framework choice?",
            "Show me the opening context of the database ADR.",
            "What initial concern kicked off the refactor?",
        ],
    ),
    "ordinal_last": (
        "questions about the FINAL / CLOSING / LAST / MOST RECENT item",
        [
            "What was the final decision in the rate-limiting ADR?",
            "Show me the closing summary of the auth migration.",
            "What was the last thing we resolved about deployment?",
        ],
    ),
}


def build_prompt(shape: str, description: str, examples: list[str], n: int) -> str:
    ex = "\n".join(f"  - {e}" for e in examples)
    return f"""You are generating training data for a query-intent classifier.

Generate exactly {n} DIVERSE, NATURAL queries that a real software engineer \
would type or speak when asking their memory system a question.

The intent category is "{shape}" = {description}.

Example queries for this intent:
{ex}

Guidelines:
  - Keep queries concise (4-15 words), natural English, conversational.
  - Vary the phrasing: questions with "what", "which", "how", "when",
    sometimes with ellipsis ("database we use now?"), sometimes typed
    in full ("Tell me which database is in production.").
  - Mix subject matter: databases, frameworks, languages, CI tools,
    deployment platforms, architecture patterns, libraries.
  - No two queries should share a full-sentence template.
  - DON'T wrap queries in "What X in: <ADR>?" meta-templates — write
    them as they'd actually be asked.

Return ONLY a JSON object with one key "queries" whose value is a \
JSON array of {n} strings.  No prose, no markdown fences."""


async def generate_shape(
    shape: str, spec: tuple[str, list[str]],
    *, n: int, model: str, api_base: str | None,
) -> list[dict]:
    desc, examples = spec
    prompt = build_prompt(shape, desc, examples, n)
    try:
        result = await call_llm_json(
            prompt=prompt, model=model, api_base=api_base,
            max_tokens=3000, temperature=0.7,  # temp>0 for diversity
        )
    except Exception as exc:
        log.warning("LLM call failed for shape %s: %s", shape, exc)
        return []
    if not isinstance(result, dict):
        return []
    queries = result.get("queries") or []
    out = []
    seen = set()
    for q in queries:
        if not isinstance(q, str):
            continue
        q = q.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        out.append({
            "text": q,
            "domain": "software_dev",
            "intent": "none",          # query-voice doesn't carry pref intent
            "slots": {},
            "shape_intent": shape,
            "split": "gold",
            "source": f"spark_natural_queries model={model} v7.2",
            "note": "",
        })
    return out


async def main() -> None:
    model = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    api_base = "http://spark-ee7d.local:8000/v1"
    n_per = 25

    out_path = _REPO / "adapters" / "corpora" / "gold_shape_intent_software_dev.jsonl"
    existing = []
    with out_path.open() as f:
        for line in f:
            existing.append(json.loads(line))
    log.info("existing rows in %s: %d", out_path.name, len(existing))

    new_rows: list[dict] = []
    for shape, spec in SHAPES.items():
        batch = await generate_shape(
            shape, spec, n=n_per, model=model, api_base=api_base,
        )
        log.info("shape=%s: generated %d queries", shape, len(batch))
        new_rows.extend(batch)

    # Append only — preserve existing template rows so we get both signals.
    with out_path.open("a") as f:
        for r in new_rows:
            f.write(json.dumps(r) + "\n")
    log.info("appended %d new rows to %s", len(new_rows), out_path)
    log.info("total rows now: %d", len(existing) + len(new_rows))


if __name__ == "__main__":
    asyncio.run(main())
