"""Builder Agent for the NemoClaw Non-Deterministic demo.

Designs the imdb-identity-service by consulting Architecture and Security agents
via the Knowledge Bus. Uses LLM to reason about what to ask or decide next.
"""

from __future__ import annotations

import logging

from ncms.demo.nemoclaw_nd.llm_agent import LLMAgent

logger = logging.getLogger(__name__)

# Max chars of accumulated context fed to the planner LLM
_PLANNER_CONTEXT_MAX_CHARS = 6000

# LLM inference needs 30-60s; default bus timeout of 5s is far too short
_ASK_TIMEOUT_MS = 120_000


class BuilderAgent(LLMAgent):
    """Builder agent that designs imdb-identity-service autonomously."""

    primary_domain = "identity-service"
    _expertise = ["identity-service", "implementation"]
    _subscriptions = ["architecture", "security"]

    system_prompt = (
        "You are the Builder Agent tasked with designing and implementing the "
        "imdb-identity-service. This is an Express-based microservice for user "
        "authentication backed by PostgreSQL. You must consult the Architecture "
        "and Security agents before making design decisions. Store your design "
        "decisions as you go."
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._turns: list[dict[str, str]] = []

    async def work_loop(
        self,
        max_turns: int = 8,
        on_turn: object = None,
    ) -> list[dict[str, str]]:
        """Run the autonomous design loop.

        Each turn: ask the LLM what to do next, then execute (ask agent,
        store decision, or announce). Returns the list of turn records.

        Args:
            max_turns: Maximum number of reasoning turns.
            on_turn: Optional async callback ``(turn_number, turn_record)``
                called after each turn for live output.
        """
        for turn_num in range(1, max_turns + 1):
            turn_record = await self._execute_turn(turn_num, max_turns)
            self._turns.append(turn_record)

            if on_turn is not None:
                import asyncio

                coro = on_turn(turn_num, turn_record)  # type: ignore[operator]
                if asyncio.iscoroutine(coro):
                    await coro

            # If the LLM said we're done, stop early
            if turn_record.get("action") == "done":
                break

        # Store final design summary
        summary = self._build_summary()
        await self.store_knowledge(
            content=summary,
            domains=["identity-service", "implementation"],
            memory_type="fact",
        )

        return self._turns

    # ── Internal turn execution ────────────────────────────────────────

    async def _execute_turn(
        self,
        turn_num: int,
        max_turns: int,
    ) -> dict[str, str]:
        """Execute a single reasoning turn."""
        history_text = self._format_history()

        # Build action guidance based on what's been done so far
        actions_taken = [t.get("action", "") for t in self._turns]
        arch_asks = sum(1 for a in actions_taken if a == "ask_architecture")
        sec_asks = sum(1 for a in actions_taken if a == "ask_security")
        decisions = sum(1 for a in actions_taken if a in ("decide", "announce"))

        guidance: list[str] = []
        if arch_asks == 0:
            guidance.append(
                "You have NOT yet consulted the Architecture agent. "
                "Start by asking about service structure and ADRs."
            )
        elif sec_asks == 0:
            guidance.append(
                "You have consulted Architecture but NOT Security. "
                "You MUST ask the Security agent about threats, OWASP, "
                "and compliance before making decisions."
            )
        elif decisions == 0:
            guidance.append(
                "You have consulted both agents. Now DECIDE on key design "
                "choices based on their answers. Use DECIDE: to record each."
            )
        elif turn_num >= max_turns - 1:
            guidance.append(
                "This is one of your final turns. Use ANNOUNCE: to broadcast "
                "your design, or DONE: to wrap up with a summary."
            )
        else:
            guidance.append(
                "Continue making decisions or ask follow-up questions. "
                "Vary your actions — don't repeat the same question."
            )

        guidance_text = "\n".join(guidance)

        planner_prompt = (
            f"## Design Task\n"
            f"Design the imdb-identity-service (Express + PostgreSQL auth "
            f"microservice).\n\n"
            f"## Progress So Far (turn {turn_num}/{max_turns})\n"
            f"{history_text}\n\n"
            f"## Guidance\n{guidance_text}\n\n"
            f"## Instructions\n"
            f"Respond with EXACTLY ONE action. The first line MUST be the "
            f"action tag followed by your content:\n\n"
            f"ASK_ARCHITECTURE: <question about structure, patterns, ADRs>\n"
            f"ASK_SECURITY: <question about threats, OWASP, compliance>\n"
            f"DECIDE: <a concrete design decision to record>\n"
            f"ANNOUNCE: <a finalized decision to broadcast to all agents>\n"
            f"DONE: <final design summary>\n\n"
            f"IMPORTANT: Do NOT repeat a previous question. Each turn must "
            f"make new progress. Ask different questions or make decisions."
        )

        raw = await self._call_llm_text(planner_prompt)
        if not raw:
            return {"action": "error", "detail": "LLM call failed"}

        return await self._parse_and_execute(raw)

    async def _parse_and_execute(self, raw: str) -> dict[str, str]:
        """Parse the LLM action and execute it."""
        first_line = raw.strip().split("\n")[0].strip()

        if first_line.startswith("ASK_ARCHITECTURE:"):
            question = first_line[len("ASK_ARCHITECTURE:") :].strip()
            if not question:
                question = raw.split("\n", 1)[-1].strip()[:300]
            return await self._do_ask("architecture", question)

        if first_line.startswith("ASK_SECURITY:"):
            question = first_line[len("ASK_SECURITY:") :].strip()
            if not question:
                question = raw.split("\n", 1)[-1].strip()[:300]
            return await self._do_ask("security", question)

        if first_line.startswith("DECIDE:"):
            decision = first_line[len("DECIDE:") :].strip()
            if not decision:
                decision = raw.split("\n", 1)[-1].strip()[:500]
            return await self._do_decide(decision)

        if first_line.startswith("ANNOUNCE:"):
            announcement = first_line[len("ANNOUNCE:") :].strip()
            if not announcement:
                announcement = raw.split("\n", 1)[-1].strip()[:500]
            return await self._do_announce(announcement)

        if first_line.startswith("DONE:"):
            summary = first_line[len("DONE:") :].strip()
            if not summary:
                summary = raw.split("\n", 1)[-1].strip()[:500]
            return {"action": "done", "detail": summary}

        # Couldn't parse -- treat as a decision
        return await self._do_decide(raw.strip()[:500])

    async def _do_ask(self, target: str, question: str) -> dict[str, str]:
        """Ask another agent via the Knowledge Bus."""
        if target == "architecture":
            domains = ["architecture", "calm-model", "quality", "decisions"]
        else:
            domains = ["security", "threats", "compliance", "controls"]

        try:
            response = await self.ask_knowledge(
                question=question,
                domains=domains,
                urgency="important",
                timeout_ms=_ASK_TIMEOUT_MS,
            )
            if response:
                answer = response.knowledge.content[:500]
                # Store the answer as our own knowledge
                await self.store_knowledge(
                    content=(f"[from {target}] Q: {question[:200]} A: {answer}"),
                    domains=["identity-service"],
                )
                return {
                    "action": f"ask_{target}",
                    "question": question,
                    "answer": answer,
                    "confidence": f"{response.confidence:.2f}",
                }
            return {
                "action": f"ask_{target}",
                "question": question,
                "answer": "(no response)",
                "confidence": "0.0",
            }
        except Exception as e:
            logger.warning("Ask %s failed: %s", target, e)
            return {
                "action": f"ask_{target}",
                "question": question,
                "answer": f"(error: {e})",
                "confidence": "0.0",
            }

    async def _do_decide(self, decision: str) -> dict[str, str]:
        """Store a design decision."""
        await self.store_knowledge(
            content=f"[design-decision] {decision}",
            domains=["identity-service", "implementation"],
            memory_type="fact",
        )
        return {"action": "decide", "detail": decision}

    async def _do_announce(self, content: str) -> dict[str, str]:
        """Announce a design decision to all agents."""
        await self.announce_knowledge(
            event="created",
            domains=["identity-service", "implementation"],
            content=content,
        )
        return {"action": "announce", "detail": content}

    # ── Helpers ─────────────────────────────────────────────────────────

    def _format_history(self) -> str:
        """Format turn history for the planner prompt."""
        if not self._turns:
            return "(no actions taken yet -- this is your first turn)"

        parts: list[str] = []
        total_len = 0
        for i, turn in enumerate(self._turns, 1):
            action = turn.get("action", "unknown")
            if action.startswith("ask_"):
                line = (
                    f"Turn {i}: Asked {action[4:]} -- "
                    f"Q: {turn.get('question', '')[:100]} "
                    f"A: {turn.get('answer', '')[:150]}"
                )
            elif action == "decide":
                line = f"Turn {i}: Decided -- {turn.get('detail', '')[:200]}"
            elif action == "announce":
                line = f"Turn {i}: Announced -- {turn.get('detail', '')[:200]}"
            elif action == "done":
                line = f"Turn {i}: Done -- {turn.get('detail', '')[:200]}"
            else:
                line = f"Turn {i}: {action} -- {turn.get('detail', '')[:200]}"

            if total_len + len(line) > _PLANNER_CONTEXT_MAX_CHARS:
                parts.append("... (earlier turns truncated)")
                break
            parts.append(line)
            total_len += len(line)

        return "\n".join(parts)

    def _build_summary(self) -> str:
        """Build a final design summary from all turns."""
        decisions = [
            t.get("detail", "")
            for t in self._turns
            if t.get("action") in ("decide", "announce", "done")
        ]
        asks = [
            f"Q: {t.get('question', '')[:100]} -> A: {t.get('answer', '')[:150]}"
            for t in self._turns
            if t.get("action", "").startswith("ask_")
        ]

        parts = ["# imdb-identity-service Design Summary\n"]
        if asks:
            parts.append("## Consultations")
            parts.extend(f"- {a}" for a in asks)
            parts.append("")
        if decisions:
            parts.append("## Decisions")
            parts.extend(f"- {d}" for d in decisions)

        return "\n".join(parts)
