# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based PRD (Product Requirements Document) agent for NAT/NCMS.

Deterministic pipeline: read_document → ask_experts → synthesize_prd → publish_prd → verify_and_trigger.
LLM called exactly once (synthesis). All other nodes are pure Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig

from .http_client import NCMSHttpClient
from .pipeline_utils import extract_project_id, extract_research_id, extract_topic, emit_telemetry, build_design_trigger, check_interrupt, traced_llm_call, snapshot_agent_config, ensure_langchain_instrumented
from .prd_prompts import SYNTHESIZE_PRD_PROMPT, MANIFEST_PROMPT

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class PRDState(TypedDict):
    """Graph state for the PRD pipeline."""

    topic: str  # Research topic / PRD subject
    source_doc_id: str | None  # Researcher's doc ID (parsed from input)
    source_content: str  # Content from source document
    research_id: str | None  # Research doc ID (for passing to design trigger)
    research_methodology: dict  # Structured research metadata (query plans, results)
    expert_input: dict[str, str]  # {"architect": "...", "security": "..."}
    prd: str  # Synthesized PRD markdown
    manifest: dict  # Structured requirements manifest (JSON)
    document_id: str | None  # Published PRD doc ID
    messages: list[BaseMessage]  # LangGraph compat
    project_id: str | None  # PRJ-XXXXXXXX for pipeline tracking
    interrupted: bool


# ── Agent ─────────────────────────────────────────────────────────────────────


