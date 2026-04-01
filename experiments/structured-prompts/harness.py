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


# ── ArXiv Search (with caching) ──────────────────────────────────────────────


def arxiv_search_sync(query: str, max_results: int = 5) -> list[dict]:
    """Search ArXiv for recent papers (synchronous — run via to_thread)."""
    import arxiv as _arxiv
    from datetime import datetime, timedelta, timezone

    search = _arxiv.Search(
        query=query,
        max_results=max_results * 2,  # Fetch extra to filter by date
        sort_by=_arxiv.SortCriterion.Relevance,
    )

    results = []
    client = _arxiv.Client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    for paper in client.results(search):
        if paper.published and paper.published < cutoff:
            continue
        results.append({
            "title": paper.title,
            "url": paper.entry_id,
            "summary": paper.summary[:1500],
            "published": paper.published.isoformat() if paper.published else "",
            "authors": ", ".join(a.name for a in paper.authors[:3]),
        })
        if len(results) >= max_results:
            break
    return results


async def run_arxiv_searches(queries: list[str], cache_key: str) -> list[dict]:
    """Run ArXiv searches with disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}_arxiv.json"

    if cache_file.exists():
        logger.info("Using cached ArXiv results: %s", cache_file.name)
        return json.loads(cache_file.read_text())

    logger.info("Running %d ArXiv searches...", len(queries))
    all_results: list[dict] = []
    for i, q in enumerate(queries):
        logger.info("  ArXiv %d/%d: %s", i + 1, len(queries), q[:80])
        try:
            papers = await asyncio.to_thread(arxiv_search_sync, q)
            all_results.append({"query": q, "papers": papers})
            logger.info("    Found %d recent papers", len(papers))
        except Exception as e:
            logger.warning("  ArXiv search %d failed: %s", i + 1, e)
            all_results.append({"query": q, "papers": [], "error": str(e)})

    cache_file.write_text(json.dumps(all_results, indent=2))
    logger.info("Cached ArXiv results to %s", cache_file.name)
    return all_results


def format_arxiv_results(searches: list[dict]) -> str:
    """Format ArXiv results as markdown."""
    parts = ["## Academic Papers (ArXiv — last 12 months)\n"]
    paper_num = 0
    for sr in searches:
        for p in sr.get("papers", []):
            paper_num += 1
            parts.append(f"### Paper {paper_num}: {p['title']}")
            parts.append(f"**Authors:** {p.get('authors', 'Unknown')}")
            parts.append(f"**Published:** {p.get('published', '')[:10]}")
            parts.append(f"**URL:** {p['url']}")
            parts.append(f"{p['summary'][:1000]}")
            parts.append("")
    if paper_num == 0:
        parts.append("(No recent papers found for the given queries)")
    return "\n".join(parts)


# ── Query Planning ────────────────────────────────────────────────────────────


PLAN_WEB_QUERIES_PROMPT = """\
You are an expert research query planner. Given a topic, generate exactly 5 \
high-quality web search queries. Each query should be specific enough to find \
targeted results (not generic overviews) and include temporal markers (2025, 2026) \
where relevant.

Topic: {topic}

The 5 queries MUST cover these distinct angles:
1. MARKET: Market size, growth projections, key vendors, competitive landscape
2. STANDARDS: Specific standards (e.g., NIST, OWASP, ISO), frameworks, compliance requirements
3. SECURITY: Threat landscape, specific attack vectors, vulnerability data, breach statistics
4. IMPLEMENTATION: Architecture patterns, technology stacks, integration approaches, trade-offs
5. EVIDENCE: Case studies with measurable outcomes (ROI, latency, conversion), real deployments

Make each query specific with domain terminology. Include version numbers, years, \
and specific framework names when possible.

Return ONLY a JSON array of 5 strings.
"""

PLAN_ARXIV_QUERIES_PROMPT = """\
You are an academic research query planner. Given a topic, generate exactly 3 \
search queries optimized for ArXiv academic papers. Use technical/formal language, \
not marketing terms. Focus on: formal methods, protocol analysis, security proofs, \
benchmark evaluations, and novel architectures.

Topic: {topic}

