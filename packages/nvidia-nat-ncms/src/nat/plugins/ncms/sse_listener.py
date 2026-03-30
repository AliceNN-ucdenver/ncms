# SPDX-License-Identifier: Apache-2.0
"""Background SSE listener for Knowledge Bus announcements and routed questions.

Connects to the NCMS Hub SSE stream and:
- Stores incoming announcements as memories (so auto_memory_wrapper picks them up)
- Responds to routed questions by searching memory + returning context
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .http_client import NCMSHttpClient

logger = logging.getLogger(__name__)

MAX_BACKOFF_S = 30


# Type alias for the in-process workflow callback
# Signature: async def(input_message: str) -> str
WorkflowCallback = Any


async def sse_listener(
    client: NCMSHttpClient,
    agent_id: str,
    subscribe_to: list[str] | None = None,
    workflow_fn: WorkflowCallback | None = None,
    domains: list[str] | None = None,
    self_port: int | None = None,
) -> None:
    """Long-running SSE consumer. Auto-reconnects with exponential backoff.

    Re-registers with the hub on each reconnect (handles hub restarts).
    If ``workflow_fn`` is provided, routed questions are answered by calling
    the NAT workflow in-process (no HTTP self-call).  If ``self_port`` is
    provided and workflow_fn is not bound, falls back to calling this
    agent's own /generate endpoint for LLM-powered synthesis.

    When ``domains`` is provided, memory searches in question handling are
    filtered to the agent's first domain for domain-specific expertise.
    """
    backoff = 1
    while True:
        try:
            # Re-register on each reconnect (hub may have restarted)
            if domains:
                try:
                    await client.bus_register(agent_id, domains, subscribe_to)
                    logger.info("Re-registered agent %s for domains %s", agent_id, domains)
                except Exception:
                    logger.warning("Re-registration failed for %s, retrying...", agent_id)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    continue

            url = f"{client._base}/api/v1/bus/subscribe?agent_id={agent_id}"
            async with client._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning("SSE stream returned %s", resp.status_code)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    continue

                logger.info("SSE stream connected for agent %s", agent_id)
                backoff = 1  # reset on success

                event_type = ""
                data_buf = ""

                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf += line[5:].strip()
                    elif line == "":
                        # End of SSE message
                        if event_type and data_buf:
                            try:
                                data = json.loads(data_buf)
                                await _handle_event(
                                    client, agent_id, event_type, data,
                                    workflow_fn, domains, self_port,
                                )
                            except json.JSONDecodeError:
                                logger.debug("Non-JSON SSE data: %s", data_buf[:100])
                            except Exception:
                                logger.exception("Error handling SSE event %s", event_type)
                        event_type = ""
                        data_buf = ""

        except asyncio.CancelledError:
            logger.info("SSE listener cancelled for agent %s", agent_id)
            return
        except Exception:
            logger.warning(
                "SSE connection lost for %s (retrying in %ds)", agent_id, backoff,
                exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def _handle_event(
    client: NCMSHttpClient,
    agent_id: str,
    event_type: str,
    data: dict[str, Any],
    workflow_fn: WorkflowCallback | None = None,
    agent_domains: list[str] | None = None,
    self_port: int | None = None,
) -> None:
    """Dispatch a single SSE event."""
    if event_type == "bus.announce":
        await _handle_announcement(client, agent_id, data, workflow_fn, self_port)
    elif event_type == "bus.ask.routed":
        await _handle_question(
            client, agent_id, data, workflow_fn, agent_domains, self_port,
        )
    else:
        logger.debug("Ignoring SSE event type: %s", event_type)


_REVIEW_TRIGGERS = ["ready for review", "build complete", "implementation complete"]


async def _handle_announcement(
    client: NCMSHttpClient,
    agent_id: str,
    data: dict[str, Any],
    workflow_fn: WorkflowCallback | None = None,
    self_port: int | None = None,
) -> None:
    """Store an announcement as a memory so agents can recall it.

    If the announcement matches a review trigger phrase and a ``workflow_fn``
    is available, auto-invokes the agent's workflow to produce a review
    and announces the feedback back to the originating domains.
    """
    content = data.get("content", "")
    domains = data.get("domains", [])
    from_agent = data.get("from_agent", "unknown")

    if not content:
        return

    # Don't store or react to our own announcements
    if from_agent == agent_id:
        return

    # ── Pipeline trigger: bus-based agent chaining ──
    # Check if this announcement is a trigger for THIS agent.
    # Triggers use domain "trigger-{agent_id}" and contain the input message.
    trigger_domain = f"trigger-{agent_id}"
    if trigger_domain in domains:
        logger.info(
            "[trigger] %s received pipeline trigger from %s: %s",
            agent_id, from_agent, content[:100],
        )
        # Self-call /generate inside the sandbox (no port forward needed)
        if self_port:
            asyncio.create_task(_self_generate(agent_id, self_port, content))
        else:
            logger.warning("[trigger] No self_port for %s — cannot process trigger", agent_id)
        return  # Don't store trigger messages as memories

    logger.info(
        "Announcement from %s: %s", from_agent, content[:80],
    )
    await client.store_memory(
        content=f"[Announcement from {from_agent}] {content}",
        type="fact",
        domains=domains,
        source_agent=from_agent,
    )

    # Auto-trigger review if workflow is available and announcement matches
    if workflow_fn:
        content_lower = content.lower()
        if any(trigger in content_lower for trigger in _REVIEW_TRIGGERS):
            logger.info(
                "Auto-triggering review for announcement from %s", from_agent,
            )
            review_prompt = (
                f"Please review the following work from {from_agent} "
                f"and provide your expert feedback. State what looks good "
                f"and what needs improvement:\n\n{content}"
            )
            try:
                review_response = await workflow_fn(review_prompt)
                if review_response:
                    await client.bus_announce(
                        content=f"[Review from {agent_id}] {review_response}",
                        domains=domains or ["general"],
                        from_agent=agent_id,
                    )
                    logger.info(
                        "Auto-review published by %s (len=%d)",
                        agent_id, len(review_response),
                    )
            except Exception:
                logger.warning(
                    "Auto-review failed for %s", agent_id, exc_info=True,
                )


async def _handle_question(
    client: NCMSHttpClient,
    agent_id: str,
    data: dict[str, Any],
    workflow_fn: WorkflowCallback | None = None,
    agent_domains: list[str] | None = None,
    self_port: int | None = None,
) -> None:
    """Respond to a routed question using the NAT workflow.

    Tries three strategies in order:
    1. ``workflow_fn`` — in-process NAT workflow (if late-bound)
    2. Self-call to ``/generate`` — calls this agent's own NAT endpoint
       for full LLM-powered synthesis (if ``self_port`` is set)
    3. Raw memory search — domain-filtered BM25/SPLADE retrieval

    When ``agent_domains`` is provided, memory searches are filtered to
    the agent's primary domain so each expert returns domain-specific
    context (e.g. architect returns ADRs, security returns threat models).
    """
    ask_id = data.get("ask_id", "")
    question = data.get("question", "")
    from_agent = data.get("from_agent", "")

    if not ask_id or not question:
        return

    # Skip questions from ourselves (prevents recursion)
    if from_agent == agent_id:
        logger.debug("Skipping self-routed question from %s", agent_id)
        return

    # Use the agent's primary domain for filtering memory searches
    domain_filter = agent_domains[0] if agent_domains else None

    logger.info(
        "Question from %s: %s (ask_id=%s, domain_filter=%s)",
        from_agent, question[:80], ask_id, domain_filter,
    )

    answer = None
    confidence = 0.5

    # Strategy 1: in-process workflow (if late-bound)
    if workflow_fn is not None:
        try:
            answer = await workflow_fn(question)
            confidence = 0.9
            logger.info("Workflow answered (len=%d)", len(answer) if answer else 0)
        except RuntimeError:
            # workflow_fn not yet bound — expected, fall through
            logger.debug("Workflow not yet bound for %s, trying self-call", agent_id)
        except Exception:
            logger.warning("Workflow call failed, falling back", exc_info=True)

    # Strategy 2: self-call to /generate for LLM synthesis
    if not answer and self_port:
        try:
            import httpx as _httpx

            async with _httpx.AsyncClient(timeout=120.0) as http:
                resp = await http.post(
                    f"http://localhost:{self_port}/generate",
                    json={"input_message": question},
                )
                resp.raise_for_status()
                result = resp.json()
                answer = result.get("value", result.get("output", str(result)))
                confidence = 0.9
                logger.info(
                    "Self-call /generate answered for %s (len=%d)",
                    agent_id, len(answer) if answer else 0,
                )
        except Exception:
            logger.warning(
                "Self-call /generate failed for %s, falling back to memory search",
                agent_id, exc_info=True,
            )

    # Strategy 3: raw memory search (domain-filtered)
    if not answer:
        results = await client.recall_memory(
            query=question, domain=domain_filter, limit=5,
        )
        if results:
            context_parts = []
            for r in results[:5]:
                memory = r.get("memory", r)
                content = memory.get("content", "")
                if content:
                    context_parts.append(content[:500])
            answer = "\n\n".join(context_parts)
            confidence = 0.7
        else:
            answer = f"No relevant information found for: {question}"
            confidence = 0.1

    await client.bus_respond(
        ask_id=ask_id,
        content=answer,
        from_agent=agent_id,
        confidence=confidence,
    )


async def _self_generate(agent_id: str, port: int, message: str) -> None:
    """Fire-and-forget self-call to this agent's /generate endpoint.

    Runs inside the sandbox on localhost — no port forward needed.
    Used by pipeline triggers (bus_announce to trigger-{agent_id}).
    """
    try:
        import httpx as _httpx

        logger.info("[trigger] %s self-calling localhost:%d/generate", agent_id, port)
        async with _httpx.AsyncClient(timeout=600.0) as http:
            resp = await http.post(
                f"http://localhost:{port}/generate",
                json={"input_message": message},
            )
            resp.raise_for_status()
            logger.info("[trigger] %s /generate completed (status %d)", agent_id, resp.status_code)
    except Exception as e:
        logger.warning("[trigger] %s /generate failed: %s", agent_id, e)