class PRDAgent:
    """Deterministic LangGraph PRD pipeline.

    Nodes: read_document → ask_experts → synthesize_prd → publish_prd → verify_and_trigger
    LLM called once: synthesize_prd.
    All other nodes are pure Python.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        client: NCMSHttpClient,
        llm_thinking: BaseChatModel | None = None,
        trigger_next_agent: bool = True,
    ) -> None:
        self.llm = llm
        self.llm_thinking = llm_thinking or llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.client = client
        self.trigger_next_agent = trigger_next_agent

    async def _check_and_interrupt(self, state: PRDState, node: str) -> bool:
        """Check for interrupt signal. Returns True if interrupted."""
        if state.get("interrupted"):
            return True
        if await check_interrupt(self.hub_url, self.from_agent):
            state["interrupted"] = True
            logger.info("[prd_agent] Interrupted at node %s", node)
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, node, "interrupted",
            )
            return True
        return False

    async def check_guardrails(self, state: PRDState) -> PRDState:
        """Check input guardrails. Block/reject violations pause for human approval."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "started")
        await snapshot_agent_config(self.client, state.get("project_id"), self.from_agent, self.llm)
        from .guardrails import request_approval, run_input_guardrails, wait_for_approval
        can_proceed, violations = await run_input_guardrails(self.hub_url, state["topic"], self.from_agent)
        if not can_proceed:
            logger.warning("[prd_agent] Guardrails BLOCKED: %s", violations)
            blocking = [v for v in violations if v.escalation in ("block", "reject")]
            if blocking:
                await emit_telemetry(
                    self.hub_url, state.get("project_id"), self.from_agent,
                    "check_guardrails", "awaiting_approval",
                    f"{len(blocking)} violation(s) require human approval",
                )
                approval_id = await request_approval(
                    self.hub_url, self.from_agent, "check_guardrails",
                    state.get("project_id"), blocking,
                    context={"topic": state["topic"]},
                )
                if approval_id:
                    decision, comment = await wait_for_approval(
                        self.hub_url, approval_id, self.from_agent,
                    )
                    if decision == "approved":
                        logger.info("[prd_agent] Human approved guardrail override")
                        can_proceed = True
                    else:
                        state["prd"] = f"Pipeline denied by human: {decision}"
                        state["interrupted"] = True
                        await emit_telemetry(
                            self.hub_url, state.get("project_id"), self.from_agent,
                            "check_guardrails", "denied", f"Human denied: {comment or decision}",
                        )
                        return state
                else:
                    state["prd"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
            else:
                state["prd"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "completed")
        return state

    async def generate_manifest(self, state: PRDState) -> PRDState:
        """Generate structured requirements manifest. Second LLM call."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "generate_manifest", "started")
        if await self._check_and_interrupt(state, "generate_manifest"):
            return state
        prd = state["prd"]
        try:
            prompt = MANIFEST_PROMPT.format(prd_content=prd[:15000])
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="Output only valid JSON. No markdown, no explanation."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="generate_manifest",
            )
            text = response.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
            state["manifest"] = json.loads(text)
            logger.info("[prd_agent] Manifest: %d endpoints, %d security reqs",
                        len(state["manifest"].get("endpoints", [])),
                        len(state["manifest"].get("security_requirements", [])))
        except Exception as e:
            logger.warning("[prd_agent] Manifest generation failed: %s", e)
            state["manifest"] = {}
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "generate_manifest", "completed")
        return state

    def _after_guardrails(self, state: PRDState) -> str:
        """Route: if guardrails denied, end the graph immediately."""
        if state.get("interrupted"):
            return END
        return "continue"

    async def build_graph(self) -> StateGraph:
        """Build and compile the deterministic PRD pipeline."""
        graph = StateGraph(PRDState)

        graph.add_node("check_guardrails", self.check_guardrails)
        graph.add_node("read_document", self.read_document)
        graph.add_node("ask_experts", self.ask_experts)
        graph.add_node("synthesize_prd", self.synthesize_prd)
        graph.add_node("generate_manifest", self.generate_manifest)
        graph.add_node("publish_prd", self.publish_prd)
        graph.add_node("verify_and_trigger", self.verify_and_trigger)

        # Guardrail gate → conditional: denied ends the graph
        graph.add_edge(START, "check_guardrails")
        graph.add_conditional_edges(
            "check_guardrails",
            self._after_guardrails,
            {"continue": "read_document", END: END},
        )
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_prd")
        graph.add_edge("synthesize_prd", "generate_manifest")
        graph.add_edge("generate_manifest", "publish_prd")
        graph.add_edge("publish_prd", "verify_and_trigger")
        graph.add_edge("verify_and_trigger", END)

        compiled = graph.compile()
        logger.info(
            "[prd_agent] Graph compiled: check_guardrails → read_document → ask_experts"
            " → synthesize_prd → generate_manifest → publish_prd → verify_and_trigger"
        )
        return compiled

    # ── Node 1: Read Document (Pure Python) ──────────────────────────────

    async def read_document(self, state: PRDState) -> PRDState:
        """Parse doc_id from input and fetch the researcher's report. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "read_document", "started")
        if await self._check_and_interrupt(state, "read_document"):
            return state
        topic = state["topic"]
        logger.info("[prd_agent] Reading source document for topic: %s", topic[:100])

        # Parse (research_id: XXXX) from the input message
        doc_id = extract_research_id(topic)
        if doc_id:
            state["source_doc_id"] = doc_id
            logger.info("[prd_agent] Found research_id in input: %s", doc_id)

            try:
                doc = await self.client.read_document(doc_id)
                content = doc.get("content", "")
                state["source_content"] = content
                state["research_id"] = doc_id
                # Extract research methodology from document metadata
                metadata = doc.get("metadata", {})
                if isinstance(metadata, str):
                    import json as _json
                    metadata = _json.loads(metadata) if metadata else {}
                state["research_methodology"] = metadata.get(
                    "research_methodology", {},
                )
                logger.info(
                    "[prd_agent] Read source document: %s (%d chars, methodology=%s, doc_keys=%s)",
                    doc_id, len(content),
                    "yes" if state["research_methodology"] else "no",
                    list(doc.keys())[:10],
                )
            except Exception as e:
                logger.warning("[prd_agent] Failed to read document %s: %s", doc_id, e)
                state["source_content"] = ""
        else:
            logger.info("[prd_agent] No research_id found in input — standalone mode")
            state["source_doc_id"] = None
            state["source_content"] = ""

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "read_document", "completed", f"doc_id={state.get('source_doc_id')}")
        return state

    # ── Node 2: Ask Experts (Pure Python) ────────────────────────────────

    async def ask_experts(self, state: PRDState) -> PRDState:
        """Parallel bus_ask to architect and security experts. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "ask_experts", "started")
        if await self._check_and_interrupt(state, "ask_experts"):
            return state
        topic = state["topic"]
        logger.info("[prd_agent] Asking experts about: %s", topic[:100])

        # Document-by-reference: pass doc_id + entity keywords to experts
        source_doc_id = state.get("source_doc_id")
        project_id = state.get("project_id", "")
        entity_keywords = ""
        if source_doc_id:
            try:
                doc_meta = await self.client.read_document(source_doc_id)
                doc_entities = doc_meta.get("entities", [])
                if doc_entities:
                    entity_keywords = ", ".join(e["name"] for e in doc_entities[:10])
                    logger.info(
                        "[prd_agent] Expert query: doc_id=%s entities=%s (ref-only)",
                        source_doc_id, entity_keywords[:80],
                    )
            except Exception as e:
                logger.debug("[prd_agent] Failed to fetch entity metadata: %s", e)

        # Build research context for expert questions
        research_context = ""
        methodology = state.get("research_methodology", {})
        if methodology:
            summary = methodology.get("results_summary", {})
            top = methodology.get("top_sources", {})
            parts = []
            if summary:
                parts.append(
                    f"Research: {summary.get('web', 0)} web, "
                    f"{summary.get('arxiv', 0)} papers, "
                    f"{summary.get('patent', 0)} patents, "
                    f"{summary.get('community', 0)} community"
                )
            if top.get("patent"):
                parts.append(f"Patents: {', '.join(top['patent'][:3])}")
            if top.get("community"):
                parts.append(f"Community: {', '.join(top['community'][:3])}")
            if parts:
                research_context = "\n" + "\n".join(parts)

        # Announce progress to bus
        try:
            await self.client.bus_announce(
                content=f"Consulting architecture and security experts for: {topic[:80]}",
                domains=["product", "requirements"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass  # Non-fatal

        async def _ask_architect() -> str:
            try:
                question = (
                    f"[NCMS:question]\n"
                    f"What architectural decisions and patterns apply to this project?\n"
                    f"(doc_id: {source_doc_id}) (project_id: {project_id})\n"
                    f"Key entities: {entity_keywords}"
                    f"{research_context}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["architecture", "decisions"],
                    from_agent=self.from_agent,
                    timeout_ms=300000,
                )
                response = result.get("response", result.get("content", ""))
                logger.info("[prd_agent] Architect response: %d chars", len(response))
                return response
            except Exception as e:
                logger.warning("[prd_agent] Architect ask failed: %s", e)
                return ""

        async def _ask_security() -> str:
            try:
                question = (
                    f"[NCMS:question]\n"
                    f"What security threats and requirements apply to this project?\n"
                    f"(doc_id: {source_doc_id}) (project_id: {project_id})\n"
                    f"Key entities: {entity_keywords}"
                    f"{research_context}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["security", "threats"],
                    from_agent=self.from_agent,
                    timeout_ms=300000,
                )
                response = result.get("response", result.get("content", ""))
                logger.info("[prd_agent] Security response: %d chars", len(response))
                return response
            except Exception as e:
                logger.warning("[prd_agent] Security ask failed: %s", e)
                return ""

        architect_resp, security_resp = await asyncio.gather(
            _ask_architect(), _ask_security()
        )

        state["expert_input"] = {
            "architect": architect_resp,
            "security": security_resp,
        }

        logger.info(
            "[prd_agent] Expert input collected — architect: %d chars, security: %d chars",
            len(architect_resp),
            len(security_resp),
        )

        # Announce completion
        try:
            await self.client.bus_announce(
                content=(
                    f"Expert consultation complete for: {topic[:80]}\n"
                    f"Architect: {len(architect_resp)} chars | Security: {len(security_resp)} chars"
                ),
                domains=["product", "requirements"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "ask_experts", "completed", f"architect={len(architect_resp)} security={len(security_resp)}")
        return state

    # ── Node 3: Synthesize PRD (LLM) ─────────────────────────────────────

    async def synthesize_prd(self, state: PRDState) -> PRDState:
        """LLM synthesizes source document and expert input into a structured PRD."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_prd", "started")
        if await self._check_and_interrupt(state, "synthesize_prd"):
            return state
        topic = state["topic"]
        logger.info("[prd_agent] Synthesizing PRD for: %s", topic[:100])

        # Truncate inputs to fit context window
        source_content = state.get("source_content", "")
        if len(source_content) > 80000:
            source_content = source_content[:80000] + "\n\n[... truncated for context window ...]"

        expert = state.get("expert_input", {})
        architect_input = expert.get("architect", "No architect input available.")
        if len(architect_input) > 15000:
            architect_input = architect_input[:15000] + "\n\n[... truncated ...]"

        security_input = expert.get("security", "No security input available.")
        if len(security_input) > 15000:
            security_input = security_input[:15000] + "\n\n[... truncated ...]"

        if not source_content:
            source_content = "No source document provided. Generate PRD from topic and expert input."

        prompt = SYNTHESIZE_PRD_PROMPT.format(
            topic=topic,
            source_content=source_content,
            architect_input=architect_input,
            security_input=security_input,
        )

        try:
            response = await traced_llm_call(
                self.llm_thinking, [
                    SystemMessage(
                        content=(
                            "You are a senior product owner using semi-formal certificate "
                            "format. Follow the structure exactly. Every requirement must "
                            "trace to a specific research finding or expert recommendation."
                        )
                    ),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="synthesize_prd",
            )
            state["prd"] = response.content

            # Note: LangChain strips reasoning_content from the response.
            # CoT still happens server-side — verify via token count in audit record.
            logger.info("[prd_agent] PRD synthesized: %d chars", len(state["prd"]))
        except Exception as e:
            logger.error("[prd_agent] PRD synthesis failed: %s", e)
            # Emergency fallback — return a skeleton PRD
            state["prd"] = (
                f"# {topic} — Product Requirements Document\n\n"
                f"## Source Content\n\n{source_content[:5000]}\n\n"
                f"## Architect Input\n\n{architect_input}\n\n"
                f"## Security Input\n\n{security_input}\n\n"
                f"*PRD synthesis failed — raw inputs provided above.*"
            )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_prd", "completed", f"{len(state['prd'])} chars")
        return state

    # ── Node 4: Publish PRD (Pure Python) ─────────────────────────────────

    async def publish_prd(self, state: PRDState) -> PRDState:
        """Publish the PRD to the NCMS document store. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish_prd", "started")
        if await self._check_and_interrupt(state, "publish_prd"):
            return state
        topic = state["topic"]
        clean_topic = extract_topic(topic)
        prd = state["prd"]
        # Tag content with project_id for traceability
        project_id = state.get("project_id")
        if project_id:
            prd = f"<!-- project_id: {project_id} -->\n{prd}"
        title = f"{clean_topic} — PRD"
        logger.info("[prd_agent] Publishing PRD: %d chars", len(prd))

        try:
            result = await self.client.publish_document(
                content=prd,
                title=title,
                from_agent=self.from_agent,
                doc_type="prd",
                source_format="markdown",
                domains=["product", "requirements"],
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[prd_agent] PRD published: %s (type=prd)", doc_id)

            # Create traceability link: PRD → Research Report
            source_doc_id = state.get("source_doc_id")
            if source_doc_id and doc_id != "unknown":
                try:
                    await self.client.create_document_link(
                        source_doc_id=doc_id,
                        target_doc_id=source_doc_id,
                        link_type="derived_from",
                    )
                    logger.info("[prd_agent] Link: %s derived_from %s", doc_id, source_doc_id)
                except Exception as e:
                    logger.debug("[prd_agent] Link creation failed: %s", e)

            # Announce to the bus
            try:
                await self.client.bus_announce(
                    content=(
                        f"PRD published: {topic}\n"
                        f"Document ID: {doc_id}\n"
                        f"Size: {len(prd)} chars"
                    ),
                    domains=["product", "requirements"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[prd_agent] Publish failed: %s", e)
            state["document_id"] = None

        # Publish the requirements manifest as a separate document if it exists
        manifest = state.get("manifest", {})
        if manifest:
            try:
                manifest_content = f"<!-- project_id: {project_id} -->\n" if project_id else ""
                manifest_content += f"# {clean_topic} — Requirements Manifest\n\n```json\n{json.dumps(manifest, indent=2)}\n```"
                await self.client.publish_document(
                    content=manifest_content,
                    title=f"{clean_topic} — Requirements Manifest",
                    from_agent=self.from_agent,
                    doc_type="manifest",
                    source_format="markdown",
                    domains=["product", "requirements"],
                )
                logger.info("[prd_agent] Requirements manifest published")
            except Exception as e:
                logger.warning("[prd_agent] Failed to publish manifest: %s", e)

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish_prd", "completed", f"doc_id={state.get('document_id')}")
        return state

    # ── Node 5: Verify and Trigger (Pure Python) ─────────────────────────

    async def verify_and_trigger(self, state: PRDState) -> PRDState:
        """Verify pipeline completion and optionally trigger the designer agent."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify_and_trigger", "started")
        if await self._check_and_interrupt(state, "verify_and_trigger"):
            return state
        topic = state["topic"]
        doc_id = state.get("document_id")
        prd = state.get("prd", "")

        if doc_id:
            logger.info(
                "[prd_agent] Pipeline complete. Topic: '%s' | Doc: %s | PRD: %d chars",
                topic[:60],
                doc_id,
                len(prd),
            )
        else:
            logger.error(
                "[prd_agent] Pipeline FAILED — no document published for: %s",
                topic[:60],
            )
            project_id = state.get("project_id")
            if project_id:
                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=10.0) as hc:
                        await hc.post(f"{self.hub_url}/api/v1/projects/{project_id}/fail")
                except Exception:
                    pass
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "verify_and_trigger", "failed",
                "No document published — PRD synthesis returned empty",
            )
            return state

        # Trigger designer agent (fire-and-forget — don't block the return)
        if self.trigger_next_agent and doc_id:
            import asyncio

            clean_topic = extract_topic(topic)
            logger.info("[prd_agent] 🔗 Triggering designer (async) with doc_id: %s", doc_id)

            # Announce handoff so it's visible in the dashboard
            try:
                await self.client.bus_announce(
                    content=f"🔗 Handing off to Designer → Create implementation design from PRD (doc_id: {doc_id})",
                    domains=["product", "implementation"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

            async def _trigger() -> None:
                try:
                    msg = build_design_trigger(
                        clean_topic,
                        prd_id=doc_id,
                        project_id=state.get("project_id"),
                        research_id=state.get("research_id"),
                    )
                    await self.client.bus_announce(
                        content=msg,
                        domains=["trigger-designer", "product", "implementation"],
                        from_agent=self.from_agent,
                    )
                    logger.info("[prd_agent] ✅ Designer triggered via bus announce")
                except Exception as e:
                    logger.warning("[prd_agent] ⚠️ Failed to trigger designer: %s", e)

            asyncio.create_task(_trigger())

        # Verify memory will be saved by auto_memory (log what we're returning)
        logger.info(
            "[prd_agent] Returning PRD (%d chars) to auto_memory_agent for persistence",
            len(prd),
        )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify_and_trigger", "completed", f"doc_id={doc_id}")
        return state


# ── NAT Registration ──────────────────────────────────────────────────────────


class PRDAgentConfig(FunctionBaseConfig, name="prd_agent"):
    """Configuration for the LangGraph PRD agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for PRD synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing, bus asks, and announcements",
    )
    from_agent: str = Field(
        default="product_owner",
        description="Agent ID for bus announcements and document attribution",
    )
    llm_thinking_name: str = Field(
        default="",
        description="LLM name for thinking-enabled calls (synthesis). Falls back to llm_name if empty.",
    )
    trigger_next_agent: bool = Field(
        default=True,
        description="Whether to trigger the designer agent after PRD is published",
    )


@register_function(config_type=PRDAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def prd_agent_fn(
    config: PRDAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph PRD pipeline and register as a NAT function."""
    logger.info("[prd_agent] Initializing LangGraph PRD agent")

    # Get LangChain-compatible LLM from NAT builder
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    llm_thinking = None
    if config.llm_thinking_name:
        llm_thinking = await builder.get_llm(config.llm_thinking_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
        logger.info("[prd_agent] Thinking LLM loaded: %s", config.llm_thinking_name)
    client = NCMSHttpClient(hub_url=config.hub_url)

    # Build the LangGraph pipeline
    agent = PRDAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
        llm_thinking=llm_thinking,
        trigger_next_agent=config.trigger_next_agent,
    )
    ensure_langchain_instrumented()
    graph = await agent.build_graph()
    logger.info("[prd_agent] LangGraph pipeline ready")

    async def _prd(input_message: str) -> str:
        """Run the full PRD pipeline and return the synthesized PRD.

        This is the function that auto_memory_agent calls. The returned
        string (the full markdown PRD) gets saved to NCMS memory
        automatically by the auto_memory wrapper.

        Args:
            input_message: The PRD topic from the user (may contain doc_id reference).

        Returns:
            The synthesized markdown PRD.
        """
        logger.info("[prd_agent] === Starting PRD pipeline ===")
        logger.info("[prd_agent] Topic: %s", input_message[:200])

        project_id = extract_project_id(input_message)
        result = await graph.ainvoke({
            "topic": input_message,
            "source_doc_id": None,
            "source_content": "",
            "research_id": None,
            "research_methodology": {},
            "expert_input": {},
            "prd": "",
            "manifest": {},
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
            "interrupted": False,
        }, config={"recursion_limit": 30})

        prd = result.get("prd", "PRD pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[prd_agent] === Pipeline complete ===")
        logger.info("[prd_agent] PRD: %d chars | Doc ID: %s", len(prd), doc_id)
        logger.info("[prd_agent] Returning to auto_memory for persistence")

        return prd

    try:
        yield FunctionInfo.from_fn(
            _prd,
            description=(
                "Product Requirements Document (PRD) agent. Reads a researcher's "
                "report (if doc_id provided), consults architecture and security "
                "experts via the knowledge bus, and synthesizes a structured PRD "
                "with requirements, acceptance criteria, risk matrix, and success "
                "metrics. Publishes to the document store and optionally triggers "
                "the builder agent. Returns the full PRD."
            ),
        )
    finally:
        await client.close()
        logger.info("[prd_agent] Cleaned up HTTP client")
