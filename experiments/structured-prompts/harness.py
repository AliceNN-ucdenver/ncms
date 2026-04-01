#!/usr/bin/env python3
"""Structured prompt experiment harness.

Runs the same topic through both standard and semi-formal prompt formats
using the live NCMS hub and Nemotron Nano on DGX Spark. Produces paired
documents for blind evaluation by the judge.

Usage:
    uv run python experiments/structured-prompts/harness.py \
        --topic "Authentication patterns for identity services" \
        --agent researcher \
        --hub-url http://localhost:9080

    uv run python experiments/structured-prompts/harness.py \
        --topic "Authentication patterns for identity services" \
        --agent prd \
        --hub-url http://localhost:9080 \
        --source-doc-id abc123  # research doc ID for PO input
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

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = logging.getLogger(__name__)

# LLM endpoint (Nemotron Nano on DGX Spark or Ollama)
DEFAULT_LLM_MODEL = os.environ.get(
    "LLM_MODEL", "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
)
DEFAULT_LLM_API_BASE = os.environ.get(
    "LLM_API_BASE", "http://spark-ee7d.local:8000/v1"
)
DEFAULT_HUB_URL = os.environ.get("NCMS_HUB_URL", "http://localhost:9080")

RESULTS_DIR = Path(__file__).parent / "results"


async def call_llm(
    prompt: str,
    system: str = "You are a helpful assistant.",
    model: str = DEFAULT_LLM_MODEL,
    api_base: str = DEFAULT_LLM_API_BASE,
    max_tokens: int = 32768,
) -> str:
    """Call the LLM via OpenAI-compatible endpoint (direct httpx, no SSL)."""
    # Strip litellm prefixes
    clean_model = model.removeprefix("openai/")
    url = f"{api_base}/chat/completions"

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            url,
            json={
                "model": clean_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def fetch_document(hub_url: str, doc_id: str) -> dict:
    """Fetch a document from the hub."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{hub_url}/api/v1/documents/{doc_id}")
        resp.raise_for_status()
        return resp.json()


