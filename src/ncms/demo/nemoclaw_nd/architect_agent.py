"""Architecture Agent for the NemoClaw Non-Deterministic demo.

Expert in CALM architecture model, ADRs, quality attributes, and fitness functions.
"""

from __future__ import annotations

from ncms.demo.nemoclaw_nd.llm_agent import LLMAgent


class ArchitectAgent(LLMAgent):
    """Architecture domain agent for IMDB Lite platform."""

    primary_domain = "architecture"
    _expertise = ["architecture", "calm-model", "quality", "decisions"]
    _subscriptions = ["identity-service", "implementation"]

    system_prompt = (
        "You are the Architecture Agent for the IMDB Lite platform. "
        "You are an expert in the CALM architecture model, Architecture Decision "
        "Records (ADRs), quality attributes, and fitness functions. "
        "When answering questions, cite specific ADRs, CALM nodes, quality "
        "attributes, or fitness functions from your knowledge. Be precise and "
        "reference specific IDs (e.g., ADR-003, THR-001, node: imdb-identity-service)."
    )
