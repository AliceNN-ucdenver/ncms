# SPDX-License-Identifier: Apache-2.0
"""NAT function registrations for NCMS Knowledge Bus tools."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import httpx
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from .http_client import NCMSHttpClient

logger = logging.getLogger(__name__)


# ── ask_knowledge ─────────────────────────────────────────────────────────


class AskKnowledgeConfig(FunctionBaseConfig, name="ask_knowledge"):
    """Ask a question to agents registered for specific domains."""

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub API URL",
    )
    from_agent: str = Field(
        default="nat-agent",
        description="Agent ID of the caller",
    )
    timeout_ms: int = Field(
        default=60000,
        description="How long to wait for a response (ms)",
    )


@register_function(config_type=AskKnowledgeConfig)
async def ask_knowledge(
    config: AskKnowledgeConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Ask a question to domain experts via the NCMS Knowledge Bus."""
    client = NCMSHttpClient(hub_url=config.hub_url)

    async def _ask(question: str, domains: str) -> str:
        """Ask a question to agents in specific domains.

        Args:
            question: The question to ask.
            domains: Comma-separated list of domains to route to.
                Valid domains: architecture, calm-model, quality, decisions
                (routes to architect), security, threats, compliance, controls
                (routes to security agent). Use "architecture,decisions" for
                architecture questions. Use "security,threats" for security.

        Returns:
            The answer from the domain expert, or a fallback message.
        """
        domain_list = [d.strip() for d in domains.split(",") if d.strip()]
        if not domain_list:
            return "Error: no domains specified."

        try:
            result = await client.bus_ask(
                question=question,
                domains=domain_list,
                from_agent=config.from_agent,
                timeout_ms=config.timeout_ms,
            )
            if result.get("answered"):
                agent = result.get("from_agent", "unknown")
                content = result.get("content", "")
                confidence = result.get("confidence", 0)
                return (
                    f"[Answer from {agent} (confidence: {confidence:.1%})]\n{content}"
                )
            return f"No agent responded for domains: {', '.join(domain_list)}"
        except Exception as e:
            logger.exception("ask_knowledge failed")
            return f"Error asking knowledge bus: {e}"

    try:
        yield FunctionInfo.from_fn(
            _ask,
            description=(
                "Ask a question to domain expert agents via the NCMS Knowledge Bus. "
                "Provide the question and comma-separated domains. "
                "Use domains 'architecture,decisions' for the architect agent. "
                "Use domains 'security,threats' for the security agent."
            ),
        )
    finally:
        await client.close()


# ── announce_knowledge ────────────────────────────────────────────────────


class AnnounceKnowledgeConfig(FunctionBaseConfig, name="announce_knowledge"):
    """Broadcast an announcement to subscribed agents."""

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub API URL",
    )
    from_agent: str = Field(
        default="nat-agent",
        description="Agent ID of the announcer",
    )


