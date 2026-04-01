#!/usr/bin/env python3
"""LLM Judge for structured prompt experiment.

Evaluates paired documents (standard vs semi-formal) using a structured
rubric. Documents are presented blind as "Version A" and "Version B"
with random assignment to eliminate ordering bias.

The judge evaluates on 6 dimensions, each scored 0-10:
1. Source Traceability — Can every claim be traced to a specific source?
2. Requirement Coverage — Does the document address all input findings?
3. Factual Grounding — Are references real and correctly cited?
4. Completeness — Are required sections present and substantive?
5. Consistency — Do sections contradict each other?
6. Actionability — Could a developer/PM act on this document?

Usage:
    uv run python experiments/structured-prompts/judge.py \
        --standard results/researcher_auth_standard.md \
        --semiformal results/researcher_auth_semiformal.md

    # Or auto-discover latest pair:
    uv run python experiments/structured-prompts/judge.py --latest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"

JUDGE_RUBRIC = """\
You are an expert technical document evaluator. You will evaluate two versions \
of a {doc_type} document on the same topic. The versions are labeled "Version A" \
and "Version B" — you do not know which technique produced which version.

## Topic: {topic}

## Version A:
{version_a}

## Version B:
{version_b}

---

Evaluate EACH version on these 6 dimensions (score 0-10 for each):

### 1. Source Traceability (0-10)
Can every claim be traced to a specific source, reference, or input?
- 10: Every claim cites a specific source with enough detail to verify
- 5: Some claims are cited, many are not
- 0: No source attribution at all

### 2. Requirement Coverage (0-10)
Does the document address all the information from its inputs?
- 10: Every input finding/recommendation is addressed or explicitly excluded
- 5: Major inputs are addressed but significant gaps exist
- 0: Most input material is ignored

### 3. Factual Grounding (0-10)
Are references and citations real, specific, and correctly used?
- 10: All references are specific (URLs, IDs, standards with versions)
- 5: Mix of specific and vague references
- 0: References appear fabricated or are entirely generic

### 4. Completeness (0-10)
Are all required sections present with substantive content?
- 10: Every section is present, detailed, and substantive
- 5: Most sections present but some are thin or missing
- 0: Major sections missing

### 5. Consistency (0-10)
Do sections support each other without contradictions?
- 10: Perfect internal consistency, sections reinforce each other
- 5: Minor inconsistencies that don't affect usability
- 0: Major contradictions that undermine the document

### 6. Actionability (0-10)
Could a downstream consumer (developer, PM, architect) act on this?
- 10: Specific enough to implement without further clarification
- 5: Provides direction but needs significant clarification
- 0: Too vague or abstract to act on

---

