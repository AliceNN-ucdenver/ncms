"""NCMS Bus Agent Sidecar — SSE client for OpenShell sandboxes.

Runs alongside OpenClaw in each sandbox. Maintains an SSE connection to
the NCMS Hub, handles incoming questions (search + LLM synthesis) and
stores incoming announcements.

Usage:
    ncms bus-agent --hub http://ncms-hub:8080 \
        --agent-id architect \
        --domains architecture,calm-model \
        --subscribe-to security,identity-service

The sidecar:
1. Registers with the Hub (POST /api/v1/bus/register)
2. Connects SSE stream (GET /api/v1/bus/subscribe?agent_id=...)
3. On question: searches Hub memory → synthesizes answer via LLM → POSTs response
4. On announcement: stores into Hub memory for future recall
5. Reconnects with exponential backoff on disconnect
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# Max chars of memory context for LLM answer synthesis
_CONTEXT_MAX_CHARS = 3000

# Reconnect backoff: 1s, 2s, 4s, 8s, 16s, 30s max
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0


class BusAgentSidecar:
    """Lightweight bus agent that connects to an NCMS Hub via HTTP + SSE."""

    def __init__(
        self,
        hub_url: str,
        agent_id: str,
        domains: list[str],
        subscribe_to: list[str] | None = None,
        llm_model: str | None = None,
        llm_api_base: str | None = None,
        system_prompt: str = "You are a helpful agent. Answer questions "
        "based on the provided context.",
        startup_questions: list[dict] | None = None,
    ):
        self.hub_url = hub_url.rstrip("/")
        self.agent_id = agent_id
        self.domains = domains
        self.subscribe_to = subscribe_to or []
        self.llm_model = llm_model
        self.llm_api_base = llm_api_base
        self.system_prompt = system_prompt
        self.startup_questions = startup_questions or []
        self._running = True
        self._client: httpx.AsyncClient | None = None
        self._consulted = False

    @property
    def _http(self) -> httpx.AsyncClient:
        """Return the active HTTP client (always non-None after run() starts)."""
        assert self._client is not None, "BusAgentSidecar._http called before run()"
        return self._client

    async def run(self) -> None:
        """Main loop: register → connect SSE → handle events → reconnect on failure."""
        import httpx

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        client = self._client
        backoff = _INITIAL_BACKOFF

        try:
            while self._running:
                try:
                    await self._register()
                    # Run startup consultations once (in background)
                    if not self._consulted and self.startup_questions:
                        self._consulted = True
                        asyncio.create_task(self._run_startup_questions())
                    await self._consume_sse()
                    # If SSE stream ends cleanly, reconnect
                    backoff = _INITIAL_BACKOFF
                except httpx.ConnectError as e:
                    logger.warning("Connection to hub failed: %s (retrying in %.0fs)", e, backoff)
                except httpx.ReadError as e:
                    logger.warning("SSE stream read error: %s (retrying in %.0fs)", e, backoff)
                except Exception:
                    logger.exception("Unexpected error (retrying in %.0fs)", backoff)

                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
        finally:
            # Deregister on shutdown
            import contextlib
            with contextlib.suppress(Exception):
                await self._deregister()
            await client.aclose()

    async def _register(self) -> None:
        """Register with the NCMS Hub."""
        resp = await self._http.post(f"{self.hub_url}/api/v1/bus/register", json={
            "agent_id": self.agent_id,
            "domains": self.domains,
            "subscribe_to": self.subscribe_to or self.domains,
        })
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "Registered agent %s for domains %s",
            self.agent_id, data.get("domains"),
        )

    async def _deregister(self) -> None:
        """Deregister from the NCMS Hub."""
        await self._http.post(f"{self.hub_url}/api/v1/bus/deregister", json={
            "agent_id": self.agent_id,
        })
        logger.info("Deregistered agent %s", self.agent_id)

    async def _consume_sse(self) -> None:
        """Connect to SSE stream and process events."""
        url = f"{self.hub_url}/api/v1/bus/subscribe?agent_id={self.agent_id}"
        logger.info("Connecting SSE stream: %s", url)

        async with self._http.stream("GET", url) as response:
            response.raise_for_status()
            logger.info("SSE stream connected for agent %s", self.agent_id)

            event_type = ""
            data_lines: list[str] = []

            async for line in response.aiter_lines():
                if not self._running:
                    break

                line = line.rstrip()

                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and data_lines:
                    # End of event — process it
                    raw = "\n".join(data_lines)
                    data_lines = []
                    try:
                        event_data = json.loads(raw)
                        etype = event_type or event_data.get("type", "")
                        await self._handle_event(etype, event_data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid SSE JSON: %s", raw[:200])
                    event_type = ""
                elif line.startswith(":"):
                    # Comment (keepalive)
                    pass

    async def _handle_event(self, event_type: str, data: dict) -> None:
        """Dispatch an SSE event to the appropriate handler."""
        if event_type == "bus.ask.routed":
            await self._handle_question(data)
        elif event_type == "bus.announce":
            await self._handle_announcement(data)
        else:
            logger.debug("Ignoring event type: %s", event_type)

    async def _handle_question(self, data: dict) -> None:
        """Handle an incoming question: search memory → synthesize → respond."""
        ask_id = data.get("ask_id", "")
        question = data.get("question", "")
        from_agent = data.get("from_agent", "")

        logger.info(
            "Question from %s: %s (ask_id=%s)",
            from_agent, question[:100], ask_id,
        )

        try:
            # Search NCMS Hub for relevant knowledge (try recall, fall back to search)
            resp = await self._http.get(
                f"{self.hub_url}/api/v1/memories/recall",
                params={"q": question, "limit": "5"},
            )
            if resp.status_code != 200:
                logger.warning("Recall failed (%s), falling back to search", resp.status_code)
                resp = await self._http.get(
                    f"{self.hub_url}/api/v1/memories/search",
                    params={"q": question, "limit": "5"},
                )
            resp.raise_for_status()
            results = resp.json().get("results", [])

            # Build context from results
            context_parts: list[str] = []
            total_len = 0
            for r in results:
                snippet = r.get("content", "")[:500]
                if total_len + len(snippet) > _CONTEXT_MAX_CHARS:
                    break
                context_parts.append(snippet)
                total_len += len(snippet)

            # Synthesize answer
            answer = await self._synthesize(question, context_parts)
            confidence = min(0.92, 0.6 + 0.05 * len(context_parts))

            # POST response back to Hub
            resp = await self._http.post(f"{self.hub_url}/api/v1/bus/respond", json={
                "ask_id": ask_id,
                "from_agent": self.agent_id,
                "content": answer,
                "confidence": confidence,
            })
            resp.raise_for_status()
            logger.info("Responded to ask %s (confidence=%.2f)", ask_id, confidence)

        except Exception:
            logger.exception("Failed to handle question %s", ask_id)

    async def _handle_announcement(self, data: dict) -> None:
        """Store an announcement into NCMS memory."""
        content = data.get("content", "")
        from_agent = data.get("from_agent", "")
        domains = data.get("domains", [])
        event = data.get("event", "updated")

        logger.info(
            "Announcement from %s [%s]: %s",
            from_agent, event, content[:100],
        )

        try:
            resp = await self._http.post(f"{self.hub_url}/api/v1/memories", json={
                "content": f"[{event} from {from_agent}] {content}",
                "type": "fact",
                "domains": domains,
                "source_agent": self.agent_id,
                "importance": 7.0,
            })
            resp.raise_for_status()
            logger.info("Stored announcement from %s", from_agent)
        except Exception:
            logger.exception("Failed to store announcement from %s", from_agent)

    async def _run_startup_questions(self) -> None:
        """Ask startup questions to other agents via the bus."""
        # Small delay to let all agents connect
        await asyncio.sleep(10)
        for q in self.startup_questions:
            if not self._running:
                break
            question = q.get("question", "")
            domains = q.get("domains", [])
            if not question or not domains:
                continue
            logger.info(
                "[startup] Asking: %s (domains=%s)", question[:80], domains,
            )
            try:
                resp = await self._http.post(
                    f"{self.hub_url}/api/v1/bus/ask",
                    json={
                        "from_agent": self.agent_id,
                        "question": question,
                        "domains": domains,
                        "timeout_ms": 90000,
                    },
                    timeout=120.0,
                )
                data = resp.json()
                if data.get("answered"):
                    answer = data.get("content", "")
                    logger.info(
                        "[startup] Got answer from %s: %s",
                        data.get("from_agent", "?"),
                        answer[:120],
                    )
                    # Store the answer as knowledge
                    await self._http.post(
                        f"{self.hub_url}/api/v1/memories",
                        json={
                            "content": (
                                f"[consultation] Q: {question}\n"
                                f"A ({data.get('from_agent','?')}): "
                                f"{answer}"
                            ),
                            "type": "fact",
                            "domains": self.domains,
                            "source_agent": self.agent_id,
                            "importance": 6.0,
                        },
                    )
                else:
                    logger.info("[startup] No answer for: %s", question[:80])
            except Exception:
                logger.warning(
                    "[startup] Failed to ask: %s", question[:80],
                    exc_info=True,
                )
            # Pace between questions
            await asyncio.sleep(5)

    async def _synthesize(self, question: str, context_parts: list[str]) -> str:
        """Synthesize an answer from context, optionally via LLM."""
        context_block = "\n---\n".join(context_parts) if context_parts else "(no context found)"

        # If no LLM configured, return raw context
        if not self.llm_model:
            if context_parts:
                return context_parts[0]
            return "(no relevant knowledge found)"

        # Call LLM for synthesis
        try:
            import litellm

            user_message = (
                f"## Question\n{question}\n\n"
                f"## Relevant Knowledge\n{context_block}\n\n"
                "Provide a concise, expert answer. Cite specific IDs, references, "
                "or document sections where possible."
            )

            kwargs: dict = dict(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=800,
            )
            if self.llm_api_base:
                kwargs["api_base"] = self.llm_api_base
                if self.llm_model.startswith("openai/"):
                    kwargs["api_key"] = "na"
            # Disable thinking mode for reasoning models
            if self.llm_model.startswith("ollama"):
                kwargs["think"] = False
            elif any(name in self.llm_model.lower() for name in ("nemotron", "qwen")):
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False},
                }

            response = await litellm.acompletion(**kwargs)
            raw = response.choices[0].message.content
            return raw.strip() if raw else context_parts[0] if context_parts else ""
        except Exception:
            logger.warning("LLM synthesis failed, returning raw context", exc_info=True)
            return context_parts[0] if context_parts else "(no relevant knowledge found)"

    def stop(self) -> None:
        """Signal the sidecar to stop."""
        self._running = False


def run_bus_agent(
    hub_url: str,
    agent_id: str,
    domains: str,
    subscribe_to: str | None = None,
    llm_model: str | None = None,
    llm_api_base: str | None = None,
    system_prompt: str | None = None,
    startup_questions_json: str | None = None,
) -> None:
    """Entry point for the bus agent sidecar CLI."""
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    domain_list = [d.strip() for d in domains.split(",") if d.strip()]
    sub_list = [d.strip() for d in (subscribe_to or "").split(",") if d.strip()] or None

    # Parse startup questions from JSON env var or CLI arg
    startup_questions: list[dict] = []
    sq_json = startup_questions_json or os.environ.get(
        "NCMS_STARTUP_QUESTIONS", "",
    )
    if sq_json:
        try:
            startup_questions = json.loads(sq_json)
        except json.JSONDecodeError:
            logger.warning("Invalid NCMS_STARTUP_QUESTIONS JSON")

    sidecar = BusAgentSidecar(
        hub_url=hub_url,
        agent_id=agent_id,
        domains=domain_list,
        subscribe_to=sub_list,
        llm_model=llm_model,
        llm_api_base=llm_api_base,
        system_prompt=system_prompt or (
            "You are a helpful agent. Answer questions "
            "based on the provided context."
        ),
        startup_questions=startup_questions,
    )

    # Handle SIGINT/SIGTERM gracefully
    def _handle_signal(sig, frame):
        logger.info("Received signal %s, shutting down", sig)
        sidecar.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Starting NCMS Bus Agent: agent_id=%s, domains=%s, hub=%s",
        agent_id, domain_list, hub_url,
    )
    asyncio.run(sidecar.run())
