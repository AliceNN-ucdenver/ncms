#!/usr/bin/env python3
"""Structured prompt experiment harness.

Runs the same topic through multiple prompt configurations using real Tavily
searches and Nemotron Nano on DGX Spark. Cached search results ensure all
experiment variants get identical raw input.

Modes:
  - one-shot: Plan 5 queries → search → synthesize (current pipeline)
  - two-stage: Plan 5 queries → search → analyze gaps → plan 3 refined
    queries → search again → synthesize from ALL results

Usage:
    # One-shot, all 4 variants (standard/semiformal x thinking on/off)
    uv run python experiments/structured-prompts/harness.py \
        --topic "Authentication patterns for identity services"

    # Two-stage refinement
    uv run python experiments/structured-prompts/harness.py \
        --topic "Authentication patterns for identity services" \
        --two-stage

    # Single variant for testing
    uv run python experiments/structured-prompts/harness.py \
        --topic "Authentication patterns for identity services" \
        --prompt semiformal --thinking
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

# LLM endpoint
DEFAULT_LLM_MODEL = os.environ.get(
    "LLM_MODEL", "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
)
DEFAULT_LLM_API_BASE = os.environ.get(
    "LLM_API_BASE", "http://spark-ee7d.local:8000/v1"
)
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

RESULTS_DIR = Path(__file__).parent / "results"
CACHE_DIR = Path(__file__).parent / "cache"

# Load prompts
PROMPT_DIR = Path(__file__).parent / "prompts"
NAT_PROMPT_DIR = (
    Path(__file__).resolve().parents[2]
    / "packages" / "nvidia-nat-ncms" / "src" / "nat" / "plugins" / "ncms"
)


def _load_prompt(path: Path, var_name: str) -> str:
    ns: dict = {}
    exec(path.read_text(), ns)
    return ns[var_name]


# ── LLM Call ──────────────────────────────────────────────────────────────────


async def call_llm(
    prompt: str,
    system: str = "You are a helpful assistant.",
    model: str = DEFAULT_LLM_MODEL,
    api_base: str = DEFAULT_LLM_API_BASE,
    max_tokens: int = 32768,
    enable_thinking: bool = False,
) -> str:
    """Call LLM via OpenAI-compatible endpoint."""
    clean_model = model.removeprefix("openai/")
    url = f"{api_base}/chat/completions"

    body: dict = {
        "model": clean_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""

        if enable_thinking and reasoning:
            logger.info("CoT reasoning: %d chars (content: %d chars)", len(reasoning), len(content))
        elif enable_thinking and not reasoning:
            logger.warning("CoT enabled but reasoning_content empty — stripping <think> tags")
            import re
            content = re.sub(r"^.*?</think>\s*", "", content, count=1, flags=re.DOTALL)
        return content


# ── Tavily Search (with caching) ─────────────────────────────────────────────


async def tavily_search(query: str, max_results: int = 5) -> dict:
    """Single Tavily search. Returns {query, answer, results: [{title, url, content}]}."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": "basic",
            },
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "query": query,
            "answer": data.get("answer", ""),
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:2000],
                }
                for r in data.get("results", [])
            ],
        }


async def run_searches(queries: list[str], cache_key: str) -> list[dict]:
    """Run Tavily searches with disk cache. Same cache_key = same results."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists():
        logger.info("Using cached search results: %s", cache_file.name)
        return json.loads(cache_file.read_text())

    logger.info("Running %d Tavily searches...", len(queries))
    results = []
    for i, q in enumerate(queries):
        logger.info("  Search %d/%d: %s", i + 1, len(queries), q[:80])
        try:
            r = await tavily_search(q)
            results.append(r)
        except Exception as e:
            logger.warning("  Search %d failed: %s", i + 1, e)
            results.append({"query": q, "answer": "", "results": [], "error": str(e)})

    cache_file.write_text(json.dumps(results, indent=2))
    logger.info("Cached %d search results to %s", len(results), cache_file.name)
    return results


def format_search_results(searches: list[dict]) -> str:
    """Format search results as markdown for prompt injection."""
    parts = []
    for i, sr in enumerate(searches, 1):
        parts.append(f"### Search {i}: {sr['query']}")
        if sr.get("answer"):
            parts.append(f"**Summary:** {sr['answer']}")
        for r in sr.get("results", []):
            parts.append(f"- [{r['title']}]({r['url']})")
            parts.append(f"  {r['content'][:800]}")
        parts.append("")
    return "\n".join(parts)


# ── Query Planning ────────────────────────────────────────────────────────────


PLAN_QUERIES_PROMPT = """\
You are a research query planner. Given a topic, generate exactly {count} search \
queries that cover different angles. Return ONLY a JSON array of strings.