ArXiv search tips:
- Use short keyword phrases (3-6 words work best)
- Include CS subfields: "zero trust" NOT "zero-trust architecture solutions"
- Prefer formal terms: "formal verification" over "testing"
- Include specific protocols/standards by name

Return ONLY a JSON array of 3 strings.
"""

GAP_ANALYSIS_STANDARD_PROMPT = """\
You are a research analyst reviewing initial search results for: {topic}

Here is what the first round of searches found:
{search_results}

Identify 3 specific EVIDENCE GAPS — topics where the results are thin (only 1 \
source), contradictory, or completely missing. For each gap, write a concrete \
web search query that would find NEW information not already covered.

Each query must be a real, specific search string with domain terminology and \
year markers. NOT placeholders.

Return ONLY a JSON array of 3 search query strings.
"""

GAP_ANALYSIS_SEMIFORMAL_PROMPT = """\
You are a research analyst reviewing initial search results for: {topic}

Here is what the first round of searches found:
{search_results}

Perform a STRUCTURED gap analysis:

PREMISES: For each major finding, state how many independent sources support it.

EVIDENCE GAPS: Identify exactly 3 topics where:
- A finding has only 1 supporting source (needs independent confirmation)
- Two sources contradict each other (needs resolution)
- An important sub-topic has zero coverage (needs new research)

For each gap, write a targeted web search query that would fill it.

Each query must be a real, specific search string with domain terminology.

