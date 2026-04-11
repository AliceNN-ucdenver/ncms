"""LLM-powered agent base class for the NemoClaw Non-Deterministic demo.

Extends KnowledgeAgent with LLM-backed on_ask: retrieves relevant memories
via search, builds context, and calls the LLM to synthesize a response.
"""

from __future__ import annotations

import logging

from ncms.domain.models import (
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    SnapshotEntry,
)
from ncms.interfaces.agent.base import KnowledgeAgent

logger = logging.getLogger(__name__)

# Max chars of context to send to the LLM (fits comfortably in 32K window)
_CONTEXT_MAX_CHARS = 3000


class LLMAgent(KnowledgeAgent):
    """Base class for LLM-powered agents.

    Subclasses must set:
    - ``primary_domain``: domain to search when answering
    - ``system_prompt``: LLM system message
    - ``_expertise``: list of expertise domains
    - ``_subscriptions``: list of subscription domains
    """

    primary_domain: str = "general"
    system_prompt: str = "You are a helpful agent."
    llm_model: str = ""
    llm_api_base: str | None = None

    _expertise: list[str] = []
    _subscriptions: list[str] = []

    def declare_expertise(self) -> list[str]:
        return list(self._expertise)

    def declare_subscriptions(self) -> list[str]:
        return list(self._subscriptions)

    # ── on_ask: search + LLM synthesis ─────────────────────────────────

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Search memory for context, then call LLM to synthesize a response."""
        results = await self._memory.search(
            query=ask.question,
            domain=self.primary_domain,
            limit=5,
            agent_id=self.agent_id,
        )

        # Build context from search results
        context_parts: list[str] = []
        total_len = 0
        for r in results:
            snippet = r.memory.content[:500]
            if total_len + len(snippet) > _CONTEXT_MAX_CHARS:
                break
            context_parts.append(snippet)
            total_len += len(snippet)

        context_block = "\n---\n".join(context_parts) if context_parts else "(no context found)"

        user_prompt = (
            f"## Question from {ask.from_agent}\n{ask.question}\n\n"
            f"## Relevant Knowledge\n{context_block}\n\n"
            "Provide a concise, expert answer. Cite specific IDs, references, "
            "or document sections where possible."
        )

        # Call LLM
        answer = await self._call_llm_text(user_prompt)
        if not answer:
            # Fallback: return best search result raw
            if results:
                answer = results[0].memory.content[:500]
            else:
                return None

        confidence = min(0.92, 0.6 + 0.05 * len(context_parts))

        return KnowledgeResponse(
            ask_id=ask.ask_id,
            from_agent=self.agent_id,
            confidence=confidence,
            knowledge=KnowledgePayload(type="fact", content=answer),
            provenance=KnowledgeProvenance(
                source="memory-store", trust_level="authoritative",
            ),
            source_mode="live",
        )

    # ── collect_working_knowledge ──────────────────────────────────────

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        """Export recent domain memories for snapshot."""
        memories = await self._memory.list_memories(
            domain=self.primary_domain, agent_id=self.agent_id,
        )
        entries: list[SnapshotEntry] = []
        for m in memories[:20]:
            entries.append(
                SnapshotEntry(
                    domain=self.primary_domain,
                    knowledge=KnowledgePayload(type=m.type, content=m.content),  # type: ignore[arg-type]
                    confidence=0.9,
                    volatility="changing",
                )
            )
        return entries

    # ── LLM helper (text response, not JSON) ───────────────────────────

    async def _call_llm_text(self, user_message: str) -> str | None:
        """Call the LLM and return the raw text response.

        Degrades gracefully: returns None on any failure.
        """
        try:
            import litellm

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
            elif any(
                name in self.llm_model.lower()
                for name in ("nemotron", "qwen")
            ):
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False},
                }

            response = await litellm.acompletion(**kwargs)
            raw = response.choices[0].message.content  # type: ignore[union-attr]
            return raw.strip() if raw else None
        except Exception:
            logger.warning(
                "LLM call failed for agent %s, degrading gracefully",
                self.agent_id,
                exc_info=True,
            )
            return None