{guidance}

Topic: {topic}

Return ONLY a JSON array like: ["query 1", "query 2", ...]
"""

GAP_ANALYSIS_PROMPT = """\
You analyzed search results for: {topic}

Here are the search results from the first round:
{search_results}

Identify 3 specific evidence gaps — topics where the search results are thin, \
contradictory, or missing entirely. For each gap, write a targeted search query \
that would fill it.

Return ONLY a JSON array of 3 search query strings:
["targeted query 1", "targeted query 2", "targeted query 3"]
"""


async def plan_queries(topic: str, count: int = 5, guidance: str = "") -> list[str]:
    """LLM plans search queries for a topic."""
    if not guidance:
        guidance = (
            f"The {count} queries must cover:\n"
            "1. Broad topic overview and current landscape\n"
            "2. Industry standards, frameworks, and best practices\n"
            "3. Security, compliance, and regulatory aspects\n"
            "4. Implementation patterns, architectures, and technology choices\n"
            "5. Case studies, real-world examples, and lessons learned"
        )

    prompt = PLAN_QUERIES_PROMPT.format(topic=topic, count=count, guidance=guidance)
    text = await call_llm(
        prompt,
        system="You output only valid JSON arrays. No markdown, no explanation.",
    )
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        queries = json.loads(text)
        if isinstance(queries, list):
            return [str(q) for q in queries[:count]]
    except json.JSONDecodeError:
        logger.warning("Failed to parse query plan, using templates")

    # Fallback
    return [
        f"{topic} overview current landscape 2025 2026",
        f"{topic} industry standards frameworks best practices",
        f"{topic} security compliance regulatory requirements",
        f"{topic} implementation patterns architecture technology",
        f"{topic} case studies real-world examples lessons learned",
    ][:count]


async def identify_gaps(topic: str, search_results_text: str) -> list[str]:
    """LLM identifies evidence gaps and generates refined search queries."""
    prompt = GAP_ANALYSIS_PROMPT.format(topic=topic, search_results=search_results_text[:15000])
    text = await call_llm(
        prompt,
        system="You output only valid JSON arrays. No markdown, no explanation.",
    )
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        queries = json.loads(text)
        if isinstance(queries, list):
            return [str(q) for q in queries[:3]]
    except json.JSONDecodeError:
        logger.warning("Failed to parse gap analysis, using fallback queries")

    return [
        f"{topic} regulatory compliance specific requirements",
        f"{topic} ROI cost benefit analysis enterprise",
        f"{topic} emerging threats vulnerabilities 2026",
    ]


# ── Experiment Runner ─────────────────────────────────────────────────────────


async def run_experiment(
    topic: str,
    prompt_type: str,  # "standard" or "semiformal"
    enable_thinking: bool,
    search_results_text: str,
    two_stage: bool = False,
) -> dict:
    """Run a single experiment variant."""
    if prompt_type == "semiformal":
        PROMPT = _load_prompt(PROMPT_DIR / "researcher_semiformal.py", "SYNTHESIZE_SEMIFORMAL_PROMPT")
        system = "You are a market research analyst. Follow the certificate structure exactly."
    else:
        PROMPT = _load_prompt(NAT_PROMPT_DIR / "research_prompts.py", "SYNTHESIZE_PROMPT")
        system = "You are a market research analyst. Be specific and cite sources."

    label = f"{prompt_type}/{'cot' if enable_thinking else 'nocot'}"
    if two_stage:
        label += "/two-stage"
    logger.info("Generating: %s (%d chars input)...", label, len(search_results_text))

    output = await call_llm(
        PROMPT.format(topic=topic, search_results=search_results_text),
        system=system,
        enable_thinking=enable_thinking,
    )

    return {
        "label": label,
        "prompt_type": prompt_type,
        "thinking": enable_thinking,
        "two_stage": two_stage,
        "output": output,
        "output_chars": len(output),
        "input_chars": len(search_results_text),
    }


async def main():
    parser = argparse.ArgumentParser(description="Structured prompt experiment harness")
    parser.add_argument("--topic", required=True, help="Research topic")
    parser.add_argument("--two-stage", action="store_true", help="Enable two-stage refinement")
    parser.add_argument("--prompt", choices=["standard", "semiformal", "all"], default="all")
    parser.add_argument("--thinking", action="store_true", help="Enable CoT (use --all-modes for matrix)")
    parser.add_argument("--all-modes", action="store_true", help="Run full 4-way (or 8-way with --two-stage) matrix")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY not set — check .env file")
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    topic = args.topic
    slug = topic[:40].replace(" ", "_").lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Stage 1: Plan and run initial searches ──
    logger.info("=== Stage 1: Initial search ===")
    cache_key = f"search_{slug}"
    queries = await plan_queries(topic, count=5)
    logger.info("Planned queries: %s", queries)
    stage1_results = await run_searches(queries, cache_key)
    stage1_text = format_search_results(stage1_results)
    total_sources = sum(len(r.get("results", [])) for r in stage1_results)
    logger.info("Stage 1: %d queries, %d results, %d chars", len(queries), total_sources, len(stage1_text))

    # ── Stage 2: Gap-driven refinement (optional) ──
    if args.two_stage:
        logger.info("=== Stage 2: Evidence gap refinement ===")
        gap_queries = await identify_gaps(topic, stage1_text)
        logger.info("Gap queries: %s", gap_queries)
        cache_key_s2 = f"search_{slug}_refined"
        stage2_results = await run_searches(gap_queries, cache_key_s2)
        stage2_text = format_search_results(stage2_results)
        refined_sources = sum(len(r.get("results", [])) for r in stage2_results)
        logger.info("Stage 2: %d queries, %d results, %d chars", len(gap_queries), refined_sources, len(stage2_text))

        # Merge: all results combined
        all_search_text = stage1_text + "\n---\n\n### Refined Search Results (Gap-Driven)\n\n" + stage2_text
        logger.info("Combined: %d chars from %d total sources", len(all_search_text), total_sources + refined_sources)
    else:
        all_search_text = stage1_text

    # ── Determine variants to run ──
    if args.all_modes:
        prompts = ["standard", "semiformal"]
        thinking_modes = [False, True]
    else:
        prompts = ["standard", "semiformal"] if args.prompt == "all" else [args.prompt]
        thinking_modes = [args.thinking]

    # ── Run experiments ──
    results = []
    for prompt_type in prompts:
        for thinking in thinking_modes:
            # One-shot (stage 1 only)
            r = await run_experiment(topic, prompt_type, thinking, stage1_text, two_stage=False)
            results.append(r)

            # Two-stage (if enabled)
            if args.two_stage:
                r2 = await run_experiment(topic, prompt_type, thinking, all_search_text, two_stage=True)
                results.append(r2)

    # ── Save results ──
    stage_tag = "2stage" if args.two_stage else "1stage"
    for r in results:
        thinking_tag = "cot" if r["thinking"] else "nocot"
        stage = "2stage" if r["two_stage"] else "1stage"
        filename = f"researcher_{slug}_{r['prompt_type']}_{thinking_tag}_{stage}_{timestamp}.md"
        filepath = output_dir / filename
        filepath.write_text(r["output"], encoding="utf-8")
        r["filename"] = filename
        logger.info("  %s: %s (%d chars)", r["label"], filename, r["output_chars"])

    # Save metadata
    meta = {
        "topic": topic,
        "timestamp": timestamp,
        "two_stage": args.two_stage,
        "stage1_queries": queries,
        "stage1_sources": total_sources,
        "stage2_queries": gap_queries if args.two_stage else [],
        "stage2_sources": refined_sources if args.two_stage else 0,
        "variants": [
            {
                "label": r["label"],
                "filename": r["filename"],
                "prompt_type": r["prompt_type"],
                "thinking": r["thinking"],
                "two_stage": r["two_stage"],
                "output_chars": r["output_chars"],
                "input_chars": r["input_chars"],
            }
            for r in results
        ],
    }
    meta_path = output_dir / f"researcher_{slug}_{stage_tag}_{timestamp}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    logger.info("Metadata: %s", meta_path.name)

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"EXPERIMENT RESULTS: {topic}")
    print(f"Stage: {'Two-stage (gap refinement)' if args.two_stage else 'One-shot'}")
    print(f"Sources: {total_sources}" + (f" + {refined_sources} refined" if args.two_stage else ""))
    print("=" * 60)
    for r in results:
        print(f"  {r['label']:35s}  {r['output_chars']:>6,d} chars  {r['filename']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