Return ONLY a JSON array of 3 search query strings.
"""


async def _llm_to_json_array(prompt: str, count: int, fallback: list[str]) -> list[str]:
    """Call LLM and parse JSON array, with fallback."""
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
            return [str(q) for q in queries[:count]]
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON output, using fallback")
    return fallback[:count]


async def plan_web_queries(topic: str) -> list[str]:
    """LLM plans 5 web search queries."""
    return await _llm_to_json_array(
        PLAN_WEB_QUERIES_PROMPT.format(topic=topic),
        count=5,
        fallback=[
            f"{topic} market size growth projections vendors 2025 2026",
            f"{topic} NIST OWASP ISO standards compliance requirements",
            f"{topic} security threats attack vectors breach statistics 2025",
            f"{topic} architecture patterns technology stack implementation",
            f"{topic} case study ROI deployment results enterprise",
        ],
    )


async def plan_arxiv_queries(topic: str) -> list[str]:
    """LLM plans 3 ArXiv search queries."""
    return await _llm_to_json_array(
        PLAN_ARXIV_QUERIES_PROMPT.format(topic=topic),
        count=3,
        fallback=[
            f"authentication identity formal verification",
            f"zero trust access control architecture",
            f"multi-factor authentication security analysis",
        ],
    )


async def identify_gaps(topic: str, search_results_text: str, semiformal: bool = False) -> list[str]:
    """LLM identifies evidence gaps. Uses semiformal analysis if requested."""
    template = GAP_ANALYSIS_SEMIFORMAL_PROMPT if semiformal else GAP_ANALYSIS_STANDARD_PROMPT
    return await _llm_to_json_array(
        template.format(topic=topic, search_results=search_results_text[:20000]),
        count=3,
        fallback=[
            f"{topic} regulatory compliance specific requirements 2025 2026",
            f"{topic} ROI cost benefit analysis enterprise deployment",
            f"{topic} emerging threats vulnerabilities recent incidents",
        ],
    )


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
    parser.add_argument("--arxiv", action="store_true", help="Include ArXiv academic paper search")
    parser.add_argument("--prompt", choices=["standard", "semiformal", "all"], default="all")
    parser.add_argument("--thinking", action="store_true", help="Enable CoT (use --all-modes for matrix)")
    parser.add_argument("--all-modes", action="store_true", help="Run full matrix")
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

    # ── Stage 1: Plan and run initial searches (shared across all variants) ──
    logger.info("=== Stage 1: Initial web search ===")
    cache_key = f"search_{slug}"
    web_queries = await plan_web_queries(topic)
    logger.info("Web queries: %s", web_queries)
    stage1_results = await run_searches(web_queries, cache_key)
    stage1_text = format_search_results(stage1_results)
    total_web = sum(len(r.get("results", [])) for r in stage1_results)
    logger.info("Stage 1 web: %d queries, %d results, %d chars", len(web_queries), total_web, len(stage1_text))

    # ── ArXiv academic papers (shared across all variants) ──
    arxiv_text = ""
    total_arxiv = 0
    if args.arxiv:
        logger.info("=== Stage 1: ArXiv academic papers ===")
        arxiv_queries = await plan_arxiv_queries(topic)
        logger.info("ArXiv queries: %s", arxiv_queries)
        arxiv_results = await run_arxiv_searches(arxiv_queries, cache_key)
        arxiv_text = format_arxiv_results(arxiv_results)
        total_arxiv = sum(len(r.get("papers", [])) for r in arxiv_results)
        logger.info("ArXiv: %d queries, %d papers, %d chars", len(arxiv_queries), total_arxiv, len(arxiv_text))

    # Stage 1 combined (shared input for all one-shot variants)
    stage1_combined = stage1_text
    if arxiv_text:
        stage1_combined += "\n---\n\n" + arxiv_text

    # ── Stage 2: Consistent gap analysis per prompt type ──
    # Standard variants get standard gap analysis → standard refined searches
    # Semiformal variants get semiformal gap analysis → semiformal refined searches
    # This ensures the two-stage pipeline is internally consistent
    stage2_data: dict[str, str] = {}  # prompt_type -> combined search text
    gap_queries_log: dict[str, list[str]] = {}
    refined_web = 0

    if args.two_stage:
        for prompt_type in (["standard", "semiformal"] if args.all_modes or args.prompt == "all" else [args.prompt]):
            is_sf = prompt_type == "semiformal"
            logger.info("=== Stage 2 (%s): Evidence gap refinement ===", prompt_type)

            gaps = await identify_gaps(topic, stage1_combined, semiformal=is_sf)
            gap_queries_log[prompt_type] = gaps
            logger.info("Gap queries (%s): %s", prompt_type, gaps)

            # Web refinement
            s2_cache = f"search_{slug}_refined_{prompt_type}_v3"
            s2_results = await run_searches(gaps, s2_cache)
            s2_text = format_search_results(s2_results)
            s2_count = sum(len(r.get("results", [])) for r in s2_results)
            refined_web += s2_count
            logger.info("Stage 2 web (%s): %d results, %d chars", prompt_type, s2_count, len(s2_text))

            # ArXiv refinement
            arxiv_s2_text = ""
            if args.arxiv:
                arxiv_s2 = await run_arxiv_searches(gaps[:2], f"{cache_key}_refined_{prompt_type}")
                arxiv_s2_text = format_arxiv_results(arxiv_s2)
                s2_arxiv = sum(len(r.get("papers", [])) for r in arxiv_s2)
                total_arxiv += s2_arxiv
                logger.info("ArXiv refined (%s): %d papers", prompt_type, s2_arxiv)

            stage2_data[prompt_type] = (
                stage1_combined
                + f"\n---\n\n### Refined Search Results ({prompt_type} gap analysis)\n\n"
                + s2_text
                + ("\n" + arxiv_s2_text if arxiv_s2_text else "")
            )
            logger.info("Combined (%s): %d chars", prompt_type, len(stage2_data[prompt_type]))

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
            # One-shot (stage 1 input — shared across all variants)
            r = await run_experiment(topic, prompt_type, thinking, stage1_combined, two_stage=False)
            results.append(r)

            # Two-stage (prompt-type-consistent gap analysis)
            if args.two_stage and prompt_type in stage2_data:
                r2 = await run_experiment(
                    topic, prompt_type, thinking, stage2_data[prompt_type], two_stage=True,
                )
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
        "arxiv": args.arxiv,
        "stage1_web_queries": web_queries,
        "stage1_web_sources": total_web,
        "stage1_arxiv_papers": total_arxiv,
        "stage2_gap_queries": gap_queries_log if args.two_stage else {},
        "stage2_refined_web": refined_web if args.two_stage else 0,
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
    src_summary = f"Web: {total_web}"
    if total_arxiv:
        src_summary += f" | ArXiv: {total_arxiv}"
    if args.two_stage:
        src_summary += f" | Refined: {refined_web}"
    print(f"Sources: {src_summary}")
    print("=" * 60)
    for r in results:
        print(f"  {r['label']:35s}  {r['output_chars']:>6,d} chars  {r['filename']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
