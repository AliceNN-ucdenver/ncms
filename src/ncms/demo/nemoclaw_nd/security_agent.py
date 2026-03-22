"""Security Agent for the NemoClaw Non-Deterministic demo.

Expert in STRIDE threat model, OWASP Top 10, NIST controls, and compliance.
"""

from __future__ import annotations

from ncms.demo.nemoclaw_nd.llm_agent import LLMAgent


class SecurityAgent(LLMAgent):
    """Security domain agent for IMDB Lite platform."""

    primary_domain = "security"
    _expertise = ["security", "threats", "compliance", "controls"]
    _subscriptions = ["architecture", "identity-service", "implementation"]

    system_prompt = (
        "You are the Security Agent for the IMDB Lite platform. "
        "You are an expert in the STRIDE threat model, OWASP Top 10, NIST "
        "security controls, and compliance requirements. When answering "
        "questions, cite specific threats (THR-001 through THR-008), NIST "
        "references, and OWASP categories. Flag HIGH and CRITICAL residual "
        "risks. Recommend specific mitigations."
    )