async def search_memories(hub_url: str, query: str, domain: str | None = None, limit: int = 10) -> list:
    """Search NCMS memories via recall endpoint."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        params = {"q": query, "limit": str(limit)}
        if domain:
            params["domain"] = domain
        resp = await client.get(f"{hub_url}/api/v1/memories/recall", params=params)
        resp.raise_for_status()
        return resp.json().get("results", resp.json() if isinstance(resp.json(), list) else [])


async def run_researcher_experiment(topic: str, hub_url: str) -> dict:
    """Run researcher synthesis with both prompt formats."""
    # Load prompts directly (avoid module import issues with hyphenated dirs)
    prompt_dir = Path(__file__).parent / "prompts"
    ns = {}
    exec(Path(prompt_dir / "researcher_semiformal.py").read_text(), ns)
    SYNTHESIZE_SEMIFORMAL_PROMPT = ns["SYNTHESIZE_SEMIFORMAL_PROMPT"]

    ns2 = {}
    research_prompts_path = (
        Path(__file__).resolve().parents[2]
        / "packages" / "nvidia-nat-ncms" / "src" / "nat" / "plugins" / "ncms" / "research_prompts.py"
    )
    exec(research_prompts_path.read_text(), ns2)
    SYNTHESIZE_PROMPT = ns2["SYNTHESIZE_PROMPT"]

    # Step 1: Get search results (use Tavily or fetch from a recent project)
    logger.info("Searching for existing research documents...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{hub_url}/api/v1/documents")
        docs = resp.json()

    # Find a researcher doc to use as search results source
    research_docs = [d for d in docs if d.get("from_agent") == "researcher"]
    if research_docs:
        source = await fetch_document(hub_url, research_docs[0]["document_id"])
        search_results = source.get("content", "")[:15000]
    else:
        # Fallback: run actual Tavily searches
        logger.info("No existing research docs, running Tavily searches...")
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        if not tavily_key:
            logger.error("No TAVILY_API_KEY and no existing research docs")
            return {"error": "No search results available"}

        queries = [
            f"{topic} overview current landscape 2025 2026",
            f"{topic} industry standards frameworks best practices",
            f"{topic} security compliance regulatory requirements",
            f"{topic} implementation patterns architecture",
            f"{topic} case studies real-world examples",
        ]
        results = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for q in queries:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={"query": q, "search_depth": "basic", "max_results": 5},
                    headers={"Authorization": f"Bearer {tavily_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for r in data.get("results", []):
                        results.append(f"### {r.get('title', '')}\nURL: {r.get('url', '')}\n{r.get('content', '')[:1000]}")
        search_results = "\n\n".join(results)

    # Step 2: Generate with standard prompt
    logger.info("Generating with STANDARD prompt...")
    standard_prompt = SYNTHESIZE_PROMPT.format(topic=topic, search_results=search_results)
    standard_output = await call_llm(
        standard_prompt,
        system="You are a market research analyst. Be specific and cite sources.",
    )

    # Step 3: Generate with semi-formal prompt
    logger.info("Generating with SEMI-FORMAL prompt...")
    semiformal_prompt = SYNTHESIZE_SEMIFORMAL_PROMPT.format(
        topic=topic, search_results=search_results,
    )
    semiformal_output = await call_llm(
        semiformal_prompt,
        system="You are a market research analyst. Follow the certificate structure exactly.",
    )

    return {
        "agent": "researcher",
        "topic": topic,
        "standard": standard_output,
        "semiformal": semiformal_output,
        "search_results_chars": len(search_results),
    }


async def run_prd_experiment(topic: str, hub_url: str, source_doc_id: str | None = None) -> dict:
    """Run PRD synthesis with both prompt formats."""
    prompt_dir = Path(__file__).parent / "prompts"
    ns = {}
    exec(Path(prompt_dir / "prd_semiformal.py").read_text(), ns)
    SYNTHESIZE_PRD_SEMIFORMAL_PROMPT = ns["SYNTHESIZE_PRD_SEMIFORMAL_PROMPT"]

    ns2 = {}
    prd_prompts_path = (
        Path(__file__).resolve().parents[2]
        / "packages" / "nvidia-nat-ncms" / "src" / "nat" / "plugins" / "ncms" / "prd_prompts.py"
    )
    exec(prd_prompts_path.read_text(), ns2)
    SYNTHESIZE_PRD_PROMPT = ns2["SYNTHESIZE_PRD_PROMPT"]

    # Get source content (research report)
    if source_doc_id:
        source = await fetch_document(hub_url, source_doc_id)
        source_content = source.get("content", "")
    else:
        # Find latest researcher doc
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{hub_url}/api/v1/documents")
            docs = resp.json()
        research_docs = [d for d in docs if d.get("from_agent") == "researcher"]
        if not research_docs:
            return {"error": "No research document available"}
        source = await fetch_document(hub_url, research_docs[0]["document_id"])
        source_content = source.get("content", "")

    # Get expert input from NCMS memory
    arch_results = await search_memories(hub_url, f"{topic} architecture ADR patterns", "architecture", 5)
    sec_results = await search_memories(hub_url, f"{topic} security threats STRIDE OWASP", "security", 5)

    architect_input = "\n\n".join(
        r.get("content", r.get("memory", {}).get("content", ""))[:1000]
        for r in arch_results[:3]
    ) or "(No architect input available)"

    security_input = "\n\n".join(
        r.get("content", r.get("memory", {}).get("content", ""))[:1000]
        for r in sec_results[:3]
    ) or "(No security input available)"

    # Standard
    logger.info("Generating PRD with STANDARD prompt...")
    standard_prompt = SYNTHESIZE_PRD_PROMPT.format(
        topic=topic, source_content=source_content[:8000],
        architect_input=architect_input, security_input=security_input,
    )
    standard_output = await call_llm(
        standard_prompt,
        system="You are a senior product owner writing a PRD.",
    )

    # Semi-formal
    logger.info("Generating PRD with SEMI-FORMAL prompt...")
    semiformal_prompt = SYNTHESIZE_PRD_SEMIFORMAL_PROMPT.format(
        topic=topic, source_content=source_content[:8000],
        architect_input=architect_input, security_input=security_input,
    )
    semiformal_output = await call_llm(
        semiformal_prompt,
        system="You are a senior product owner. Follow the certificate structure exactly.",
    )

    return {
        "agent": "prd",
        "topic": topic,
        "standard": standard_output,
        "semiformal": semiformal_output,
        "source_chars": len(source_content),
    }


async def main():
    parser = argparse.ArgumentParser(description="Structured prompt experiment harness")
    parser.add_argument("--topic", required=True, help="Research topic")
    parser.add_argument("--agent", choices=["researcher", "prd", "archeologist"], required=True)
    parser.add_argument("--hub-url", default=DEFAULT_HUB_URL)
    parser.add_argument("--source-doc-id", default=None, help="Source document ID (for PRD)")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.agent == "researcher":
        result = await run_researcher_experiment(args.topic, args.hub_url)
    elif args.agent == "prd":
        result = await run_prd_experiment(args.topic, args.hub_url, args.source_doc_id)
    else:
        logger.error("Archeologist experiment not yet implemented")
        return

    if "error" in result:
        logger.error("Experiment failed: %s", result["error"])
        return

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = args.topic[:40].replace(" ", "_").lower()
    prefix = f"{result['agent']}_{slug}_{timestamp}"

    standard_path = output_dir / f"{prefix}_standard.md"
    semiformal_path = output_dir / f"{prefix}_semiformal.md"
    meta_path = output_dir / f"{prefix}_meta.json"

    standard_path.write_text(result["standard"], encoding="utf-8")
    semiformal_path.write_text(result["semiformal"], encoding="utf-8")
    meta_path.write_text(json.dumps({
        "agent": result["agent"],
        "topic": args.topic,
        "timestamp": timestamp,
        "standard_file": standard_path.name,
        "semiformal_file": semiformal_path.name,
        "standard_chars": len(result["standard"]),
        "semiformal_chars": len(result["semiformal"]),
    }, indent=2), encoding="utf-8")

    logger.info("Results saved:")
    logger.info("  Standard:    %s (%d chars)", standard_path.name, len(result["standard"]))
    logger.info("  Semi-formal: %s (%d chars)", semiformal_path.name, len(result["semiformal"]))
    logger.info("  Metadata:    %s", meta_path.name)
    logger.info("")
    logger.info("Next: run judge.py to evaluate the pair")


if __name__ == "__main__":
    asyncio.run(main())