@register_function(config_type=AnnounceKnowledgeConfig)
async def announce_knowledge(
    config: AnnounceKnowledgeConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Broadcast an announcement to agents subscribed to specific domains."""
    client = NCMSHttpClient(hub_url=config.hub_url)

    async def _announce(content: str, domains: str) -> str:
        """Broadcast an announcement to agents subscribed to specific domains.

        Args:
            content: The announcement content.
            domains: Comma-separated list of target domains
                     (e.g. "architecture,security").

        Returns:
            Confirmation of the broadcast.
        """
        domain_list = [d.strip() for d in domains.split(",") if d.strip()]
        if not domain_list:
            return "Error: no domains specified."

        try:
            result = await client.bus_announce(
                content=content,
                domains=domain_list,
                from_agent=config.from_agent,
            )
            if result.get("announced"):
                return f"Announced to domains: {', '.join(domain_list)}"
            return "Announcement failed."
        except Exception as e:
            logger.exception("announce_knowledge failed")
            return f"Error announcing: {e}"

    try:
        yield FunctionInfo.from_fn(
            _announce,
            description=(
                "Broadcast an announcement to all agents subscribed to specific "
                "domains via the NCMS Knowledge Bus."
            ),
        )
    finally:
        await client.close()


# ── request_approval ──────────────────────────────────────────────────────


class RequestApprovalConfig(FunctionBaseConfig, name="request_approval"):
    """Submit a plan for human approval via the Knowledge Bus.

    Non-blocking: announces the plan to the ``human-approval`` domain and
    persists it in NCMS memory so the dashboard Approval Queue can display
    it across sessions.  Returns immediately without waiting for a response.
    """

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub API URL",
    )
    from_agent: str = Field(
        default="nat-agent",
        description="Agent ID of the requester",
    )


@register_function(config_type=RequestApprovalConfig)
async def request_approval(
    config: RequestApprovalConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Submit a plan for human review and approval (non-blocking)."""
    client = NCMSHttpClient(hub_url=config.hub_url)

    async def _request_approval(plan: str, title: str = "") -> str:
        """Submit a plan for human approval. Returns immediately.

        The plan is announced to the human-approval domain and stored
        in NCMS memory.  A human reviewer will see it in the dashboard
        Approval Queue and can approve, reject, or suggest changes.
        The approval response will arrive later via the approval-response
        domain.

        IMPORTANT: The plan argument must contain the COMPLETE plan text
        that the human needs to review. Include all details, not just a
        summary. The human can only see what you put in the plan argument.
        Include the full expert responses you received and your synthesis.

        Args:
            plan: The COMPLETE plan content for human review. Include all
                  architectural decisions, security requirements, and your
                  synthesis. Markdown formatting is supported. Do NOT
                  abbreviate or summarize — include everything.
            title: Short title for the plan (e.g. "Identity Service Design").

        Returns:
            Confirmation with the plan_id for tracking.
        """
        plan_id = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat()
        title_str = title or "Untitled Plan"

        # Build structured approval content
        approval_content = (
            f"AWAITING_APPROVAL plan_id={plan_id}\n"
            f"Title: {title_str}\n"
            f"From: {config.from_agent}\n"
            f"Submitted: {ts}\n"
            f"---\n"
            f"{plan}"
        )

        try:
            # 1. Announce to human-approval domain (fires SSE to dashboard)
            await client.bus_announce(
                content=approval_content,
                domains=["human-approval"],
                from_agent=config.from_agent,
            )

            # 2. Persist in memory for Approval Queue across sessions
            await client.store_memory(
                content=approval_content,
                type="fact",
                domains=["human-approval"],
                tags=["approval-request", f"plan_id:{plan_id}"],
                importance=8.0,
                source_agent=config.from_agent,
            )

            logger.info(
                "Approval request submitted: plan_id=%s from=%s",
                plan_id, config.from_agent,
            )
            return (
                f"Plan submitted for human approval.\n"
                f"Plan ID: {plan_id}\n"
                f"Title: {title_str}\n"
                f"Status: AWAITING_APPROVAL\n"
                f"The plan is now visible in the dashboard Approval Queue. "
                f"You will receive an announcement on the approval-response "
                f"domain when a human reviews it."
            )
        except Exception as e:
            logger.exception("request_approval failed")
            return f"Error submitting approval request: {e}"

    try:
        yield FunctionInfo.from_fn(
            _request_approval,
            description=(
                "Submit a plan for human review and approval. Non-blocking: "
                "the plan is sent to the dashboard Approval Queue and this "
                "tool returns immediately. The human's decision will arrive "
                "later as a bus announcement on the approval-response domain."
            ),
        )
    finally:
        await client.close()


# ── publish_document ──────────────────────────────────────────────────


class PublishDocumentConfig(FunctionBaseConfig, name="publish_document"):
    """Publish a final design document to the hub document store.

    The document is stored as a markdown file and made available for
    download in the dashboard Documents tab.
    """

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub API URL",
    )
    from_agent: str = Field(
        default="nat-agent",
        description="Agent ID of the publisher",
    )


