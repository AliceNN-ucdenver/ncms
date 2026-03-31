# SPDX-License-Identifier: Apache-2.0
"""Spec completeness validator for the Builder's LangGraph pipeline.

Pure Python validation that checks implementation designs against
structural requirements and a PRD requirements manifest. No LLM cost.
Runs in under 1 second.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a completeness validation check."""

    passed: bool
    issues: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        if self.passed:
            return (
                f"✅ Completeness check passed: "
                f"{self.stats.get('sections', 0)} sections, "
                f"{self.stats.get('endpoints', 0)} endpoints, "
                f"{self.stats.get('interfaces', 0)} interfaces, "
                f"{self.stats.get('code_blocks', 0)} code blocks"
            )
        return (
            f"❌ Completeness check failed ({len(self.issues)} issues): "
            + "; ".join(self.issues[:5])
        )


# Default required sections in an implementation design
DEFAULT_REQUIRED_SECTIONS = [
    "Project Structure",
    "API Endpoint",
    "Data Model",
    "Authentication",
    "Security Control",
    "Configuration",
    "Error Handling",
    "Testing",
    "Deployment",
]


def validate_design_completeness(
    design: str,
    prd: str = "",
    manifest: dict | None = None,
    required_sections: list[str] | None = None,
    min_endpoints: int = 3,
    min_interfaces: int = 3,
    min_code_blocks: int = 5,
) -> ValidationResult:
    """Validate structural completeness of an implementation design.

    Args:
        design: The markdown implementation design content
        prd: The PRD content (for cross-reference checking)
        manifest: Optional structured requirements manifest from PO (JSON dict)
        required_sections: List of section names to check for (case-insensitive)
        min_endpoints: Minimum number of API endpoints expected
        min_interfaces: Minimum number of TypeScript interfaces expected
        min_code_blocks: Minimum number of code blocks expected

    Returns:
        ValidationResult with passed/failed status, issues list, and stats
    """
    sections = required_sections or DEFAULT_REQUIRED_SECTIONS
    issues: list[str] = []
    stats: dict[str, int] = {}

    design_lower = design.lower()

    # ── 1. Section presence ──────────────────────────────────────────────
    found_sections = 0
    for section in sections:
        if section.lower() in design_lower:
            found_sections += 1
        else:
            issues.append(f"Missing section: {section}")
    stats["sections"] = found_sections
    stats["sections_required"] = len(sections)

    # ── 2. Code blocks ──────────────────────────────────────────────────
    code_blocks = design.count("```")  # pairs of code fences
    actual_blocks = code_blocks // 2
    stats["code_blocks"] = actual_blocks
    if actual_blocks < min_code_blocks:
        issues.append(
            f"Only {actual_blocks} code blocks (expected {min_code_blocks}+)"
        )

    # ── 3. Sections without code ─────────────────────────────────────────
    # Split by ## headings and check each has at least one code block
    heading_sections = re.split(r"^##\s+", design, flags=re.MULTILINE)
    for section_text in heading_sections[1:]:  # skip preamble before first ##
        title_line = section_text.split("\n")[0].strip()
        if "```" not in section_text and len(section_text) > 100:
            issues.append(f"Section '{title_line}' has no code examples")

    # ── 4. API endpoint coverage ─────────────────────────────────────────
    endpoints = re.findall(
        r"(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s\"'`,)]+)", design,
    )
    stats["endpoints"] = len(endpoints)

    if manifest and "endpoints" in manifest:
        # Validate against the PRD requirements manifest
        expected = manifest["endpoints"]
        stats["endpoints_expected"] = len(expected)
        for ep in expected:
            method = ep.get("method", "").upper()
            path = ep.get("path", "")
            found = any(
                m == method and p == path for m, p in endpoints
            )
            if not found:
                # Try fuzzy match (path might have parameters)
                path_base = re.sub(r"/:\w+", "/", path)
                found = any(
                    m == method and path_base in p for m, p in endpoints
                )
            if not found:
                desc = ep.get("description", "")
                issues.append(
                    f"Missing endpoint: {method} {path} ({desc})"
                )
    elif len(endpoints) < min_endpoints:
        issues.append(
            f"Only {len(endpoints)} API endpoints (expected {min_endpoints}+)"
        )

    # ── 5. TypeScript interface definitions ───────────────────────────────
    interfaces = re.findall(r"interface\s+(\w+)", design)
    stats["interfaces"] = len(interfaces)
    if len(interfaces) < min_interfaces:
        issues.append(
            f"Only {len(interfaces)} TypeScript interfaces (expected {min_interfaces}+)"
        )

    # ── 6. Security requirement coverage (from manifest) ─────────────────
    if manifest and "security_requirements" in manifest:
        for req in manifest["security_requirements"]:
            # Check if the requirement term appears in the design
            req_terms = req.replace("_", " ").split()
            if not any(term.lower() in design_lower for term in req_terms):
                issues.append(
                    f"Security requirement not covered: {req}"
                )

    # ── 7. Technology alignment (from manifest) ──────────────────────────
    if manifest and "technology_constraints" in manifest:
        for tech in manifest["technology_constraints"]:
            if tech.lower() not in design_lower:
                issues.append(
                    f"Required technology not referenced: {tech}"
                )

    # ── 8. PRD cross-reference (keyword matching) ────────────────────────
    if prd and not manifest:
        # Fallback: extract key phrases from PRD and check presence
        prd_requirements = re.findall(r"(?:^|\n)\s*[-*]\s+(.{20,80})", prd)
        for req in prd_requirements[:15]:
            key_terms = [w for w in req.split() if len(w) > 5][:3]
            if key_terms and not any(
                term.lower() in design_lower for term in key_terms
            ):
                issues.append(
                    f"PRD requirement may not be covered: '{req[:60]}'"
                )

    # ── 9. Environment variables ─────────────────────────────────────────
    env_patterns = re.findall(r"[A-Z][A-Z_]{3,}(?:_[A-Z]+)+", design)
    stats["env_vars"] = len(set(env_patterns))
    if "environment" not in design_lower and "env" not in design_lower:
        issues.append("No environment variable documentation found")

    # ── 10. Error handling with status codes ──────────────────────────────
    status_codes = re.findall(r"\b[45]\d{2}\b", design)
    stats["status_codes"] = len(set(status_codes))
    if "error" in design_lower and len(status_codes) < 2:
        issues.append("Error handling section has fewer than 2 status codes")

    # ── Result ───────────────────────────────────────────────────────────
    passed = len(issues) == 0
    result = ValidationResult(passed=passed, issues=issues, stats=stats)

    logger.info(
        "[spec_validator] %s",
        result.summary(),
    )

    return result
