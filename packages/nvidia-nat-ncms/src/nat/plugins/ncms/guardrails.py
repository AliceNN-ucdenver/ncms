# SPDX-License-Identifier: Apache-2.0
"""NemoGuardrails policy enforcement for LangGraph pipelines.

Provides a guardrails check node that validates pipeline inputs and outputs
against policies stored in NCMS. Policies are versioned documents with
configurable escalation levels (warn, block, reject).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PolicyViolation:
    """Represents a single policy violation."""

    def __init__(
        self,
        policy_type: str,
        rule: str,
        message: str,
        escalation: str = "warn",  # warn, block, reject
    ) -> None:
        self.policy_type = policy_type
        self.rule = rule
        self.message = message
        self.escalation = escalation

    def __repr__(self) -> str:
        return f"[{self.escalation.upper()}] {self.policy_type}: {self.message}"


async def load_policies(hub_url: str) -> dict[str, Any]:
    """Load all active policies from the NCMS hub.

    Returns a dict keyed by policy_type with the parsed policy content.
    Falls back to empty policies if hub is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{hub_url}/api/v1/policies")
            if resp.status_code == 200:
                policies = resp.json()
                return {p["policy_type"]: p for p in policies}
    except Exception as e:
        logger.warning("[guardrails] Failed to load policies: %s", e)
    return {}


def check_domain_scope(
    topic: str, policy: dict[str, Any],
) -> list[PolicyViolation]:
    """Check if the topic falls within allowed domains.

    Args:
        topic: The research/design topic text
        policy: The domain_scope policy document

    Returns:
        List of violations (empty if compliant)
    """
    violations = []
    content = policy.get("content", "")

    # Parse allowed and denied domains from YAML-like content
    allowed = _extract_list(content, "allowed_domains")
    denied = _extract_list(content, "denied_domains")
    elevated = _extract_list(content, "elevated_approval")

    topic_lower = topic.lower()

    # Check denied domains
    for domain in denied:
        if domain.lower() in topic_lower:
            violations.append(PolicyViolation(
                policy_type="domain_scope",
                rule=f"denied_domain:{domain}",
                message=f"Topic matches denied domain: '{domain}'",
                escalation="reject",
            ))

    # Check elevated approval
    for domain in elevated:
        if domain.lower() in topic_lower:
            violations.append(PolicyViolation(
                policy_type="domain_scope",
                rule=f"elevated_approval:{domain}",
                message=f"Topic requires elevated approval: '{domain}'",
                escalation="block",
            ))

    # Check if topic matches any allowed domain (if allow-list is defined)
    if allowed:
        matched = any(d.lower() in topic_lower for d in allowed)
        if not matched:
            violations.append(PolicyViolation(
                policy_type="domain_scope",
                rule="not_in_allowed_domains",
                message=f"Topic does not match any allowed domain: {allowed}",
                escalation="warn",
            ))

    return violations


def check_technology_scope(
    content: str, policy: dict[str, Any],
) -> list[PolicyViolation]:
    """Check if a design uses approved technologies.

    Args:
        content: The design document content
        policy: The technology_scope policy document

    Returns:
        List of violations
    """
    violations = []
    policy_content = policy.get("content", "")

    # Parse prohibited patterns
    prohibited = _extract_list(policy_content, "prohibited")

    content_lower = content.lower()
    for pattern in prohibited:
        # Simple substring check for prohibited patterns
        if pattern.lower().replace("()", "").strip() in content_lower:
            violations.append(PolicyViolation(
                policy_type="technology_scope",
                rule=f"prohibited:{pattern}",
                message=f"Design uses prohibited pattern: '{pattern}'",
                escalation="block",
            ))

    return violations