Output your evaluation as JSON:
```json
{{
  "version_a": {{
    "source_traceability": <score>,
    "requirement_coverage": <score>,
    "factual_grounding": <score>,
    "completeness": <score>,
    "consistency": <score>,
    "actionability": <score>,
    "total": <sum>,
    "strengths": "<1-2 sentences>",
    "weaknesses": "<1-2 sentences>"
  }},
  "version_b": {{
    "source_traceability": <score>,
    "requirement_coverage": <score>,
    "factual_grounding": <score>,
    "completeness": <score>,
    "consistency": <score>,
    "actionability": <score>,
    "total": <sum>,
    "strengths": "<1-2 sentences>",
    "weaknesses": "<1-2 sentences>"
  }},
  "preferred": "A" or "B",
  "reasoning": "<2-3 sentences explaining the preference>"
}}
```
"""


async def judge_pair(
    standard_text: str,
    semiformal_text: str,
    topic: str,
    doc_type: str = "market research",
) -> dict:
    """Run the LLM judge on a document pair with random assignment."""
    import litellm

    # Random assignment to eliminate ordering bias
    coin = random.choice([True, False])
    if coin:
        version_a, version_b = standard_text, semiformal_text
        mapping = {"A": "standard", "B": "semiformal"}
    else:
        version_a, version_b = semiformal_text, standard_text
        mapping = {"A": "semiformal", "B": "standard"}

    prompt = JUDGE_RUBRIC.format(
        doc_type=doc_type,
        topic=topic,
        version_a=version_a[:30000],  # Cap to fit context
        version_b=version_b[:30000],
    )

    model = os.environ.get("JUDGE_MODEL", "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16")
    api_base = os.environ.get("JUDGE_API_BASE", "http://spark-ee7d.local:8000/v1")

    response = await litellm.acompletion(
        model=model,
        messages=[
            {"role": "system", "content": "You are an expert document evaluator. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=4096,
        api_base=api_base if api_base else None,
    )

    text = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        scores = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse judge output as JSON: %s", text[:200])
        return {"error": "JSON parse failed", "raw": text, "mapping": mapping}

    # Add mapping so we know which is which
    scores["mapping"] = mapping
    scores["standard_was"] = "A" if mapping["A"] == "standard" else "B"
    scores["semiformal_was"] = "A" if mapping["A"] == "semiformal" else "B"

    return scores


def find_latest_pair() -> tuple[Path, Path, dict] | None:
    """Find the latest experiment pair in results/."""
    meta_files = sorted(RESULTS_DIR.glob("*_meta.json"), reverse=True)
    for meta_path in meta_files:
        meta = json.loads(meta_path.read_text())
        standard = RESULTS_DIR / meta["standard_file"]
        semiformal = RESULTS_DIR / meta["semiformal_file"]
        if standard.exists() and semiformal.exists():
            return standard, semiformal, meta
    return None


async def main():
    parser = argparse.ArgumentParser(description="LLM Judge for prompt experiment")
    parser.add_argument("--standard", help="Path to standard version")
    parser.add_argument("--semiformal", help="Path to semi-formal version")
    parser.add_argument("--topic", default="Software delivery document")
    parser.add_argument("--doc-type", default="market research")
    parser.add_argument("--latest", action="store_true", help="Auto-discover latest pair")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.latest:
        pair = find_latest_pair()
        if not pair:
            logger.error("No experiment pairs found in %s", RESULTS_DIR)
            return
        standard_path, semiformal_path, meta = pair
        topic = meta.get("topic", args.topic)
        doc_type = "market research" if meta.get("agent") == "researcher" else "PRD"
    else:
        if not args.standard or not args.semiformal:
            logger.error("Provide --standard and --semiformal paths, or use --latest")
            return
        standard_path = Path(args.standard)
        semiformal_path = Path(args.semiformal)
        topic = args.topic
        doc_type = args.doc_type

    standard_text = standard_path.read_text(encoding="utf-8")
    semiformal_text = semiformal_path.read_text(encoding="utf-8")

    logger.info("Judging: %s vs %s", standard_path.name, semiformal_path.name)
    logger.info("Topic: %s | Type: %s", topic, doc_type)

    result = await judge_pair(standard_text, semiformal_text, topic, doc_type)

    if "error" in result:
        logger.error("Judge failed: %s", result["error"])
        print(result.get("raw", ""))
        return

    # Print results
    mapping = result["mapping"]
    print("\n" + "=" * 60)
    print(f"JUDGE RESULTS: {topic}")
    print("=" * 60)

    for version_key in ["version_a", "version_b"]:
        label = version_key.replace("version_", "Version ").upper()
        actual = mapping[version_key.replace("version_", "").upper()]
        scores = result[version_key]
        print(f"\n{label} ({actual.upper()}):")
        print(f"  Source Traceability:  {scores.get('source_traceability', '?')}/10")
        print(f"  Requirement Coverage: {scores.get('requirement_coverage', '?')}/10")
        print(f"  Factual Grounding:   {scores.get('factual_grounding', '?')}/10")
        print(f"  Completeness:        {scores.get('completeness', '?')}/10")
        print(f"  Consistency:         {scores.get('consistency', '?')}/10")
        print(f"  Actionability:       {scores.get('actionability', '?')}/10")
        print(f"  TOTAL:               {scores.get('total', '?')}/60")
        print(f"  Strengths:  {scores.get('strengths', '')}")
        print(f"  Weaknesses: {scores.get('weaknesses', '')}")

    preferred = result.get("preferred", "?")
    preferred_actual = mapping.get(preferred, "?")
    print(f"\nPREFERRED: Version {preferred} ({preferred_actual.upper()})")
    print(f"REASONING: {result.get('reasoning', '')}")
    print("=" * 60)

    # Save judge output
    judge_path = standard_path.with_name(
        standard_path.name.replace("_standard.md", "_judge.json")
    )
    judge_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info("Judge results saved to: %s", judge_path.name)


if __name__ == "__main__":
    asyncio.run(main())