@register_function(config_type=PublishDocumentConfig)
async def publish_document(
    config: PublishDocumentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Publish a design document to the hub for download."""
    client = NCMSHttpClient(hub_url=config.hub_url)

    async def _publish(content: str, title: str, plan_id: str = "") -> str:
        """Publish a final design document to the NCMS document store.

        The document is stored as a downloadable markdown file in the
        dashboard Documents tab. Use this after your plan has been
        approved to deliver the final implementation document.

        IMPORTANT: The content argument must be the COMPLETE document.
        Include all sections: architecture, security, data models,
        API contracts, deployment, and testing. Use markdown formatting.

        Args:
            content: The COMPLETE document content in markdown format.
            title: Document title (e.g. "IMDb Identity Service Design").
            plan_id: Optional plan_id to link this document to an approval.

        Returns:
            Confirmation with the document URL for download.
        """
        try:
            result = await client.publish_document(
                content=content,
                title=title,
                from_agent=config.from_agent,
                plan_id=plan_id or None,
            )

            doc_id = result.get("document_id", "unknown")
            url = result.get("url", "")

            logger.info(
                "Document published: doc_id=%s from=%s",
                doc_id, config.from_agent,
            )
            return (
                f"Document published successfully.\n"
                f"Document ID: {doc_id}\n"
                f"Title: {title}\n"
                f"URL: {url}\n"
                f"The document is now available in the dashboard Documents tab."
            )
        except Exception as e:
            logger.exception("publish_document failed")
            return f"Error publishing document: {e}"

    try:
        yield FunctionInfo.from_fn(
            _publish,
            description=(
                "Publish a final design document to the NCMS document store. "
                "The document becomes downloadable from the dashboard. Use "
                "this after your plan has been approved to deliver the final "
                "implementation document with full detail."
            ),
        )
    finally:
        await client.close()


# ── web_search ────────────────────────────────────────────────────────


class WebSearchConfig(FunctionBaseConfig, name="web_search"):
    """Search the web using Tavily for research and information gathering."""

    tavily_api_key: str = Field(
        default="",
        description="Tavily API key. Falls back to TAVILY_API_KEY env var.",
    )
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for progress announcements.",
    )
    from_agent: str = Field(
        default="product_owner",
        description="Agent ID for progress announcements.",
    )


@register_function(config_type=WebSearchConfig)
async def web_search(
    config: WebSearchConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Search the web for information using Tavily."""

    api_key = config.tavily_api_key or os.environ.get("TAVILY_API_KEY", "")

    async def _search(
        query: str,
        search_depth: str = "basic",
        max_results: str = "5",
    ) -> str:
        """Search the web for information on a topic using Tavily.

        Use this to research best practices, industry standards, current
        technologies, security guidelines, and other external knowledge
        that the team's internal documents may not cover.

        Args:
            query: The search query (be specific for better results).
            search_depth: "basic" (fast, 1 credit) or "advanced" (thorough, 2 credits).
            max_results: Number of results to return (1-20, default 5).

        Returns:
            Formatted search results with titles, URLs, and content snippets.
            Includes a synthesized answer when available.
        """
        if not api_key:
            return "Error: TAVILY_API_KEY not configured."

        try:
            num_results = min(20, max(1, int(max_results)))
        except (ValueError, TypeError):
            num_results = 5

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Announce progress to hub so dashboard shows live status
                try:
                    await client.post(
                        f"{config.hub_url}/api/v1/bus/announce",
                        json={
                            "from_agent": config.from_agent,
                            "domains": ["research"],
                            "content": f"🌐 Searching web: {query[:80]}...",
                        },
                    )
                except Exception:
                    pass  # Non-fatal — don't block search on announce failure

                resp = await client.post(
                    "https://api.tavily.com/search",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "search_depth": search_depth,
                        "max_results": num_results,
                        "include_answer": "advanced",
                        "include_raw_content": "markdown",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # Format results
            parts = []
            answer = data.get("answer")
            if answer:
                parts.append(f"**Summary:** {answer}\n")

            results = data.get("results", [])
            for i, r in enumerate(results, 1):
                title = r.get("title", "Untitled")
                url = r.get("url", "")
                content = r.get("content", "")[:500]
                raw = r.get("raw_content", "")
                score = r.get("score", 0)
                # Prefer raw markdown content (truncated), fall back to snippet
                body = raw[:2000] if raw else content
                parts.append(
                    f"## [{i}] {title}\n"
                    f"- **URL:** {url}\n"
                    f"- **Relevance:** {score:.2f}\n\n"
                    f"{body}\n"
                )

            if not parts:
                return f"No results found for: {query}"

            return "\n---\n".join(parts)

        except httpx.HTTPStatusError as e:
            logger.warning("Tavily API error: %s", e)
            return f"Search error: {e.response.status_code} {e.response.text[:200]}"
        except Exception as e:
            logger.exception("web_search failed")
            return f"Search error: {e}"

    try:
        yield FunctionInfo.from_fn(
            _search,
            description=(
                "Search the web for information using Tavily. Use this to "
                "research best practices, industry standards, security "
                "guidelines, OWASP recommendations, and current technology "
                "trends. Returns titles, URLs, and content snippets."
            ),
        )
    finally:
        pass  # No client to close — uses per-request httpx


# ── create_prd ────────────────────────────────────────────────────────

_PRD_TEMPLATE = """# {title}

## 1. Problem Statement
{problem_statement}

## 2. Goals and Non-Goals

### Goals
{goals}

### Non-Goals
{non_goals}

## 3. User Stories
{user_stories}

## 4. Technical Requirements
{technical_requirements}

## 5. Security Requirements
{security_requirements}

## 6. Constraints
{constraints}

## 7. Success Metrics
{success_metrics}

## 8. References
{references}
"""


class CreatePRDConfig(FunctionBaseConfig, name="writeprd"):
    """Create a PRD from research and publish it to the document store."""

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing.",
    )
    from_agent: str = Field(
        default="product_owner",
        description="Agent ID to tag on the published document.",
    )


@register_function(config_type=CreatePRDConfig)
async def create_prd(
    config: CreatePRDConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Create a PRD document from research and publish it."""

    async def _create_prd(
        title: str,
        content: str,
    ) -> str:
        """Create a PRD (Product Requirements Document) and publish it.

        Call this AFTER you have completed your web_search research.
        Pass the full PRD content as markdown text. Include sections for:
        Problem Statement, Goals, User Stories, Technical Requirements,
        Security Requirements, and References.

        Args:
            title: Short document title (e.g. "IMDB Identity Service PRD").
            content: The full PRD content in markdown format.

        Returns:
            Confirmation with the document ID and URL.
        """
        prd_content = f"# {title}\n\n{content}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Announce progress
                try:
                    await client.post(
                        f"{config.hub_url}/api/v1/bus/announce",
                        json={
                            "from_agent": config.from_agent,
                            "domains": ["product"],
                            "content": f"📝 Creating PRD: {title[:80]}",
                        },
                    )
                except Exception:
                    pass

                resp = await client.post(
                    f"{config.hub_url}/api/v1/documents",
                    json={
                        "title": title,
                        "content": prd_content,
                        "from_agent": config.from_agent,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                doc_id = data.get("document_id", "unknown")
                return (
                    f"PRD published successfully.\n"
                    f"- Document ID: {doc_id}\n"
                    f"- Title: {title}\n"
                    f"- Size: {len(prd_content)} characters\n"
                    f"- URL: /documents/{doc_id}.md\n\n"
                    f"Other agents can now access this PRD."
                )
        except Exception as e:
            logger.exception("create_prd failed to publish")
            return f"Error publishing PRD: {e}"

    try:
        yield FunctionInfo.from_fn(
            _create_prd,
            description=(
                "Create a PRD and publish it. Takes two arguments: "
                "title (short name) and content (full PRD in markdown). "
                "Call AFTER web_search. The document is published to the "
                "document store for other agents to use."
            ),
        )
    finally:
        pass


# ── create_design ─────────────────────────────────────────────────────

_DESIGN_TEMPLATE = """# {title}

## Role
Implementation architect producing a detailed design from PRD and expert input.

## Context
**PRD Reference:** {prd_reference}

## Architecture Overview
{architecture_overview}

## Component Design
{component_design}

## API Contracts
{api_contracts}

## Data Models
{data_models}

## Security Controls
{security_controls}

## Deployment Strategy
{deployment_strategy}

## Testing Approach
{testing_approach}

## Requirements Traceability
- PRD: {prd_reference}
- Architecture input incorporated
- Security controls mapped to threats
"""


class CreateDesignConfig(FunctionBaseConfig, name="writedesign"):
    """Create an implementation design document and publish it."""

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing.",
    )
    from_agent: str = Field(
        default="builder",
        description="Agent ID to tag on the published document.",
    )


@register_function(config_type=CreateDesignConfig)
async def create_design(
    config: CreateDesignConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Create an implementation design document and publish it."""

    async def _create_design(
        title: str,
        content: str,
    ) -> str:
        """Create an implementation design document and publish it.

        Call this AFTER you have consulted the architect and security agents
        via ask_knowledge. Pass the full coding implementation design as
        markdown. This must be a TypeScript implementation plan, NOT an ADR.
        Include: project structure, API endpoints, TypeScript interfaces,
        database schemas, middleware, security implementations, config,
        error handling, test cases, and deployment. Include code snippets.

        Args:
            title: Short document title (e.g. "IMDB Identity Service Implementation Design").
            content: The full implementation design in markdown with TypeScript code snippets.

        Returns:
            Confirmation with the document ID and URL.
        """
        design_content = f"# {title}\n\n{content}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Announce progress
                try:
                    await client.post(
                        f"{config.hub_url}/api/v1/bus/announce",
                        json={
                            "from_agent": config.from_agent,
                            "domains": ["implementation"],
                            "content": f"📐 Creating design: {title[:80]}",
                        },
                    )
                except Exception:
                    pass

                resp = await client.post(
                    f"{config.hub_url}/api/v1/documents",
                    json={
                        "title": title,
                        "content": design_content,
                        "from_agent": config.from_agent,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                doc_id = data.get("document_id", "unknown")
                return (
                    f"Design document published successfully.\n"
                    f"- Document ID: {doc_id}\n"
                    f"- Title: {title}\n"
                    f"- Size: {len(design_content)} characters\n"
                    f"- URL: /documents/{doc_id}.md\n\n"
                    f"The design is now available in the document store."
                )
        except Exception as e:
            logger.exception("create_design failed to publish")
            return f"Error publishing design: {e}"

    try:
        yield FunctionInfo.from_fn(
            _create_design,
            description=(
                "Create a coding implementation design and publish it. Takes "
                "title and content (full TypeScript implementation plan in "
                "markdown with code snippets). NOT an ADR — a coding plan. "
                "Call AFTER ask_knowledge."
            ),
        )
    finally:
        pass