def check_output_compliance(
    content: str, policy: dict[str, Any],
) -> list[PolicyViolation]:
    """Check output document for compliance violations.

    Checks for hardcoded secrets, prohibited patterns, and
    mandatory section presence.

    Args:
        content: The document content to check
        policy: The compliance_requirements policy document

    Returns:
        List of violations
    """
    violations = []

    # Secret detection (always active, regardless of policy)
    secret_patterns = [
        (r'(?:api[_-]?key|apikey)\s*[:=]\s*["\']?[a-zA-Z0-9_\-]{20,}', "Possible API key"),
        (r'(?:password|passwd|pwd)\s*[:=]\s*["\'][^"\']{8,}', "Possible hardcoded password"),
        (r'(?:secret|token)\s*[:=]\s*["\'][a-zA-Z0-9_\-]{10,}', "Possible hardcoded secret"),
        (r'mongodb(?:\+srv)?://[^\s"\']+:[^\s"\']+@', "MongoDB connection string with credentials"),
        (r'postgres(?:ql)?://[^\s"\']+:[^\s"\']+@', "PostgreSQL connection string with credentials"),
    ]

    for pattern, description in secret_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            # Connection strings with real credentials are BLOCK level
            # Password field references in code examples are WARN level
            is_connection_string = "://" in description.lower()
            violations.append(PolicyViolation(
                policy_type="compliance",
                rule="secret_detection",
                message=f"Possible secret in document: {description}",
                escalation="block" if is_connection_string else "warn",
            ))

    # Check mandatory sections from policy
    policy_content = policy.get("content", "") if policy else ""
    mandatory = _extract_list(policy_content, "mandatory_sections")
    for section in mandatory:
        if section.lower() not in content.lower():
            violations.append(PolicyViolation(
                policy_type="compliance",
                rule=f"missing_section:{section}",
                message=f"Mandatory section not found: '{section}'",
                escalation="warn",
            ))

    return violations


async def run_input_guardrails(
    hub_url: str,
    topic: str,
    agent_id: str,
) -> tuple[bool, list[PolicyViolation]]:
    """Run input guardrails before a pipeline starts.

    Returns:
        (can_proceed, violations) - can_proceed is False if any violation
        has escalation="reject"
    """
    policies = await load_policies(hub_url)
    violations = []

    domain_policy = policies.get("domain_scope")
    if domain_policy:
        violations.extend(check_domain_scope(topic, domain_policy))

    # Log violations
    for v in violations:
        logger.info("[guardrails:%s] %s", agent_id, v)

    # Announce violations to bus
    if violations:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{hub_url}/api/v1/bus/announce",
                    json={
                        "content": f"⚠️ Guardrails: {len(violations)} violation(s) for {agent_id}: "
                                   + "; ".join(str(v) for v in violations),
                        "domains": ["guardrails"],
                        "from_agent": agent_id,
                    },
                )
        except Exception:
            pass

    can_proceed = not any(v.escalation == "reject" for v in violations)
    return can_proceed, violations


async def run_output_guardrails(
    hub_url: str,
    content: str,
    agent_id: str,
) -> tuple[bool, list[PolicyViolation]]:
    """Run output guardrails before a document is published.

    Returns:
        (can_publish, violations) - can_publish is False if any violation
        has escalation="block" or "reject"
    """
    policies = await load_policies(hub_url)
    violations = []

    tech_policy = policies.get("technology_scope")
    if tech_policy:
        violations.extend(check_technology_scope(content, tech_policy))

    compliance_policy = policies.get("compliance_requirements")
    violations.extend(check_output_compliance(content, compliance_policy or {}))

    for v in violations:
        logger.info("[guardrails:%s] %s", agent_id, v)

    if violations:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{hub_url}/api/v1/bus/announce",
                    json={
                        "content": f"⚠️ Output guardrails: {len(violations)} finding(s) for {agent_id}: "
                                   + "; ".join(str(v) for v in violations[:5]),
                        "domains": ["guardrails"],
                        "from_agent": agent_id,
                    },
                )
        except Exception:
            pass

    can_publish = not any(v.escalation in ("block", "reject") for v in violations)
    return can_publish, violations


def _extract_list(yaml_text: str, key: str) -> list[str]:
    """Extract a YAML-like list from text. Simple parser for policy content.

    Handles:
        key:
          - item1
          - item2
    """
    items = []
    in_list = False
    for line in yaml_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            in_list = True
            continue
        if in_list:
            if stripped.startswith("- "):
                items.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                in_list = False  # End of list
    return items
